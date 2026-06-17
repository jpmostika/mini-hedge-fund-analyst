"""
Market metrics loader — fetches and caches yfinance .info fields that aren't
stored in the Layer 1 fundamentals table (enterprise value, total revenue,
total assets, price-to-book, etc.).

Uses 20-thread parallelism. Refreshes once per day.
"""

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf
import yaml
from tqdm import tqdm

from data.db import get_connection, init_db

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

TABLE = _CFG["scoring"]["market_metrics_table"]
_WORKERS = 20

_INFO_FIELDS = {
    "marketCap":                 "market_cap",
    "priceToBook":               "price_to_book",
    "enterpriseValue":           "enterprise_value",
    "enterpriseToEbitda":        "enterprise_to_ebitda",
    "totalRevenue":              "total_revenue",
    "totalAssets":               "total_assets",
    "totalCash":                 "total_cash",
    "forwardPE":                 "forward_pe",
    "trailingPE":                "trailing_pe",
    "priceToSalesTrailing12Months": "price_to_sales",
}


def _fetch_one(ticker: str) -> Optional[dict]:
    try:
        info = yf.Ticker(ticker).info
        row = {"ticker": ticker, "date": date.today().isoformat()}
        for yf_key, col in _INFO_FIELDS.items():
            val = info.get(yf_key)
            row[col] = float(val) if val is not None else None
        return row
    except Exception as e:
        logger.debug(f"market_metrics fetch failed for {ticker}: {e}")
        return None


def _is_fresh(conn: sqlite3.Connection, ticker: str) -> bool:
    today = date.today().isoformat()
    cur = conn.execute(f"SELECT 1 FROM {TABLE} WHERE ticker=? AND date=?", (ticker, today))
    return cur.fetchone() is not None


def refresh_market_metrics(tickers: list[str], force: bool = False) -> dict:
    init_db()
    conn = get_connection()

    if not force:
        tickers = [t for t in tickers if not _is_fresh(conn, t)]

    if not tickers:
        logger.info("market_metrics: all tickers fresh, skipping fetch")
        conn.close()
        return {"fetched": 0}

    logger.info(f"Fetching market metrics for {len(tickers)} tickers ({_WORKERS} threads)")
    rows = []

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, t): t for t in tickers}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Market metrics"):
            result = future.result()
            if result:
                rows.append(result)

    if rows:
        cols = ["ticker", "date"] + list(_INFO_FIELDS.values())
        placeholders = ", ".join(f":{c}" for c in cols)
        col_str = ", ".join(cols)
        conn.executemany(
            f"INSERT OR REPLACE INTO {TABLE} ({col_str}) VALUES ({placeholders})",
            rows,
        )
        conn.commit()

    conn.close()
    logger.info(f"market_metrics: {len(rows)} tickers updated")
    return {"fetched": len(rows)}


def load_market_metrics(conn: sqlite3.Connection, tickers: Optional[list[str]] = None) -> pd.DataFrame:
    """Load cached market_metrics into a DataFrame, filtered to most recent date per ticker."""
    query = f"""
        SELECT m.*
        FROM {TABLE} m
        INNER JOIN (
            SELECT ticker, MAX(date) AS max_date FROM {TABLE} GROUP BY ticker
        ) latest ON m.ticker = latest.ticker AND m.date = latest.max_date
    """
    df = pd.read_sql(query, conn)
    if tickers:
        df = df[df["ticker"].isin(tickers)]
    return df.set_index("ticker")
