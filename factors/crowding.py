"""
Crowding Detection — synthesises daily factor returns and flags when
pairwise correlations deviate significantly from academic baselines.

Factor return = top-quintile composite minus bottom-quintile composite (daily).
Uses 60-day rolling pairwise correlations between factor return series.
Flags when |actual_corr - baseline| > 0.4.

Academic baselines (Asness et al.):
  momentum / value   ≈ -0.30  (they tend to be negatively correlated)
  momentum / quality ≈ +0.10
"""

import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_CROWD_CFG = _CFG["scoring"]["crowding"]
_WINDOW     = _CROWD_CFG["window_days"]
_THRESHOLD  = _CROWD_CFG["flag_threshold"]
_BASELINES  = _CROWD_CFG["baselines"]

FACTOR_NAMES = [
    "momentum", "value", "quality", "growth",
    "revisions", "short_interest", "insider", "institutional",
]


def _load_factor_scores(conn: sqlite3.Connection) -> pd.DataFrame:
    """Load historical factor scores from factor_scores table."""
    return pd.read_sql(
        f"""SELECT date, ticker, sector, {', '.join(FACTOR_NAMES)}, composite
            FROM factor_scores
            ORDER BY date, ticker""",
        conn,
    )


def _compute_factor_returns(scores: pd.DataFrame) -> pd.DataFrame:
    """
    For each date, compute top-quintile minus bottom-quintile mean score per factor.
    This is a proxy for the daily factor return spread.
    """
    rows = []
    for dt, day_df in scores.groupby("date"):
        row = {"date": dt}
        for fname in FACTOR_NAMES:
            if fname not in day_df.columns:
                continue
            vals = day_df[fname].dropna()
            if len(vals) < 10:
                continue
            q80 = vals.quantile(0.80)
            q20 = vals.quantile(0.20)
            top_mean = vals[vals >= q80].mean()
            bot_mean = vals[vals <= q20].mean()
            row[fname] = top_mean - bot_mean
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    returns = pd.DataFrame(rows).set_index("date")
    returns.index = pd.to_datetime(returns.index)
    return returns.sort_index()


def detect_crowding(conn: sqlite3.Connection) -> dict:
    """
    Returns dict with:
      warnings: list of (factor_pair, rolling_corr, baseline, deviation) strings
      corr_matrix: current 60-day correlation matrix DataFrame
    """
    scores = _load_factor_scores(conn)
    if scores.empty or len(scores["date"].unique()) < _WINDOW:
        logger.info(f"Crowding: insufficient history ({len(scores['date'].unique())} days, need {_WINDOW})")
        return {"warnings": [], "corr_matrix": None, "days_available": len(scores["date"].unique())}

    factor_returns = _compute_factor_returns(scores)
    if factor_returns.empty or len(factor_returns) < _WINDOW:
        return {"warnings": [], "corr_matrix": None}

    # Rolling 60-day correlation of the most recent window
    recent = factor_returns.tail(_WINDOW)
    available_factors = [f for f in FACTOR_NAMES if f in recent.columns]
    corr = recent[available_factors].corr()

    warnings = []

    # Check momentum / value
    _check_pair(corr, "momentum", "value", _BASELINES.get("momentum_value", -0.3),
                _THRESHOLD, warnings)
    # Check momentum / quality
    _check_pair(corr, "momentum", "quality", _BASELINES.get("momentum_quality", 0.1),
                _THRESHOLD, warnings)
    # Check all pairs for extreme correlations (> 0.7 or < -0.7 is unusual)
    for i, f1 in enumerate(available_factors):
        for f2 in available_factors[i+1:]:
            c = corr.loc[f1, f2]
            if abs(c) > 0.7:
                warnings.append(
                    f"HIGH CORRELATION: {f1}/{f2} = {c:.2f} (unusual crowding)"
                )

    if warnings:
        logger.warning(f"Crowding detected: {len(warnings)} warning(s)")
        for w in warnings:
            logger.warning(f"  → {w}")
    else:
        logger.info("Crowding: no anomalies detected")

    return {
        "warnings": warnings,
        "corr_matrix": corr,
        "days_available": len(factor_returns),
    }


def _check_pair(
    corr: pd.DataFrame,
    f1: str, f2: str,
    baseline: float,
    threshold: float,
    warnings: list,
):
    if f1 not in corr.index or f2 not in corr.columns:
        return
    actual = corr.loc[f1, f2]
    deviation = abs(actual - baseline)
    if deviation > threshold:
        warnings.append(
            f"{f1}/{f2}: rolling_corr={actual:.2f}, baseline={baseline:.2f}, "
            f"deviation={deviation:.2f} > {threshold} ← CROWDING FLAG"
        )
