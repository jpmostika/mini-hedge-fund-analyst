"""
Factor Monitor — z-score each factor spread (long minus short) vs universe std.
Alerts when |z| > 1.5 sigma. Cross-checks crowding warnings → HIGH priority.
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

_R = _CFG["risk"]
ALERT_SIGMA = _R["factor_alert_sigma"]

FACTOR_NAMES = [
    "momentum", "value", "quality", "growth",
    "revisions", "short_interest", "insider", "institutional",
]


def monitor_factor_spreads(
    weights: dict[str, float],
    conn: Optional[sqlite3.Connection] = None,
    crowding_warnings: Optional[list[str]] = None,
) -> dict:
    """
    Compute factor spreads for the portfolio (long - short weighted avg)
    and z-score vs the full universe cross-sectional distribution.

    Returns alerts list and z-scores per factor.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        # Load latest factor scores for all tickers
        all_scores = pd.read_sql(
            """SELECT fs.ticker, fs.momentum, fs.value, fs.quality, fs.growth,
                      fs.revisions, fs.short_interest, fs.insider, fs.institutional
               FROM factor_scores fs
               INNER JOIN (
                   SELECT ticker, MAX(date) AS md FROM factor_scores GROUP BY ticker
               ) lp ON fs.ticker=lp.ticker AND fs.date=lp.md""",
            conn,
        ).set_index("ticker")

        if all_scores.empty:
            return {"alerts": [], "z_scores": {}}

        alerts = []
        z_scores = {}

        for factor in FACTOR_NAMES:
            if factor not in all_scores.columns:
                continue

            universe_scores = all_scores[factor].dropna()
            if len(universe_scores) < 10:
                continue

            # Universe cross-sectional distribution
            universe_mean = universe_scores.mean()
            universe_std  = universe_scores.std()
            if universe_std == 0:
                continue

            # Portfolio-weighted long and short factor exposures
            long_scores  = []
            short_scores = []
            long_weights_total  = 0.0
            short_weights_total = 0.0

            for ticker, w in weights.items():
                if ticker not in all_scores.index:
                    continue
                score = all_scores.loc[ticker, factor]
                if pd.isna(score):
                    continue
                if w > 0:
                    long_scores.append((w, score))
                    long_weights_total += w
                elif w < 0:
                    short_scores.append((abs(w), score))
                    short_weights_total += abs(w)

            if long_weights_total == 0 or short_weights_total == 0:
                continue

            long_avg  = sum(w * s for w, s in long_scores)  / long_weights_total
            short_avg = sum(w * s for w, s in short_scores) / short_weights_total
            spread    = long_avg - short_avg

            # Z-score of the spread vs universe std
            z = spread / universe_std
            z_scores[factor] = round(z, 2)

            if abs(z) > ALERT_SIGMA:
                # Check if also flagged by crowding monitor
                is_crowding = crowding_warnings and any(factor in w for w in crowding_warnings)
                priority = "HIGH" if is_crowding else "MEDIUM"
                alert = {
                    "factor":   factor,
                    "z_score":  round(z, 2),
                    "spread":   round(spread, 1),
                    "priority": priority,
                    "message":  (
                        f"Factor '{factor}' spread={spread:.1f} pts "
                        f"z={z:.2f} > {ALERT_SIGMA} sigma"
                        + (" [CROWDING CROSSCHECK = HIGH PRIORITY]" if is_crowding else "")
                    ),
                }
                alerts.append(alert)
                log_fn = logger.critical if priority == "HIGH" else logger.warning
                log_fn(f"[factor_monitor] {alert['message']}")

        if not alerts:
            logger.info(f"Factor monitor: OK | {len(z_scores)} factors checked, none exceeded {ALERT_SIGMA} sigma")

        return {"alerts": alerts, "z_scores": z_scores}

    finally:
        if close_conn:
            conn.close()
