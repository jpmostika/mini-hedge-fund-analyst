"""
Institutional holdings — 13-F filings from SEC EDGAR for 9 tracked hedge funds.
Parses: fund_name, ticker, shares_held, market_value, report_date.
Flags tickers with 3+ funds opening new positions simultaneously.
"""

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import yaml
from tqdm import tqdm

from data.db import get_connection, init_db
from data.providers import get_providers

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_INST_CFG = _CFG["institutional"]
TABLE = _INST_CFG["table"]
HEDGE_FUNDS = _INST_CFG["hedge_funds"]
NEW_POSITION_MIN_FUNDS = _INST_CFG["new_position_min_funds"]
EDGAR_SUBMISSIONS = _CFG["sec"]["edgar_submissions"]

_last_req = 0.0
_RATE = _CFG["sec"]["rate_limit"]


def _get(url: str, headers: dict) -> requests.Response:
    global _last_req
    gap = 1.0 / _RATE
    elapsed = time.time() - _last_req
    if elapsed < gap:
        time.sleep(gap - elapsed)
    resp = requests.get(url, headers=headers, timeout=30)
    _last_req = time.time()
    return resp


def _fetch_13f_accessions(cik: str, headers: dict) -> list[dict]:
    """Return list of recent 13-F-HR filings for a fund."""
    url = f"{EDGAR_SUBMISSIONS}/CIK{cik}.json"
    try:
        resp = _get(url, headers)
        if resp.status_code != 200:
            return []
        data = resp.json()
        filings = data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])
        results = []
        for form, date_str, acc in zip(forms, dates, accessions):
            if form in ("13F-HR", "13F-HR/A"):
                results.append({"filing_date": date_str, "accession": acc})
                if len(results) >= 4:  # last ~1 year of quarters
                    break
        return results
    except Exception as e:
        logger.error(f"13-F fetch failed for CIK {cik}: {e}")
        return []


def _parse_13f_xml(accession: str, cik: str, headers: dict) -> Optional[pd.DataFrame]:
    """Download and parse 13-F info table XML."""
    import xml.etree.ElementTree as ET
    import re

    acc_clean = accession.replace("-", "")
    cik_int = int(cik)
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{acc_clean}-index.htm"

    try:
        resp = _get(index_url, headers)
        xml_files = re.findall(r'href="([^"]*infotable[^"]*\.xml)"', resp.text, re.IGNORECASE)
        if not xml_files:
            # fallback: any .xml that isn't the primary doc
            xml_files = re.findall(r'href="([^"]+\.xml)"', resp.text, re.IGNORECASE)

        if not xml_files:
            return None

        xml_url = xml_files[0]
        if not xml_url.startswith("http"):
            xml_url = f"https://www.sec.gov{xml_url}" if xml_url.startswith("/") else \
                      f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{xml_url}"

        resp = _get(xml_url, headers)
        if resp.status_code != 200:
            return None

        root = ET.fromstring(resp.text)
        ns_match = re.search(r'xmlns="([^"]+)"', resp.text)
        ns = {"ns": ns_match.group(1)} if ns_match else {}

        rows = []
        tag = lambda t: f"ns:{t}" if ns else t

        for info in root.findall(f".//{tag('infoTable')}", ns):
            def get(t):
                node = info.find(f"{tag(t)}", ns)
                return node.text.strip() if node is not None and node.text else None

            ticker = get("cusip") or get("nameOfIssuer") or ""  # may need CUSIP->ticker mapping
            name = get("nameOfIssuer") or ""
            shares_str = get("sshPrnamt") or get("value")
            value_str = get("value")

            try:
                shares = float(shares_str.replace(",", "")) if shares_str else None
            except ValueError:
                shares = None
            try:
                market_val = float(value_str.replace(",", "")) * 1000 if value_str else None  # values in thousands
            except ValueError:
                market_val = None

            rows.append({
                "issuer_name": name,
                "raw_ticker": ticker,
                "shares_held": shares,
                "market_value": market_val,
            })

        return pd.DataFrame(rows) if rows else None

    except Exception as e:
        logger.error(f"13-F XML parse failed {accession}: {e}")
        return None


def _resolve_cusip_to_ticker(cusip: str) -> Optional[str]:
    """Placeholder — in production integrate a CUSIP->ticker mapping database."""
    return None


def _upsert_holdings(conn: sqlite3.Connection, records: list[dict]):
    if not records:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO institutional_holdings
           (fund_name, ticker, shares_held, market_value, report_date, shares_prev, net_change, last_updated)
           VALUES (:fund_name, :ticker, :shares_held, :market_value, :report_date,
                   :shares_prev, :net_change, :last_updated)""",
        records,
    )
    conn.commit()


def _compute_net_change(conn: sqlite3.Connection, fund_name: str, ticker: str,
                        report_date: str, shares_now: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Return (shares_prev, net_change) by comparing to prior quarter's record."""
    cur = conn.execute(
        """SELECT shares_held FROM institutional_holdings
           WHERE fund_name=? AND ticker=? AND report_date < ?
           ORDER BY report_date DESC LIMIT 1""",
        (fund_name, ticker, report_date),
    )
    row = cur.fetchone()
    if row and row[0] is not None and shares_now is not None:
        return row[0], shares_now - row[0]
    return None, None


def _flag_new_positions(conn: sqlite3.Connection):
    """Tickers where 3+ funds opened a new position in the same quarter."""
    cur = conn.execute(
        """SELECT ticker, report_date, COUNT(DISTINCT fund_name) as cnt
           FROM institutional_holdings
           WHERE shares_prev IS NULL OR shares_prev = 0
           GROUP BY ticker, report_date
           HAVING cnt >= ?""",
        (NEW_POSITION_MIN_FUNDS,),
    )
    flagged = cur.fetchall()
    if flagged:
        logger.info(
            f"New simultaneous positions in {len(flagged)} ticker/quarter combos: "
            f"{[(r[0], r[1]) for r in flagged[:5]]}"
        )
    return flagged


def refresh_institutional(tickers_of_interest: Optional[list[str]] = None) -> dict:
    """
    tickers_of_interest: if provided, only store holdings for these tickers.
                         If None, store all holdings found.
    """
    init_db()
    conn = get_connection()
    providers = get_providers()
    headers = providers.sec_headers()

    total_holdings = 0

    for fund in tqdm(HEDGE_FUNDS, desc="Institutional 13-F"):
        fund_name = fund["name"]
        cik = str(fund["cik"]).lstrip("0").zfill(10)

        filings = _fetch_13f_accessions(cik, headers)
        if not filings:
            logger.warning(f"No 13-F filings found for {fund_name} (CIK {cik})")
            continue

        # Process the most recent filing (latest quarter)
        latest = filings[0]
        report_date = latest["filing_date"]

        # Check if already processed
        cur = conn.execute(
            "SELECT 1 FROM institutional_holdings WHERE fund_name=? AND report_date=? LIMIT 1",
            (fund_name, report_date),
        )
        if cur.fetchone():
            logger.info(f"{fund_name} {report_date} already loaded")
            continue

        df = _parse_13f_xml(latest["accession"], cik, headers)
        if df is None or df.empty:
            logger.warning(f"Empty 13-F parse for {fund_name}")
            continue

        records = []
        now = datetime.utcnow().isoformat()
        for _, row in df.iterrows():
            ticker = row.get("raw_ticker", "")
            if not ticker or len(ticker) > 10:
                continue
            if tickers_of_interest and ticker not in tickers_of_interest:
                continue

            shares_prev, net_change = _compute_net_change(
                conn, fund_name, ticker, report_date, row.get("shares_held")
            )
            records.append({
                "fund_name": fund_name,
                "ticker": ticker,
                "shares_held": row.get("shares_held"),
                "market_value": row.get("market_value"),
                "report_date": report_date,
                "shares_prev": shares_prev,
                "net_change": net_change,
                "last_updated": now,
            })

        _upsert_holdings(conn, records)
        total_holdings += len(records)
        logger.info(f"{fund_name}: {len(records)} holdings stored for {report_date}")

    flagged = _flag_new_positions(conn)
    conn.close()

    logger.info(f"Institutional refresh complete — {total_holdings} holding rows upserted")
    return {
        "holdings_stored": total_holdings,
        "new_position_signals": len(flagged),
    }
