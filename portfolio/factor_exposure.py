"""
Factor Exposure Calculator — weighted average of each factor score across
the long and short books. Flags spreads that exceed 1 std dev from historical.
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

FACTOR_NAMES = [
    "momentum", "value", "quality", "growth",
    "revisions", "short_interest", "insider", "institutional",
]


def compute_factor_exposures(
    weights: dict[str, float],
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """
    weights: {ticker: signed_weight}
    Returns dict with long_exposures, short_exposures, spreads, flags.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        tickers = list(weights.keys())
        if not tickers:
            return {}

        # Load factor scores for these tickers
        placeholders = ",".join("?" * len(tickers))
        scores = pd.read_sql(
            f"""SELECT fs.* FROM factor_scores fs
                INNER JOIN (
                    SELECT ticker, MAX(date) AS md FROM factor_scores
                    WHERE ticker IN ({placeholders}) GROUP BY ticker
                ) lp ON fs.ticker=lp.ticker AND fs.date=lp.md""",
            conn,
            params=tickers,
        ).set_index("ticker")

        long_exposures  = {}
        short_exposures = {}

        for factor in FACTOR_NAMES:
            if factor not in scores.columns:
                continue

            long_w_sum  = 0.0
            long_wx     = 0.0
            short_w_sum = 0.0
            short_wx    = 0.0

            for ticker, weight in weights.items():
                if ticker not in scores.index:
                    continue
                score = scores.loc[ticker, factor]
                if pd.isna(score):
                    continue
                if weight > 0:
                    long_w_sum  += weight
                    long_wx     += weight * score
                else:
                    short_w_sum += abs(weight)
                    short_wx    += abs(weight) * score

            long_exposures[factor]  = round(long_wx  / long_w_sum,  1) if long_w_sum  > 0 else None
            short_exposures[factor] = round(short_wx / short_w_sum, 1) if short_w_sum > 0 else None

        # Spreads (long minus short for each factor)
        spreads = {}
        flags   = []
        for factor in FACTOR_NAMES:
            le = long_exposures.get(factor)
            se = short_exposures.get(factor)
            if le is not None and se is not None:
                spread = le - se
                spreads[factor] = round(spread, 1)

        # Flag spreads that are < 1 std dev (weak factor differentiation)
        spread_values = [v for v in spreads.values() if v is not None]
        if spread_values:
            mean_spread = np.mean(spread_values)
            std_spread  = np.std(spread_values)
            for factor, spread in spreads.items():
                if spread < mean_spread - std_spread:
                    flags.append(
                        f"{factor}: spread={spread:.1f} below 1-std floor "
                        f"({mean_spread - std_spread:.1f}) — weak factor differentiation"
                    )

        if flags:
            for f in flags:
                logger.warning(f"[factor_exposure] {f}")

        return {
            "long_exposures":  long_exposures,
            "short_exposures": short_exposures,
            "spreads":         spreads,
            "flags":           flags,
        }

    finally:
        if close_conn:
            conn.close()


def print_exposure_table(exposures: dict):
    if not exposures:
        print("No factor exposure data.")
        return
    print(f"\n{'Factor':<20} {'Long':>8} {'Short':>8} {'Spread':>8}")
    print("-" * 48)
    for factor in FACTOR_NAMES:
        le = exposures["long_exposures"].get(factor)
        se = exposures["short_exposures"].get(factor)
        sp = exposures["spreads"].get(factor)
        print(
            f"  {factor:<18} "
            f"{'N/A' if le is None else f'{le:>6.1f}':>8} "
            f"{'N/A' if se is None else f'{se:>6.1f}':>8} "
            f"{'N/A' if sp is None else f'{sp:>6.1f}':>8}"
        )
    if exposures.get("flags"):
        print("\nFlags:")
        for f in exposures["flags"]:
            print(f"  ! {f}")
