"""
Analyst estimates — forward EPS and price target consensus.
Stores daily snapshots; computes 30/60/90-day revision deltas after 30+ days of data.
"""

import logging
import sqlite3
from datetime import date, timedelta
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

TABLE = _CFG["estimates"]["table"]
DELTA_WINDOWS = _CFG["estimates"]["delta_windows"]


def _fetch_estimates(ticker: str) -> Optional[dict]:
    try:
        info = yf.Ticker(ticker).info
        eps_fwd = info.get("forwardEps")
        target = info.get("targetMeanPrice")
        analysts = info.get("numberOfAnalystOpinions")
        if eps_fwd is None and target is None:
            return None
        return {
            "ticker": ticker,
            "date": date.today().isoformat(),
            "eps_forward": eps_fwd,
            "price_target": target,
            "num_analysts": analysts,
        }
    except Exception as e:
        logger.debug(f"Estimates fetch failed for {ticker}: {e}")
        return None


def _compute_revision_deltas(conn: sqlite3.Connection, ticker: str, today: str) -> dict:
    """Compute EPS estimate revision deltas for 30/60/90 day windows."""
    deltas = {}
    cur_row = conn.execute(
        f"SELECT eps_forward FROM {TABLE} WHERE ticker=? AND date=?", (ticker, today)
    ).fetchone()
    if not cur_row or cur_row[0] is None:
        return deltas

    current_eps = cur_row[0]
    for window in DELTA_WINDOWS:
        past_date = (date.today() - timedelta(days=window)).isoformat()
        past_row = conn.execute(
            f"""SELECT eps_forward FROM {TABLE}
                WHERE ticker=? AND date <= ? ORDER BY date DESC LIMIT 1""",
            (ticker, past_date),
        ).fetchone()
        if past_row and past_row[0]:
            deltas[f"eps_revision_{window}d"] = current_eps - past_row[0]

    return deltas


def refresh_estimates(tickers: Optional[list[str]] = None) -> dict:
    init_db()
    conn = get_connection()

    if tickers is None:
        tickers = get_equity_tickers(conn)

    today = date.today().isoformat()
    records = []
    errors = 0

    for ticker in tqdm(tickers, desc="Analyst estimates"):
        cur = conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE ticker=? AND date=?", (ticker, today)
        )
        if cur.fetchone():
            continue

        data = _fetch_estimates(ticker)
        if data:
            records.append(data)
        else:
            errors += 1

        if len(records) >= 50:
            conn.executemany(
                f"""INSERT OR REPLACE INTO {TABLE}
                    (ticker, date, eps_forward, price_target, num_analysts)
                    VALUES (:ticker, :date, :eps_forward, :price_target, :num_analysts)""",
                records,
            )
            conn.commit()
            records = []

    if records:
        conn.executemany(
            f"""INSERT OR REPLACE INTO {TABLE}
                (ticker, date, eps_forward, price_target, num_analysts)
                VALUES (:ticker, :date, :eps_forward, :price_target, :num_analysts)""",
            records,
        )
        conn.commit()

    conn.close()
    logger.info(f"Estimates refresh complete — {len(tickers) - errors} tickers updated")
    return {"tickers_updated": len(tickers) - errors, "errors": errors}


def get_revision_deltas(ticker: str, conn: sqlite3.Connection = None) -> dict:
    close_after = conn is None
    if conn is None:
        conn = get_connection()
    today = date.today().isoformat()
    deltas = _compute_revision_deltas(conn, ticker, today)
    if close_after:
        conn.close()
    return deltas
