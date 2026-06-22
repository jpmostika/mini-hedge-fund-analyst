"""
Conviction-Tilt Optimizer — always converges, used as MVO fallback.

Algorithm:
  1. Equal-weight base within each book
  2. Score tilt: top 5% → 1.5x, top 10% → 1.25x, rest → 1.0x
  3. Liquidity cap: no position > 5% of 20-day ADV
  4. Earnings adjustment: halve size if earnings in N days
  5. Beta scale: adjust all weights so |net beta| <= max_beta
  6. Sector neutrality: trim if any sector net exceeds limit
  7. Renormalise to target gross exposures
"""

import logging
import sqlite3
from datetime import date, timedelta
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

_P = _CFG["portfolio"]


def _adv_usd(ticker: str, conn: sqlite3.Connection) -> float:
    """20-day average dollar volume."""
    row = conn.execute(
        f"""SELECT AVG(close * volume) FROM daily_prices
            WHERE ticker=? AND date >= date('now', '-{_P["adv_lookback_days"] + 5} days')""",
        (ticker,),
    ).fetchone()
    return float(row[0]) if row and row[0] else 0.0


def _has_earnings_soon(ticker: str, conn: sqlite3.Connection) -> bool:
    cutoff = (date.today() + timedelta(days=_P["earnings_blackout_days"])).isoformat()
    row = conn.execute(
        "SELECT 1 FROM earnings_calendar WHERE ticker=? AND earnings_date <= ? AND earnings_date >= date('now')",
        (ticker, cutoff),
    ).fetchone()
    return row is not None


def run_conviction(
    long_tickers: list[str],
    short_tickers: list[str],
    scores: dict[str, float],
    betas: dict[str, float],
    sectors: dict[str, str],
    conn: sqlite3.Connection,
) -> dict[str, float]:
    """
    Returns {ticker: signed_weight}. Always produces a valid portfolio.
    """
    nav = _P["nav"]
    tl  = _P["target_long_gross"]
    ts  = _P["target_short_gross"]

    def _tilt_multiplier(rank_pct: float) -> float:
        if rank_pct >= 0.95:
            return 1.5
        if rank_pct >= 0.90:
            return 1.25
        return 1.0

    def _apply_book(tickers: list[str], sign: float, target_gross: float) -> dict[str, float]:
        if not tickers:
            return {}

        # Score ranks within the book (0=lowest, 1=highest for longs; inverted for shorts)
        book_scores = {t: scores.get(t, 50.0) for t in tickers}
        ranked = sorted(book_scores.items(), key=lambda x: x[1], reverse=(sign > 0))
        n = len(ranked)

        # Base weight with tilt
        raw = {}
        for i, (ticker, _) in enumerate(ranked):
            rank_pct = (n - 1 - i) / max(n - 1, 1)  # 1.0 = best
            raw[ticker] = _tilt_multiplier(rank_pct)

        # Renormalise to target gross
        total = sum(raw.values())
        weights = {t: sign * (v / total) * target_gross for t, v in raw.items()}

        # Liquidity cap: |w| * NAV <= adv_max_pct * ADV_USD
        for ticker in list(weights.keys()):
            adv = _adv_usd(ticker, conn)
            if adv <= 0:
                continue
            max_w = (_P["adv_max_pct"] * adv) / nav
            if abs(weights[ticker]) > max_w:
                weights[ticker] = sign * max_w

        # Earnings adjustment: halve if reporting soon
        for ticker in list(weights.keys()):
            if _has_earnings_soon(ticker, conn):
                weights[ticker] *= 0.5
                logger.debug(f"Earnings blackout: halved {ticker}")

        # Re-normalise after caps/adjustments
        current_gross = sum(abs(v) for v in weights.values())
        if current_gross > 0:
            scale = target_gross / current_gross
            weights = {t: v * scale for t, v in weights.items()}

        return weights

    long_weights  = _apply_book(long_tickers,  +1.0, tl)
    short_weights = _apply_book(short_tickers, -1.0, ts)
    weights = {**long_weights, **short_weights}

    # Beta adjustment: scale all weights to hit |net_beta| <= max_beta
    net_beta = sum(w * betas.get(t, 1.0) for t, w in weights.items())
    if abs(net_beta) > _P["max_beta"]:
        # Scale short book to offset
        correction = net_beta - np.sign(net_beta) * _P["max_beta"]
        short_beta_contrib = sum(w * betas.get(t, 1.0) for t, w in weights.items() if w < 0)
        if abs(short_beta_contrib) > 1e-6:
            scale = 1.0 - correction / short_beta_contrib
            scale = max(0.5, min(1.5, scale))
            for t in list(weights.keys()):
                if weights[t] < 0:
                    weights[t] *= scale
        logger.debug(f"Beta adjustment: net_beta was {net_beta:.3f}, applied scale {scale:.3f}")

    # Sector neutrality: trim if |sector_net| > max_sector_net_pct
    unique_sectors = set(sectors.values())
    for sec in unique_sectors:
        sec_tickers = [t for t in weights if sectors.get(t) == sec]
        sec_long  = sum(weights[t] for t in sec_tickers if weights[t] > 0)
        sec_short = sum(weights[t] for t in sec_tickers if weights[t] < 0)
        sec_net   = sec_long + sec_short
        limit     = _P["max_sector_net_pct"]
        if abs(sec_net) > limit:
            excess = abs(sec_net) - limit
            # Trim the larger side
            if sec_net > 0:
                for t in sec_tickers:
                    if weights[t] > 0:
                        weights[t] = max(0, weights[t] - excess * weights[t] / sec_long)
            else:
                for t in sec_tickers:
                    if weights[t] < 0:
                        weights[t] = min(0, weights[t] - excess * weights[t] / sec_short)

    # Final renormalise to exact gross targets
    final_long_gross  = sum(v for v in weights.values() if v > 0)
    final_short_gross = abs(sum(v for v in weights.values() if v < 0))
    for t in weights:
        if weights[t] > 0 and final_long_gross > 0:
            weights[t] = weights[t] / final_long_gross * tl
        elif weights[t] < 0 and final_short_gross > 0:
            weights[t] = weights[t] / final_short_gross * ts

    net_beta_final = sum(w * betas.get(t, 1.0) for t, w in weights.items())
    logger.info(
        f"Conviction-tilt: {len(long_tickers)} longs / {len(short_tickers)} shorts | "
        f"long_gross={sum(v for v in weights.values() if v>0):.3f} "
        f"short_gross={abs(sum(v for v in weights.values() if v<0)):.3f} "
        f"net_beta={net_beta_final:.3f}"
    )
    return weights
