"""
Pre-Trade Risk Veto — 8 checks, ANY failure = REJECT (no override).

Closing/covering trades always approved.
Every rejection is logged with timestamp and reason.
"""

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from data.db import get_connection
from portfolio.state import get_positions

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_R = _CFG["risk"]
_P = _CFG["portfolio"]
NAV = _P["nav"]

HALT_LOCK = Path(__file__).parent.parent / _R["halt_lock_file"]
CLOSE_ACTIONS = {"close_long", "close_short", "flip_to_short", "flip_to_long"}


class VetoError(Exception):
    pass


def _log_rejection(ticker: str, action: str, check: str, detail: str):
    msg = f"[VETO] {datetime.utcnow().isoformat()} | {ticker} {action} | {check} | {detail}"
    logger.warning(msg)
    # Append to rejection log
    log_path = Path(__file__).parent.parent / "output" / "veto_log.txt"
    log_path.parent.mkdir(exist_ok=True)
    with open(log_path, "a") as f:
        f.write(msg + "\n")


def _is_closing(action: str) -> bool:
    return any(a in action for a in ["close", "cover"])


def check_pre_trade(
    ticker: str,
    action: str,
    shares: float,
    estimated_price: float,
    book: str,
    sector: str,
    conn: Optional[sqlite3.Connection] = None,
    current_weights: Optional[dict[str, float]] = None,
    betas: Optional[dict[str, float]] = None,
) -> tuple[bool, str, float]:
    """
    Run all 8 pre-trade checks.

    Returns:
        (approved: bool, reason: str, approved_shares: float)
        approved_shares may be < shares if earnings blackout applies.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        # Closing/covering trades bypass all checks
        if _is_closing(action):
            return True, "APPROVED (closing)", shares

        trade_value = shares * estimated_price
        trade_weight = trade_value / NAV

        # ── Check 1: Halt lock ───────────────────────────────────────── #
        if HALT_LOCK.exists():
            reason = f"HALT lock active ({HALT_LOCK})"
            _log_rejection(ticker, action, "HALT_LOCK", reason)
            return False, reason, 0

        approved_shares = shares  # may be reduced by earnings blackout

        # ── Check 2: Earnings blackout ──────────────────────────────── #
        cutoff = (date.today() + timedelta(days=_R["earnings_blackout_days"])).isoformat()
        earn_row = conn.execute(
            "SELECT earnings_date FROM earnings_calendar WHERE ticker=? AND earnings_date BETWEEN date('now') AND ?",
            (ticker, cutoff),
        ).fetchone()
        if earn_row:
            approved_shares = shares * 0.5
            logger.info(f"[pre_trade] {ticker}: earnings blackout → size halved to {approved_shares:.0f} shares")

        # ── Check 3: Liquidity (trade <= 5% of 20-day ADV) ─────────── #
        adv_row = conn.execute(
            f"""SELECT AVG(close * volume) FROM daily_prices
                WHERE ticker=? AND date >= date('now', '-{_P["adv_lookback_days"]+5} days')""",
            (ticker,),
        ).fetchone()
        adv_usd = float(adv_row[0]) if adv_row and adv_row[0] else 0
        approved_trade_value = approved_shares * estimated_price
        if adv_usd > 0 and approved_trade_value > _R["liquidity_adv_max"] * adv_usd:
            reason = f"Liquidity: trade ${approved_trade_value:,.0f} > {_R['liquidity_adv_max']*100:.0f}% of ADV ${adv_usd:,.0f}"
            _log_rejection(ticker, action, "LIQUIDITY", reason)
            return False, reason, 0

        # ── Check 4: Position size <= 5% AUM ───────────────────────── #
        if trade_weight > _R["max_position_pct"]:
            reason = f"Position {trade_weight*100:.1f}% > max {_R['max_position_pct']*100:.0f}% AUM"
            _log_rejection(ticker, action, "POSITION_SIZE", reason)
            return False, reason, 0

        # Load current positions for portfolio-level checks
        positions = get_positions(conn)
        cw = current_weights or {}
        if cw and not positions.empty:
            pass
        elif not positions.empty:
            for _, row in positions.iterrows():
                cp = row["current_price"] or estimated_price
                sign = 1.0 if row["book"] == "long" else -1.0
                cw[row["ticker"]] = sign * row["shares"] * cp / NAV

        # Proposed new weight
        sign = 1.0 if book == "long" else -1.0
        proposed_weight = sign * approved_shares * estimated_price / NAV
        new_cw = {**cw, ticker: cw.get(ticker, 0) + proposed_weight}

        # ── Check 5: Sector <= 25% one-side ────────────────────────── #
        sector_long  = sum(v for t, v in new_cw.items() if v > 0
                          and _get_sector(t, conn) == sector)
        sector_short = abs(sum(v for t, v in new_cw.items() if v < 0
                              and _get_sector(t, conn) == sector))
        if max(sector_long, sector_short) > _R["max_sector_one_side_pct"]:
            reason = (f"Sector '{sector}': long={sector_long*100:.1f}% "
                      f"short={sector_short*100:.1f}% > {_R['max_sector_one_side_pct']*100:.0f}%")
            _log_rejection(ticker, action, "SECTOR", reason)
            return False, reason, 0

        # ── Check 6: Gross <= 165%, net [-10%, +15%] ────────────────── #
        gross = sum(abs(v) for v in new_cw.values())
        net   = sum(new_cw.values())
        if gross > _R["max_gross"]:
            reason = f"Gross exposure {gross*100:.1f}% > max {_R['max_gross']*100:.0f}%"
            _log_rejection(ticker, action, "GROSS", reason)
            return False, reason, 0
        if not (_R["net_min"] <= net <= _R["net_max"]):
            reason = f"Net exposure {net*100:.1f}% outside [{_R['net_min']*100:.0f}%, {_R['net_max']*100:.0f}%]"
            _log_rejection(ticker, action, "NET", reason)
            return False, reason, 0

        # ── Check 7: |net beta| <= 0.20 ─────────────────────────────── #
        if betas:
            net_beta = sum(w * betas.get(t, 1.0) for t, w in new_cw.items())
            if abs(net_beta) > _R["max_net_beta"]:
                reason = f"|Net beta| {net_beta:.3f} > {_R['max_net_beta']}"
                _log_rejection(ticker, action, "BETA", reason)
                return False, reason, 0

        # ── Check 8: Pairwise correlation <= 0.80 ───────────────────── #
        corr_fail = _check_correlation(ticker, list(new_cw.keys()), conn)
        if corr_fail:
            reason = f"Pairwise correlation with {corr_fail} > {_R['max_pairwise_corr']}"
            _log_rejection(ticker, action, "CORRELATION", reason)
            return False, reason, 0

        return True, "APPROVED", approved_shares

    finally:
        if close_conn:
            conn.close()


def _get_sector(ticker: str, conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT sector FROM universe WHERE ticker=?", (ticker,)).fetchone()
    return row[0] if row else "Unknown"


def _check_correlation(
    new_ticker: str,
    existing_tickers: list[str],
    conn: sqlite3.Connection,
    window: int = 60,
) -> Optional[str]:
    """Return the first ticker with correlation > threshold, or None."""
    all_t = list(set(existing_tickers + [new_ticker]))
    placeholders = ",".join("?" * len(all_t))
    prices = pd.read_sql(
        f"""SELECT ticker, date, close FROM daily_prices
            WHERE ticker IN ({placeholders})
              AND date >= date('now', '-{window + 5} days')
            ORDER BY date""",
        conn,
        params=all_t,
    )
    if prices.empty or new_ticker not in prices["ticker"].values:
        return None

    pivot   = prices.pivot(index="date", columns="ticker", values="close")
    returns = pivot.pct_change().dropna(how="all").tail(window)

    if new_ticker not in returns.columns:
        return None

    for other in existing_tickers:
        if other == new_ticker or other not in returns.columns:
            continue
        common = returns[[new_ticker, other]].dropna()
        if len(common) < 20:
            continue
        corr = common.corr().iloc[0, 1]
        if abs(corr) > _R["max_pairwise_corr"]:
            return other

    return None
