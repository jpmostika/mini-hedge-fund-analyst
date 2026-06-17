"""
Earnings calendar — upcoming earnings dates for next 30 days across the universe.
Fetches via yfinance and stores daily snapshots.
"""

import logging
import sqlite3
from datetime import date, datetime, timedelta
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

TABLE = _CFG["earnings_calendar"]["table"]
LOOKAHEAD_DAYS = _CFG["earnings_calendar"]["lookahead_days"]


def _fetch_earnings_date(ticker: str) -> Optional[dict]:
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None:
            return None

        # calendar can be a dict or DataFrame depending on yfinance version
        if hasattr(cal, "to_dict"):
            cal = cal.to_dict()

        earnings_date = None
        eps_estimate = None

        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed is not None:
                if hasattr(ed, "__iter__") and not isinstance(ed, str):
                    ed = list(ed)
                    earnings_date = str(ed[0])[:10] if ed else None
                else:
                    earnings_date = str(ed)[:10]
            eps_estimate = cal.get("EPS Estimate") or cal.get("Earnings Estimate")

        if earnings_date is None:
            return None

        # Only store if within lookahead window
        today = date.today()
        cutoff = today + timedelta(days=LOOKAHEAD_DAYS)
        try:
            ed_date = date.fromisoformat(earnings_date)
            if ed_date < today or ed_date > cutoff:
                return None
        except ValueError:
            return None

        return {
            "ticker": ticker,
            "earnings_date": earnings_date,
            "eps_estimate": float(eps_estimate) if eps_estimate is not None else None,
            "fetched_date": today.isoformat(),
        }
    except Exception as e:
        logger.debug(f"Earnings calendar fetch failed for {ticker}: {e}")
        return None


def refresh_earnings_calendar(tickers: Optional[list[str]] = None) -> dict:
    init_db()
    conn = get_connection()

    if tickers is None:
        tickers = get_equity_tickers(conn)

    today = date.today().isoformat()
    records = []
    errors = 0

    for ticker in tqdm(tickers, desc="Earnings calendar"):
        data = _fetch_earnings_date(ticker)
        if data:
            records.append(data)
        else:
            errors += 1

        if len(records) >= 100:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {TABLE}
                    (ticker, earnings_date, eps_estimate, fetched_date)
                    VALUES (:ticker, :earnings_date, :eps_estimate, :fetched_date)""",
                records,
            )
            conn.commit()
            records = []

    if records:
        conn.executemany(
            f"""INSERT OR REPLACE INTO {TABLE}
                (ticker, earnings_date, eps_estimate, fetched_date)
                VALUES (:ticker, :earnings_date, :eps_estimate, :fetched_date)""",
            records,
        )
        conn.commit()

    upcoming = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE earnings_date >= ?", (today,)
    ).fetchone()[0]

    conn.close()
    logger.info(f"Earnings calendar refresh complete — {upcoming} upcoming earnings in next {LOOKAHEAD_DAYS} days")
    return {"upcoming_earnings": upcoming, "errors": errors}
