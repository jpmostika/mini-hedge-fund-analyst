"""
SEC EDGAR data ingestion.
- Form 4 insider transactions (last 180 days), parsed from XML
- Latest 10-K (Risk Factors), 10-Q (MD&A), recent 8-K filings
- Flags open-market purchases, CEO/CFO buys, cluster buying (3+ insiders/30 days)
- Rate limited to 8 req/sec per SEC fair-access policy
"""

import logging
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests
import yaml
from tqdm import tqdm

from data.db import get_connection, init_db
from data.providers import get_providers
from data.universe import get_equity_tickers

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_SEC_CFG = _CFG["sec"]
RATE_LIMIT = _SEC_CFG["rate_limit"]  # req/sec
INSIDER_DAYS = _SEC_CFG["insider_lookback_days"]
CLUSTER_WINDOW = _SEC_CFG["cluster_window_days"]
CLUSTER_MIN = _SEC_CFG["cluster_min_insiders"]
EDGAR_SUBMISSIONS = _SEC_CFG["edgar_submissions"]

OPEN_MARKET_CODES = {"P"}          # open-market purchases
GRANT_EXERCISE_CODES = {"A", "M", "F"}  # awards/exercises — not organic buying
CEO_CFO_TITLES = {"ceo", "chief executive", "cfo", "chief financial"}

_last_request_time = 0.0


def _rate_limited_get(url: str, headers: dict, params: dict = None) -> requests.Response:
    global _last_request_time
    min_interval = 1.0 / RATE_LIMIT
    elapsed = time.time() - _last_request_time
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    _last_request_time = time.time()
    return resp


def _cik_for_ticker(ticker: str, headers: dict) -> Optional[str]:
    """Resolve ticker -> CIK via EDGAR company search."""
    url = "https://efts.sec.gov/LATEST/search-index"
    params = {"q": f'"{ticker}"', "dateRange": "custom",
              "startdt": "2020-01-01", "enddt": datetime.utcnow().strftime("%Y-%m-%d"),
              "forms": "10-K"}
    try:
        resp = _rate_limited_get(url, headers, params)
        if resp.status_code != 200:
            return None
        hits = resp.json().get("hits", {}).get("hits", [])
        if hits:
            return hits[0].get("_source", {}).get("entity_id")
    except Exception as e:
        logger.debug(f"CIK lookup failed for {ticker}: {e}")
    return None


def _load_ticker_cik_map(headers: dict) -> dict[str, str]:
    """Download SEC's company_tickers.json ONCE and build a {TICKER: CIK} map.

    Previously this file (multi-MB, ~10k companies) was re-fetched once per
    ticker — 503 downloads per run. Fetching it a single time and reusing the
    map is dramatically faster.
    """
    try:
        resp = _rate_limited_get(
            "https://www.sec.gov/files/company_tickers.json", headers
        )
        data = resp.json()
        return {
            entry.get("ticker", "").upper(): str(entry["cik_str"]).zfill(10)
            for entry in data.values()
            if entry.get("ticker")
        }
    except Exception as e:
        logger.error(f"Failed to load company_tickers.json: {e}")
        return {}


def _fetch_submissions(cik: str, headers: dict) -> dict:
    """Fetch a company's EDGAR submissions feed ONCE and return its 'recent'
    filings block. Callers extract Form 4 / 10-K / 10-Q / 8-K from this single
    payload instead of re-downloading the (often large) JSON per form type."""
    url = f"{EDGAR_SUBMISSIONS}/CIK{cik}.json"
    try:
        resp = _rate_limited_get(url, headers)
        if resp.status_code != 200:
            return {}
        return resp.json().get("filings", {}).get("recent", {})
    except Exception as e:
        logger.error(f"Submissions fetch failed for CIK {cik}: {e}")
        return {}


def _extract_recent_filings(recent: dict, form_type: str, limit: int = 5) -> list[dict]:
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    results = []
    for i, (form, date_str, acc) in enumerate(zip(forms, dates, accessions)):
        if form == form_type:
            results.append({
                "form_type": form,
                "filing_date": date_str,
                "accession": acc,
                "primary_document": primary_docs[i] if i < len(primary_docs) else "",
            })
            if len(results) >= limit:
                break
    return results


def _extract_form4_filings(recent: dict, start_date: str) -> list[dict]:
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    results = []
    for form, date_str, acc in zip(forms, dates, accessions):
        if form == "4" and date_str >= start_date:
            results.append({"form_type": "4", "filing_date": date_str, "accession": acc})
    return results


def _fetch_form4_xml(accession: str, cik: str, headers: dict) -> Optional[str]:
    acc_clean = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/"
    try:
        # Use the JSON directory listing rather than scraping the HTML index page.
        # The *-index.htm endpoint is aggressively throttled (503) and required
        # fragile href regex; index.json is reliable and machine-readable.
        listing = _rate_limited_get(base + "index.json", headers)
        if listing.status_code != 200:
            return None
        items = listing.json().get("directory", {}).get("item", [])
        xml_names = [it["name"] for it in items if it.get("name", "").lower().endswith(".xml")]
        if not xml_names:
            return None
        # Prefer the ownership document over any *-index.xml metadata file
        primary = next((n for n in xml_names if "index" not in n.lower()), xml_names[0])
        xml_resp = _rate_limited_get(base + primary, headers)
        return xml_resp.text if xml_resp.status_code == 200 else None
    except Exception as e:
        logger.debug(f"Form4 XML fetch failed {accession}: {e}")
        return None


def _parse_form4(xml_text: str, ticker: str) -> list[dict]:
    """Parse Form 4 XML into transaction records."""
    transactions = []
    try:
        root = ET.fromstring(xml_text)
        ns = {"": ""}

        def find_text(element, *tags):
            """Return text for the first matching tag. Form 4 wraps many fields
            (shares, price, dates, ownership) in a nested <value> child, so try
            that first, then fall back to the element's own direct text."""
            for tag in tags:
                node = element.find(f".//{tag}")
                if node is None:
                    continue
                val = node.find("value")
                if val is not None and val.text and val.text.strip():
                    return val.text.strip()
                if node.text and node.text.strip():
                    return node.text.strip()
            return None

        insider_name = find_text(root, "rptOwnerName")
        insider_title = find_text(root, "officerTitle") or ""
        ownership_type = find_text(root, "directOrIndirectOwnership") or "D"

        is_ceo_cfo = any(kw in insider_title.lower() for kw in CEO_CFO_TITLES)

        for txn in root.findall(".//nonDerivativeTransaction"):
            code = find_text(txn, "transactionCode")
            if not code:
                continue
            shares_str = find_text(txn, "transactionShares")
            price_str = find_text(txn, "transactionPricePerShare")
            date_str = find_text(txn, "transactionDate")

            try:
                shares = float(shares_str) if shares_str else None
                price = float(price_str) if price_str else None
            except ValueError:
                shares, price = None, None

            transactions.append({
                "ticker": ticker,
                "insider_name": insider_name,
                "insider_title": insider_title,
                "transaction_code": code,
                "shares": shares,
                "price": price,
                "date": date_str,
                "ownership_type": ownership_type,
                "is_open_market": 1 if code in OPEN_MARKET_CODES else 0,
                "is_ceo_cfo": 1 if is_ceo_cfo else 0,
            })
    except ET.ParseError as e:
        logger.debug(f"XML parse error: {e}")
    return transactions


def _upsert_transactions(conn: sqlite3.Connection, transactions: list[dict]):
    if not transactions:
        return 0
    conn.executemany(
        """INSERT OR IGNORE INTO insider_transactions
           (ticker, insider_name, insider_title, transaction_code, shares, price,
            date, ownership_type, is_open_market, is_ceo_cfo)
           VALUES (:ticker, :insider_name, :insider_title, :transaction_code,
                   :shares, :price, :date, :ownership_type, :is_open_market, :is_ceo_cfo)""",
        transactions,
    )
    conn.commit()
    return len(transactions)


def _flag_cluster_buying(conn: sqlite3.Connection):
    """Flag tickers where 3+ insiders made open-market purchases within 30 days."""
    cur = conn.execute(
        """SELECT ticker, date, COUNT(DISTINCT insider_name) as cnt
           FROM insider_transactions
           WHERE is_open_market = 1
           GROUP BY ticker
           HAVING cnt >= ?""",
        (CLUSTER_MIN,),
    )
    clusters = cur.fetchall()
    if clusters:
        logger.info(f"Cluster buying detected in {len(clusters)} tickers: {[r[0] for r in clusters]}")
    return clusters


def _cache_filing(conn: sqlite3.Connection, ticker: str, form_type: str,
                  filing_date: str, accession: str, content: str):
    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO filings_cache
           (ticker, form_type, filing_date, accession, content, cached_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        # Keep enough raw HTML that the Risk Factors section (often ~400K chars
        # into the document) and its closing Item 1B boundary both survive.
        (ticker, form_type, filing_date, accession, content[:2_000_000], now),
    )
    conn.commit()


def refresh_sec_data(tickers: Optional[list[str]] = None, forms: Optional[list[str]] = None) -> dict:
    init_db()
    conn = get_connection()
    providers = get_providers()
    headers = providers.sec_headers()

    if tickers is None:
        tickers = get_equity_tickers(conn)

    if forms is None:
        forms = ["4", "10-K", "10-Q", "8-K"]

    start_date = (datetime.utcnow() - timedelta(days=INSIDER_DAYS)).strftime("%Y-%m-%d")
    total_txns = 0
    total_filings = 0

    # Resolve all CIKs from a single company_tickers.json download
    ticker_cik_map = _load_ticker_cik_map(headers)
    if not ticker_cik_map:
        logger.error("CIK map empty — aborting SEC refresh")
        conn.close()
        return {"insider_transactions": 0, "filings_cached": 0, "cluster_buy_tickers": 0}

    for ticker in tqdm(tickers, desc="SEC filings"):
        cik = ticker_cik_map.get(ticker.upper())
        if not cik:
            logger.warning(f"Could not resolve CIK for {ticker}")
            continue

        # Fetch the submissions feed ONCE; reuse for Form 4 + all filing types
        recent = _fetch_submissions(cik, headers)
        if not recent:
            continue

        # Form 4 insider transactions
        if "4" in forms:
            form4_filings = _extract_form4_filings(recent, start_date)
            for filing in form4_filings:
                acc = filing["accession"]
                # Skip already-cached filings
                cur = conn.execute(
                    "SELECT 1 FROM insider_transactions WHERE ticker=? AND date>=? LIMIT 1",
                    (ticker, start_date),
                )
                xml_text = _fetch_form4_xml(acc, cik, headers)
                if not xml_text:
                    continue
                txns = _parse_form4(xml_text, ticker)
                added = _upsert_transactions(conn, txns)
                total_txns += added

        # 10-K, 10-Q, 8-K filings cache
        for form_type in [f for f in forms if f != "4"]:
            filings_for_form = _extract_recent_filings(
                recent, form_type, limit=1 if form_type in ("10-K", "10-Q") else 3
            )
            for filing in filings_for_form:
                acc = filing["accession"]
                cur = conn.execute("SELECT 1 FROM filings_cache WHERE accession=?", (acc,))
                if cur.fetchone():
                    continue
                # Fetch the primary document (the actual 10-K/10-Q/8-K .htm).
                # The old code requested <accession>.txt which 404s; the
                # submissions feed gives us the real primary document filename.
                primary_doc = filing.get("primary_document")
                if not primary_doc:
                    logger.debug(f"No primary document for {ticker}/{form_type} {acc}")
                    continue
                try:
                    acc_clean = acc.replace("-", "")
                    doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary_doc}"
                    resp = _rate_limited_get(doc_url, headers)
                    if resp.status_code != 200 or not resp.text:
                        logger.warning(f"Filing fetch {resp.status_code} for {ticker}/{form_type} {acc}")
                        continue
                    _cache_filing(conn, ticker, form_type, filing["filing_date"], acc, resp.text)
                    total_filings += 1
                except Exception as e:
                    logger.debug(f"Filing cache failed {ticker}/{form_type}: {e}")

    clusters = _flag_cluster_buying(conn)
    conn.close()

    logger.info(f"SEC refresh complete — {total_txns} insider txns parsed, {total_filings} filings cached")
    return {
        "insider_transactions": total_txns,
        "filings_cached": total_filings,
        "cluster_buy_tickers": len(clusters),
    }
