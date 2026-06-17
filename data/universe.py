"""
Universe manager — scrapes S&P 500 from Wikipedia and maintains benchmark/ETF tickers.
Refreshes weekly; caches to SQLite.
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup

from data.db import get_connection, init_db

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_UNI_CFG = _CFG["universe"]
REFRESH_DAYS = _UNI_CFG["refresh_days"]
SP500_URL = _UNI_CFG["sp500_url"]
BENCHMARKS = _UNI_CFG["benchmarks"]
SECTOR_ETFS = _UNI_CFG["sector_etfs"]
MACRO_TICKERS = _UNI_CFG["macro"]


def _needs_refresh(conn: sqlite3.Connection) -> bool:
    cur = conn.execute(
        "SELECT MAX(last_updated) FROM universe WHERE type = 'equity'"
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return True
    last = datetime.fromisoformat(row[0])
    return datetime.utcnow() - last > timedelta(days=REFRESH_DAYS)


def _scrape_sp500() -> pd.DataFrame:
    logger.info(f"Scraping S&P 500 from {SP500_URL}")
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(SP500_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table", {"id": "constituents"})
    if table is None:
        raise RuntimeError("Could not find S&P 500 constituents table on Wikipedia")

    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cols = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cols) >= 4:
            ticker = cols[0].replace(".", "-")  # BRK.B -> BRK-B for yfinance
            rows.append({
                "ticker": ticker,
                "company_name": cols[1],
                "sector": cols[2],
                "sub_industry": cols[3],
                "type": "equity",
            })
    df = pd.DataFrame(rows)
    logger.info(f"Scraped {len(df)} S&P 500 constituents")
    return df


def _build_benchmark_rows() -> list[dict]:
    now = datetime.utcnow().isoformat()
    rows = []
    for t in BENCHMARKS:
        rows.append({"ticker": t, "company_name": t, "sector": "Benchmark", "sub_industry": "Broad Market", "type": "benchmark", "last_updated": now})
    for t in SECTOR_ETFS:
        rows.append({"ticker": t, "company_name": t, "sector": "Benchmark", "sub_industry": "Sector ETF", "type": "etf", "last_updated": now})
    for t in MACRO_TICKERS:
        rows.append({"ticker": t, "company_name": t, "sector": "Macro", "sub_industry": "Macro", "type": "macro", "last_updated": now})
    return rows


def refresh_universe(force: bool = False) -> pd.DataFrame:
    init_db()
    conn = get_connection()

    if not force and not _needs_refresh(conn):
        logger.info("Universe is fresh — skipping scrape")
        df = pd.read_sql("SELECT * FROM universe", conn)
        conn.close()
        return df

    sp500 = _scrape_sp500()
    now = datetime.utcnow().isoformat()
    sp500["last_updated"] = now

    benchmark_rows = _build_benchmark_rows()
    benchmarks_df = pd.DataFrame(benchmark_rows)

    all_tickers = pd.concat([sp500, benchmarks_df], ignore_index=True)

    cur = conn.cursor()
    cur.executemany(
        """INSERT OR REPLACE INTO universe (ticker, company_name, sector, sub_industry, type, last_updated)
           VALUES (:ticker, :company_name, :sector, :sub_industry, :type, :last_updated)""",
        all_tickers.to_dict("records"),
    )
    conn.commit()
    conn.close()

    logger.info(f"Universe saved: {len(sp500)} equities + {len(benchmark_rows)} benchmarks/ETFs/macro")
    return all_tickers


def get_equity_tickers(conn: sqlite3.Connection = None) -> list[str]:
    close_after = conn is None
    if conn is None:
        conn = get_connection()
    cur = conn.execute("SELECT ticker FROM universe WHERE type = 'equity' ORDER BY ticker")
    tickers = [r[0] for r in cur.fetchall()]
    if close_after:
        conn.close()
    return tickers


def get_all_price_tickers(conn: sqlite3.Connection = None) -> list[str]:
    """Returns equities + benchmarks + ETFs (everything needing daily prices)."""
    close_after = conn is None
    if conn is None:
        conn = get_connection()
    cur = conn.execute(
        "SELECT ticker FROM universe WHERE type IN ('equity','benchmark','etf','macro') ORDER BY ticker"
    )
    tickers = [r[0] for r in cur.fetchall()]
    if close_after:
        conn.close()
    return tickers
