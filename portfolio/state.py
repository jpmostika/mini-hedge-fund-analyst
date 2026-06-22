"""
Portfolio state management — CRUD for positions, history, and approvals.
Tracks current holdings, entry prices, unrealized P&L, and factor scores at entry.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

from data.db import get_connection, init_db

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_PORT_CFG = _CFG["portfolio"]
NAV = _PORT_CFG["nav"]


# ── Position CRUD ──────────────────────────────────────────────────────────── #

def get_positions(conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    """Load all current positions."""
    close = conn is None
    if conn is None:
        init_db(); conn = get_connection()
    try:
        df = pd.read_sql("SELECT * FROM portfolio_positions ORDER BY book, ticker", conn)
        return df
    finally:
        if close: conn.close()


def upsert_position(
    ticker: str,
    book: str,
    shares: float,
    target_weight: float,
    entry_price: float,
    sector: str,
    current_price: Optional[float] = None,
    factor_scores: Optional[dict] = None,
    conn: Optional[sqlite3.Connection] = None,
):
    close = conn is None
    if conn is None:
        init_db(); conn = get_connection()
    try:
        now = datetime.utcnow().isoformat()
        cp = current_price or entry_price
        upnl = (cp - entry_price) * shares * (1 if book == "long" else -1)
        conn.execute(
            """INSERT OR REPLACE INTO portfolio_positions
               (ticker, book, shares, target_weight, entry_price, entry_date,
                current_price, unrealized_pnl, sector, factor_scores_json, last_updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, book, shares, target_weight, entry_price, now[:10],
             cp, upnl, sector, json.dumps(factor_scores or {}), now),
        )
        conn.commit()
    finally:
        if close: conn.close()


def close_position(ticker: str, conn: Optional[sqlite3.Connection] = None):
    close = conn is None
    if conn is None:
        conn = get_connection()
    try:
        conn.execute("DELETE FROM portfolio_positions WHERE ticker=?", (ticker,))
        conn.commit()
    finally:
        if close: conn.close()


def refresh_prices(conn: Optional[sqlite3.Connection] = None):
    """Pull latest close prices from daily_prices and update unrealized P&L."""
    close = conn is None
    if conn is None:
        conn = get_connection()
    try:
        positions = get_positions(conn)
        if positions.empty:
            return
        for _, row in positions.iterrows():
            price_row = conn.execute(
                "SELECT close FROM daily_prices WHERE ticker=? ORDER BY date DESC LIMIT 1",
                (row["ticker"],),
            ).fetchone()
            if not price_row:
                continue
            cp = price_row[0]
            ep = row["entry_price"] or cp
            upnl = (cp - ep) * row["shares"] * (1 if row["book"] == "long" else -1)
            conn.execute(
                "UPDATE portfolio_positions SET current_price=?, unrealized_pnl=?, last_updated=? WHERE ticker=?",
                (cp, upnl, datetime.utcnow().isoformat(), row["ticker"]),
            )
        conn.commit()
        logger.info("Portfolio prices refreshed")
    finally:
        if close: conn.close()


# ── History logging ────────────────────────────────────────────────────────── #

def log_trade(
    ticker: str,
    book: str,
    action: str,
    shares: float,
    price: float,
    cost_bps: float = 0.0,
    reason: str = "",
    run_id: str = "",
    conn: Optional[sqlite3.Connection] = None,
):
    close = conn is None
    if conn is None:
        conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO portfolio_history
               (ticker, book, action, shares, price, cost_bps, timestamp, reason, run_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ticker, book, action, shares, price, cost_bps,
             datetime.utcnow().isoformat(), reason, run_id),
        )
        conn.commit()
    finally:
        if close: conn.close()


# ── Approval queue ─────────────────────────────────────────────────────────── #

def queue_approval(
    ticker: str,
    book: str,
    action: str,
    shares: float,
    estimated_price: float,
    cost_bps: float = 0.0,
    notes: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Queue a trade for human approval. Returns the approval ID."""
    close = conn is None
    if conn is None:
        conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO position_approvals
               (ticker, book, action, shares, estimated_price, cost_bps, status, created_at, notes)
               VALUES (?,?,?,?,?,?,'pending',?,?)""",
            (ticker, book, action, shares, estimated_price, cost_bps,
             datetime.utcnow().isoformat(), notes),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        if close: conn.close()


def get_pending_approvals(conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    close = conn is None
    if conn is None:
        conn = get_connection()
    try:
        return pd.read_sql(
            "SELECT * FROM position_approvals WHERE status='pending' ORDER BY created_at",
            conn,
        )
    finally:
        if close: conn.close()


# ── Portfolio summary ──────────────────────────────────────────────────────── #

def portfolio_summary(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Return high-level P&L and exposure summary."""
    close = conn is None
    if conn is None:
        conn = get_connection()
    try:
        refresh_prices(conn)
        df = get_positions(conn)
        if df.empty:
            return {"positions": 0, "message": "Portfolio is empty"}

        longs  = df[df["book"] == "long"]
        shorts = df[df["book"] == "short"]

        long_value  = (longs["shares"]  * longs["current_price"]).sum()
        short_value = (shorts["shares"] * shorts["current_price"]).sum()
        total_upnl  = df["unrealized_pnl"].sum()

        return {
            "total_positions":  len(df),
            "long_positions":   len(longs),
            "short_positions":  len(shorts),
            "long_gross_value": round(long_value, 2),
            "short_gross_value": round(short_value, 2),
            "gross_exposure_pct": round((long_value + short_value) / NAV * 100, 1),
            "net_exposure_pct":  round((long_value - short_value) / NAV * 100, 1),
            "unrealized_pnl":   round(total_upnl, 2),
            "unrealized_pnl_pct": round(total_upnl / NAV * 100, 2),
            "nav":              NAV,
        }
    finally:
        if close: conn.close()
