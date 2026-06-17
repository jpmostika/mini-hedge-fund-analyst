"""
Regime-conditional weights — adjusts composite factor weights based on VIX level.

Low Vol  (VIX < 15):  boost momentum, cut value
Normal   (15–25):     default weights
High Vol (VIX > 25):  boost quality + value, cut momentum
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

_SCORING = _CFG["scoring"]
_VIX_LOW  = _SCORING["vix_low_threshold"]
_VIX_HIGH = _SCORING["vix_high_threshold"]
_VIX_TICKER = _SCORING["vix_ticker"]
_ENABLED = _SCORING["regime_conditional_weights"]

_DEFAULT_WEIGHTS: dict[str, float] = _SCORING["composite_weights"]
_REGIME_WEIGHTS: dict[str, dict] = _SCORING["regime_weights"]


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    """Ensure weights sum to exactly 1.0."""
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()}


def get_current_vix(conn: sqlite3.Connection) -> float:
    """Read the latest VIX close from daily_prices."""
    try:
        row = conn.execute(
            "SELECT close FROM daily_prices WHERE ticker=? ORDER BY date DESC LIMIT 1",
            (_VIX_TICKER,),
        ).fetchone()
        if row and row[0]:
            return float(row[0])
    except Exception as e:
        logger.warning(f"Could not read VIX: {e}")
    return 20.0   # fall back to "normal" regime


def get_weights(conn: sqlite3.Connection) -> tuple[dict[str, float], str]:
    """
    Returns (weights_dict, regime_label).
    Weights are normalised to sum to 1.0.
    """
    if not _ENABLED:
        return _normalize(_DEFAULT_WEIGHTS), "default"

    vix = get_current_vix(conn)

    if vix < _VIX_LOW:
        weights = _normalize(_REGIME_WEIGHTS["low_vol"])
        regime = f"low_vol (VIX={vix:.1f})"
    elif vix > _VIX_HIGH:
        weights = _normalize(_REGIME_WEIGHTS["high_vol"])
        regime = f"high_vol (VIX={vix:.1f})"
    else:
        weights = _normalize(_DEFAULT_WEIGHTS)
        regime = f"normal (VIX={vix:.1f})"

    logger.info(f"Regime: {regime}  weights: {weights}")
    return weights, regime
