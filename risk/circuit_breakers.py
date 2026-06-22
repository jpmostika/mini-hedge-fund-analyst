"""
Circuit Breakers — fire on actual dollar losses. No override possible.

Thresholds:
  Daily  > 1.5% → SIZE_DOWN 30%
  Daily  > 2.5% → CLOSE_ALL_TODAY
  Weekly > 4.0% → SIZE_DOWN 30%
  Drawdown > 8% → KILL_SWITCH (write halt lock file)
  Single position > 3% NAV loss → force-close immediately
"""

import json
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

import sqlite3
import pandas as pd
import yaml

from data.db import get_connection

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_R   = _CFG["risk"]
_P   = _CFG["portfolio"]
NAV  = _P["nav"]

HALT_LOCK = Path(__file__).parent.parent / _R["halt_lock_file"]


def _write_halt(reason: str, breaker: str):
    HALT_LOCK.parent.mkdir(exist_ok=True)
    payload = {
        "timestamp":      datetime.utcnow().isoformat(),
        "reason":         reason,
        "circuit_breaker": breaker,
    }
    HALT_LOCK.write_text(json.dumps(payload, indent=2))
    logger.critical(f"KILL SWITCH ENGAGED: {reason}")


def clear_halt() -> bool:
    """Remove halt lock. Returns True if lock existed."""
    if HALT_LOCK.exists():
        HALT_LOCK.unlink()
        logger.info("Halt lock cleared")
        return True
    return False


def halt_status() -> Optional[dict]:
    """Return halt lock contents or None."""
    if HALT_LOCK.exists():
        try:
            return json.loads(HALT_LOCK.read_text())
        except Exception:
            return {"timestamp": "unknown", "reason": "lock file exists"}
    return None


# ── P&L computation ──────────────────────────────────────────────────────── #

def _compute_daily_pnl(conn: sqlite3.Connection) -> float:
    """Estimate today's portfolio P&L from positions and price changes."""
    from portfolio.state import get_positions
    positions = get_positions(conn)
    if positions.empty:
        return 0.0

    total_pnl = 0.0
    for _, row in positions.iterrows():
        ticker = row["ticker"]
        shares = row["shares"]
        # Get today's and yesterday's close
        prices = conn.execute(
            "SELECT close FROM daily_prices WHERE ticker=? ORDER BY date DESC LIMIT 2",
            (ticker,),
        ).fetchall()
        if len(prices) < 2:
            continue
        today_price = prices[0][0]
        prev_price  = prices[1][0]
        if prev_price and prev_price > 0:
            pct_ret = (today_price - prev_price) / prev_price
            sign = 1.0 if row["book"] == "long" else -1.0
            total_pnl += sign * shares * prev_price * pct_ret

    return total_pnl


def _compute_weekly_pnl(conn: sqlite3.Connection) -> float:
    """Approximate 5-day P&L from position changes."""
    from portfolio.state import get_positions
    positions = get_positions(conn)
    if positions.empty:
        return 0.0

    total_pnl = 0.0
    for _, row in positions.iterrows():
        ticker = row["ticker"]
        prices = conn.execute(
            "SELECT close FROM daily_prices WHERE ticker=? ORDER BY date DESC LIMIT 6",
            (ticker,),
        ).fetchall()
        if len(prices) < 2:
            continue
        today_price = prices[0][0]
        week_ago_price = prices[-1][0]
        if week_ago_price and week_ago_price > 0:
            pct_ret = (today_price - week_ago_price) / week_ago_price
            sign = 1.0 if row["book"] == "long" else -1.0
            total_pnl += sign * row["shares"] * week_ago_price * pct_ret

    return total_pnl


def _compute_drawdown(conn: sqlite3.Connection) -> float:
    """Approximate drawdown from portfolio_history equity curve."""
    rows = conn.execute(
        """SELECT timestamp, price * shares AS trade_value, action
           FROM portfolio_history ORDER BY timestamp"""
    ).fetchall()
    if not rows:
        return 0.0

    # Simple drawdown from unrealized PnL history in positions
    from portfolio.state import get_positions
    positions = get_positions(conn)
    if positions.empty:
        return 0.0

    total_upnl = positions["unrealized_pnl"].sum()
    pnl_pct = total_upnl / NAV
    return min(0.0, pnl_pct)  # only negative values = drawdown


def _position_pnl_pct(row: pd.Series) -> float:
    """Unrealized P&L as % of NAV for a single position."""
    upnl = row.get("unrealized_pnl", 0) or 0
    return upnl / NAV


# ── Main circuit breaker check ────────────────────────────────────────────── #

class CircuitBreakerAction:
    NONE        = "NONE"
    SIZE_DOWN   = "SIZE_DOWN_30"
    CLOSE_ALL   = "CLOSE_ALL_TODAY"
    KILL_SWITCH = "KILL_SWITCH"
    FORCE_CLOSE = "FORCE_CLOSE_POSITION"


def check_circuit_breakers(
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """
    Check all circuit breakers against current portfolio state.
    Returns list of triggered breaker dicts with action recommendations.
    Does NOT execute trades — actions are advisory for run_risk_check.py.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    triggered = []

    try:
        from portfolio.state import get_positions
        positions = get_positions(conn)

        # ── Daily P&L ────────────────────────────────────────────────── #
        daily_pnl  = _compute_daily_pnl(conn)
        daily_pct  = daily_pnl / NAV

        if daily_pct < -_R["daily_loss_close_all"]:
            msg = f"Daily loss {daily_pct*100:.2f}% > {_R['daily_loss_close_all']*100:.1f}%"
            triggered.append({"breaker": "DAILY_LOSS_CLOSE_ALL", "action": CircuitBreakerAction.CLOSE_ALL, "detail": msg})
            logger.critical(f"CIRCUIT BREAKER: {msg}")

        elif daily_pct < -_R["daily_loss_size_down"]:
            msg = f"Daily loss {daily_pct*100:.2f}% > {_R['daily_loss_size_down']*100:.1f}%"
            triggered.append({"breaker": "DAILY_LOSS_SIZE_DOWN", "action": CircuitBreakerAction.SIZE_DOWN, "detail": msg})
            logger.warning(f"CIRCUIT BREAKER: {msg}")

        # ── Weekly P&L ───────────────────────────────────────────────── #
        weekly_pnl = _compute_weekly_pnl(conn)
        weekly_pct = weekly_pnl / NAV

        if weekly_pct < -_R["weekly_loss_size_down"]:
            msg = f"Weekly loss {weekly_pct*100:.2f}% > {_R['weekly_loss_size_down']*100:.1f}%"
            triggered.append({"breaker": "WEEKLY_LOSS_SIZE_DOWN", "action": CircuitBreakerAction.SIZE_DOWN, "detail": msg})
            logger.warning(f"CIRCUIT BREAKER: {msg}")

        # ── Drawdown → KILL SWITCH ───────────────────────────────────── #
        drawdown = _compute_drawdown(conn)
        if drawdown < -_R["drawdown_kill_switch"]:
            msg = f"Drawdown {drawdown*100:.2f}% > {_R['drawdown_kill_switch']*100:.1f}%"
            _write_halt(msg, "DRAWDOWN_KILL_SWITCH")
            triggered.append({"breaker": "DRAWDOWN_KILL_SWITCH", "action": CircuitBreakerAction.KILL_SWITCH, "detail": msg})

        # ── Single position loss > 3% NAV ────────────────────────────── #
        if not positions.empty:
            for _, row in positions.iterrows():
                pos_pnl_pct = _position_pnl_pct(row)
                if pos_pnl_pct < -_R["position_loss_force_close"]:
                    msg = (f"{row['ticker']} unrealized loss "
                           f"{pos_pnl_pct*100:.2f}% NAV > {_R['position_loss_force_close']*100:.0f}%")
                    triggered.append({
                        "breaker": "POSITION_LOSS",
                        "action":  CircuitBreakerAction.FORCE_CLOSE,
                        "ticker":  row["ticker"],
                        "detail":  msg,
                    })
                    logger.warning(f"CIRCUIT BREAKER: {msg}")

        if triggered:
            logger.warning(f"{len(triggered)} circuit breaker(s) triggered")
        else:
            logger.info(f"Circuit breakers: OK | daily={daily_pct*100:.2f}% weekly={weekly_pct*100:.2f}% drawdown={drawdown*100:.2f}%")

        return triggered

    finally:
        if close_conn:
            conn.close()
