"""
Transaction Cost Model — three components per ticker in basis points.

1. Commission: $0 (Alpaca)
2. Spread cost: 5% of average daily H-L range
3. Market impact: coef * sqrt(trade_size_usd / ADV_usd) * daily_vol_bps

All costs returned in bps (1 bp = 0.0001).
Net-of-cost expected returns are fed into MVO so the optimizer prices in execution.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from data.db import get_connection

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_PORT_CFG        = _CFG["portfolio"]
ADV_LOOKBACK     = _PORT_CFG["adv_lookback_days"]
SPREAD_PCT       = _PORT_CFG["tc_spread_pct"]      # 0.05
IMPACT_COEF      = _PORT_CFG["tc_impact_coef"]     # 0.10
NAV              = _PORT_CFG["nav"]


def _load_price_stats(
    tickers: list[str],
    conn: sqlite3.Connection,
) -> pd.DataFrame:
    """Return avg_close, avg_hl_range_pct, avg_volume, daily_vol for each ticker."""
    placeholders = ",".join("?" * len(tickers))
    raw = pd.read_sql(
        f"""SELECT ticker, date, open, high, low, close, volume
            FROM daily_prices
            WHERE ticker IN ({placeholders})
              AND date >= date('now', '-{ADV_LOOKBACK + 10} days')
            ORDER BY ticker, date""",
        conn,
        params=tickers,
    )
    if raw.empty:
        return pd.DataFrame()

    # Compute daily return volatility in pandas (avoids SQLite window fn limitation)
    raw["daily_ret"] = raw.groupby("ticker")["close"].pct_change().abs()
    raw["hl_range_pct"] = (raw["high"] - raw["low"]) / raw["close"].replace(0, float("nan"))

    stats = raw.groupby("ticker").agg(
        avg_close=("close", "mean"),
        avg_hl_range_pct=("hl_range_pct", "mean"),
        avg_volume=("volume", "mean"),
        daily_vol=("daily_ret", "mean"),
    )
    return stats


def estimate_costs(
    tickers: list[str],
    trade_weights: dict[str, float],
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, float]:
    """
    Estimate round-trip transaction costs in bps for each ticker.

    trade_weights: {ticker: |weight change|} as fraction of NAV.
    Returns {ticker: total_cost_bps}.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        stats = _load_price_stats(tickers, conn)
        costs = {}

        for ticker in tickers:
            if ticker not in stats.index:
                costs[ticker] = 15.0  # conservative default if no data
                continue

            row = stats.loc[ticker]
            avg_close      = row["avg_close"] or 0
            hl_range_pct   = row["avg_hl_range_pct"] or 0.01
            avg_volume     = row["avg_volume"] or 1
            daily_vol      = row["daily_vol"] or 0.01

            # 1. Commission: $0 Alpaca
            commission_bps = 0.0

            # 2. Spread cost = SPREAD_PCT * daily H-L range in bps
            spread_bps = SPREAD_PCT * hl_range_pct * 10_000

            # 3. Market impact = coef * sqrt(trade_$/ ADV_$) * daily_vol_bps
            trade_weight  = abs(trade_weights.get(ticker, 0.02))
            trade_usd     = trade_weight * NAV
            adv_usd       = avg_volume * avg_close
            daily_vol_bps = daily_vol * 10_000
            if adv_usd > 0:
                impact_bps = IMPACT_COEF * np.sqrt(trade_usd / adv_usd) * daily_vol_bps
            else:
                impact_bps = 20.0  # illiquid fallback

            total_bps = commission_bps + spread_bps + impact_bps
            costs[ticker] = round(min(total_bps, 100.0), 2)  # cap at 100 bps

            logger.debug(
                f"TC {ticker}: spread={spread_bps:.1f} impact={impact_bps:.1f} "
                f"total={total_bps:.1f} bps"
            )

        return costs

    finally:
        if close_conn:
            conn.close()


def costs_to_return_drag(costs_bps: dict[str, float]) -> dict[str, float]:
    """Convert one-way bps costs to annualised return drag (assume monthly rebalance)."""
    # 12 rebalances/year; round-trip = 2x one-way
    return {t: (c / 10_000) * 2 * 12 for t, c in costs_bps.items()}
