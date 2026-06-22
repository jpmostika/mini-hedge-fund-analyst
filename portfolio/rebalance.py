"""
Rebalance Generator — compares current portfolio to target weights,
generates trade list respecting the turnover budget, estimates costs.

Priority: largest absolute score change trades first.
Turnover budget: max 30% one-way (sum |target - current| / 2 <= budget).
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from data.db import get_connection
from portfolio.state import get_positions, log_trade, queue_approval
from portfolio.transaction_costs import estimate_costs

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_P   = _CFG["portfolio"]
NAV  = _P["nav"]


def generate_trades(
    target_weights: dict[str, float],
    scores: dict[str, float],
    sectors: dict[str, str],
    conn: Optional[sqlite3.Connection] = None,
    whatif: bool = False,
    run_id: str = "",
) -> pd.DataFrame:
    """
    Compare target vs current, apply turnover budget, return trade DataFrame.

    Columns: ticker, book, action, current_weight, target_weight,
             weight_change, shares, estimated_price, cost_bps, priority_score
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        # Current positions → current weights
        positions = get_positions(conn)
        current_weights: dict[str, float] = {}
        current_prices:  dict[str, float] = {}

        for _, row in positions.iterrows():
            ticker = row["ticker"]
            cp     = row["current_price"] or row["entry_price"] or 0
            if cp > 0:
                sign = 1.0 if row["book"] == "long" else -1.0
                current_weights[ticker] = sign * row["shares"] * cp / NAV
                current_prices[ticker]  = cp

        # Get latest prices for new tickers
        all_tickers = list(set(list(target_weights.keys()) + list(current_weights.keys())))
        for ticker in all_tickers:
            if ticker not in current_prices:
                row = conn.execute(
                    "SELECT close FROM daily_prices WHERE ticker=? ORDER BY date DESC LIMIT 1",
                    (ticker,),
                ).fetchone()
                current_prices[ticker] = row[0] if row else 0.0

        # Build trade list
        trades = []
        for ticker in all_tickers:
            cw = current_weights.get(ticker, 0.0)
            tw = target_weights.get(ticker, 0.0)
            dw = tw - cw

            if abs(dw) < 0.001:  # ignore tiny changes
                continue

            book   = "long" if tw >= 0 else "short"
            action = _determine_action(cw, tw)
            price  = current_prices.get(ticker, 0.0)
            shares = abs(dw) * NAV / price if price > 0 else 0

            # Priority = absolute score change
            score_change = abs(scores.get(ticker, 50.0) - 50.0)

            trades.append({
                "ticker":           ticker,
                "sector":           sectors.get(ticker, "Unknown"),
                "book":             book,
                "action":           action,
                "current_weight":   round(cw, 4),
                "target_weight":    round(tw, 4),
                "weight_change":    round(dw, 4),
                "shares":           round(shares, 0),
                "estimated_price":  round(price, 2),
                "priority_score":   score_change,
            })

        if not trades:
            logger.info("No trades required — portfolio is at target")
            return pd.DataFrame()

        df = pd.DataFrame(trades).sort_values("priority_score", ascending=False).reset_index(drop=True)

        # Apply turnover budget
        turnover_budget = _P["turnover_budget"] * NAV
        cumulative_turnover = 0.0
        kept = []
        for _, row in df.iterrows():
            trade_value = abs(row["weight_change"]) * NAV
            if cumulative_turnover + trade_value / 2 > turnover_budget:
                logger.info(
                    f"Turnover budget ({_P['turnover_budget']*100:.0f}%) reached at "
                    f"{row['ticker']} — {len(kept)} trades included"
                )
                break
            kept.append(row)
            cumulative_turnover += trade_value / 2

        df = pd.DataFrame(kept).reset_index(drop=True)

        # Estimate transaction costs
        trade_w = {row["ticker"]: abs(row["weight_change"]) for _, row in df.iterrows()}
        costs   = estimate_costs(df["ticker"].tolist(), trade_w, conn)
        df["cost_bps"] = df["ticker"].map(costs).fillna(15.0)

        logger.info(
            f"Rebalance: {len(df)} trades | "
            f"turnover={cumulative_turnover/NAV*100:.1f}% | "
            f"avg_cost={df['cost_bps'].mean():.1f} bps"
        )

        if not whatif:
            _commit_trades(df, conn, run_id)

        return df

    finally:
        if close_conn:
            conn.close()


def _determine_action(current_w: float, target_w: float) -> str:
    if current_w == 0 and target_w > 0: return "open_long"
    if current_w == 0 and target_w < 0: return "open_short"
    if target_w == 0 and current_w > 0: return "close_long"
    if target_w == 0 and current_w < 0: return "close_short"
    if current_w > 0 and target_w > 0:  return "adjust_long"
    if current_w < 0 and target_w < 0:  return "adjust_short"
    if current_w > 0 and target_w < 0:  return "flip_to_short"
    return "flip_to_long"


def _commit_trades(df: pd.DataFrame, conn: sqlite3.Connection, run_id: str):
    """Queue trades for approval (not executed until Layer 6)."""
    for _, row in df.iterrows():
        queue_approval(
            ticker          = row["ticker"],
            book            = row["book"],
            action          = row["action"],
            shares          = row["shares"],
            estimated_price = row["estimated_price"],
            cost_bps        = row["cost_bps"],
            notes           = f"run_id={run_id}",
            conn            = conn,
        )
    logger.info(f"Queued {len(df)} trades for approval")


def print_trade_table(df: pd.DataFrame):
    if df.empty:
        print("No trades generated.")
        return
    print(f"\n{'Ticker':<8} {'Book':<6} {'Action':<14} {'Curr%':>6} {'Tgt%':>6} {'Chg%':>6} {'Shares':>8} {'Price':>8} {'Cost':>6}")
    print("-" * 90)
    for _, r in df.iterrows():
        print(
            f"  {r['ticker']:<6} {r['book']:<6} {r['action']:<14} "
            f"{r['current_weight']*100:>5.1f}% {r['target_weight']*100:>5.1f}% "
            f"{r['weight_change']*100:>+5.1f}% "
            f"{int(r['shares']):>8,} ${r['estimated_price']:>7.2f} "
            f"{r['cost_bps']:>5.1f}bp"
        )
    total_cost = (df["cost_bps"] * df["shares"] * df["estimated_price"] / 10_000).sum()
    print(f"\nEstimated total transaction cost: ${total_cost:,.0f}")
