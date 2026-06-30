"""
Market data ingestion — daily OHLCV for all universe tickers + benchmarks.
3-year lookback on first run; incremental updates thereafter (fetches only new bars).
"""

import logging
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml
from tqdm import tqdm

from data.db import get_connection, init_db
from data.providers import get_providers
from data.universe import get_all_price_tickers

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

LOOKBACK_YEARS = _CFG["market_data"]["lookback_years"]
TABLE = _CFG["market_data"]["table"]
BATCH_SIZE = 50  # tickers per yfinance download call


def _last_stored_date(conn: sqlite3.Connection, ticker: str) -> Optional[date]:
    cur = conn.execute(f"SELECT MAX(date) FROM {TABLE} WHERE ticker = ?", (ticker,))
    row = cur.fetchone()
    if row and row[0]:
        return date.fromisoformat(str(row[0])[:10])
    return None


def _fetch_start(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, str]:
    """Returns {ticker: start_date_str} — earliest missing date per ticker."""
    today = date.today()
    cutoff = today - timedelta(days=LOOKBACK_YEARS * 365)
    starts = {}
    for ticker in tickers:
        last = _last_stored_date(conn, ticker)
        if last is None:
            starts[ticker] = cutoff.isoformat()
        else:
            next_day = last + timedelta(days=1)
            if next_day <= today:
                starts[ticker] = next_day.isoformat()
            # else already up to date — omit from fetch
    return starts


def _upsert_prices(conn: sqlite3.Connection, df: pd.DataFrame):
    if df.empty:
        return 0
    records = df.to_dict("records")
    conn.executemany(
        f"""INSERT OR REPLACE INTO {TABLE} (ticker, date, open, high, low, close, volume)
            VALUES (:ticker, :date, :open, :high, :low, :close, :volume)""",
        records,
    )
    conn.commit()
    return len(records)


def refresh_prices(tickers: Optional[list[str]] = None) -> dict:
    init_db()
    conn = get_connection()
    providers = get_providers()

    if tickers is None:
        tickers = get_all_price_tickers(conn)

    logger.info(f"Checking price freshness for {len(tickers)} tickers")
    starts = _fetch_start(conn, tickers)

    if not starts:
        logger.info("All price data is current — nothing to fetch")
        conn.close()
        return {"tickers_checked": len(tickers), "bars_added": 0}

    # Group tickers by start date for efficient batching
    date_groups: dict[str, list[str]] = {}
    for ticker, start in starts.items():
        date_groups.setdefault(start, []).append(ticker)

    total_bars = 0
    # yfinance end date is exclusive — add 1 day so today's data is included
    today_str = (date.today() + timedelta(days=1)).isoformat()

    for start_date, group_tickers in date_groups.items():
        logger.info(f"Fetching {len(group_tickers)} tickers from {start_date}")
        for i in tqdm(range(0, len(group_tickers), BATCH_SIZE), desc=f"Prices from {start_date}"):
            batch = group_tickers[i : i + BATCH_SIZE]
            df = None
            for attempt in range(3):
                try:
                    df = providers.get_prices(batch, start_date, today_str)
                    break
                except Exception as e:
                    if "database is locked" in str(e).lower() and attempt < 2:
                        time.sleep(2 ** attempt)
                    else:
                        logger.error(f"Price fetch failed for batch starting {batch[0]}: {e}")
                        break
            if df is not None and not df.empty:
                df["date"] = df["date"].astype(str)
                total_bars += _upsert_prices(conn, df)

    conn.close()
    logger.info(f"Price refresh complete — {total_bars} bars added/updated")
    return {"tickers_checked": len(tickers), "bars_added": total_bars}
