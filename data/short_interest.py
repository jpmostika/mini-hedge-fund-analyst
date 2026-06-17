"""
Short interest data — daily snapshots from yfinance .info.
Fields: shares_short, short_ratio, short_percent_of_float.
"""

import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yfinance as yf
import yaml
from tqdm import tqdm

from data.db import get_connection, init_db
from data.universe import get_equity_tickers

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

TABLE = _CFG["short_interest"]["table"]


def _fetch_short_data(ticker: str) -> Optional[dict]:
    try:
        info = yf.Ticker(ticker).info
        shares_short = info.get("sharesShort")
        short_ratio = info.get("shortRatio")
        short_pct = info.get("shortPercentOfFloat")
        if shares_short is None and short_ratio is None and short_pct is None:
            return None
        return {
            "ticker": ticker,
            "date": date.today().isoformat(),
            "shares_short": shares_short,
            "short_ratio": short_ratio,
            "short_percent_of_float": short_pct,
        }
    except Exception as e:
        logger.debug(f"Short interest fetch failed for {ticker}: {e}")
        return None


def refresh_short_interest(tickers: Optional[list[str]] = None) -> dict:
    init_db()
    conn = get_connection()

    if tickers is None:
        tickers = get_equity_tickers(conn)

    today = date.today().isoformat()
    records = []
    errors = 0

    for ticker in tqdm(tickers, desc="Short interest"):
        # Skip if already fetched today
        cur = conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE ticker=? AND date=?", (ticker, today)
        )
        if cur.fetchone():
            continue

        data = _fetch_short_data(ticker)
        if data:
            records.append(data)
        else:
            errors += 1

        if len(records) >= 50:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {TABLE}
                    (ticker, date, shares_short, short_ratio, short_percent_of_float)
                    VALUES (:ticker, :date, :shares_short, :short_ratio, :short_percent_of_float)""",
                records,
            )
            conn.commit()
            records = []

    if records:
        conn.executemany(
            f"""INSERT OR REPLACE INTO {TABLE}
                (ticker, date, shares_short, short_ratio, short_percent_of_float)
                VALUES (:ticker, :date, :shares_short, :short_ratio, :short_percent_of_float)""",
            records,
        )
        conn.commit()

    conn.close()
    logger.info(f"Short interest refresh complete — {len(tickers) - errors} tickers updated")
    return {"tickers_updated": len(tickers) - errors, "errors": errors}
