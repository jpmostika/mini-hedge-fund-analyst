"""
Composite score — blends 8 factor scores with regime-conditional weights,
re-ranks within sector for the final 0-100 composite, and flags LONG/SHORT.

Output: output/scored_universe_latest.csv + factor_scores table in SQLite.
"""

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from data.db import get_connection
from factors.base import sector_percentile_rank
from factors.regime_weights import get_weights

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_SCORING = _CFG["scoring"]
_LONG_Q   = _SCORING["long_quintile"]   # 0.80
_SHORT_Q  = _SCORING["short_quintile"]  # 0.20
_OUT_FILE = Path(__file__).parent.parent / _SCORING["output_file"]

FACTOR_NAMES = [
    "momentum", "value", "quality", "growth",
    "revisions", "short_interest", "insider", "institutional",
]


def build_composite(
    factor_dfs: dict[str, pd.DataFrame],
    conn: sqlite3.Connection,
    universe: pd.DataFrame,
) -> pd.DataFrame:
    """
    factor_dfs: {factor_name: DataFrame with [ticker, sector, <factor_name> score]}
    Returns merged DataFrame with all sub-factor scores + composite + signal.
    """
    weights, regime = get_weights(conn)
    logger.info(f"Composite weights -> {weights}")

    # Start from universe to preserve all tickers
    result = universe[["ticker", "sector", "sub_industry"]].copy()

    # Merge each factor's sub-factor columns + factor score
    for fname, df in factor_dfs.items():
        if df.empty:
            result[fname] = 50.0
            continue
        sub_cols = [c for c in df.columns if c not in ("ticker", "sector")]
        result = result.merge(df[["ticker"] + sub_cols], on="ticker", how="left")
        result[fname] = result[fname].fillna(50.0)

    # Weighted blend
    composite_raw = pd.Series(0.0, index=result.index)
    for fname, w in weights.items():
        if fname in result.columns:
            composite_raw += result[fname].fillna(50.0) * w

    result["composite_raw"] = composite_raw

    # Re-rank composite within sector for final 0-100
    result["composite"] = sector_percentile_rank(
        result["composite_raw"], result["sector"], ascending=True
    ).fillna(50.0)

    # LONG / SHORT flag
    result["signal"] = "NEUTRAL"
    result.loc[result["composite"] >= _LONG_Q * 100,  "signal"] = "LONG"
    result.loc[result["composite"] <= _SHORT_Q * 100, "signal"] = "SHORT"

    result["regime"] = regime
    result["score_date"] = date.today().isoformat()

    return result


def save_results(result: pd.DataFrame, conn: sqlite3.Connection):
    """Write CSV and upsert factor_scores table."""
    _OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(_OUT_FILE, index=False)
    logger.info(f"Scores written to {_OUT_FILE}  ({len(result)} tickers)")

    # Upsert into factor_scores
    today = date.today().isoformat()
    rows = []
    for _, row in result.iterrows():
        rows.append({
            "ticker":        row["ticker"],
            "date":          today,
            "sector":        row.get("sector", ""),
            "momentum":      row.get("momentum"),
            "value":         row.get("value"),
            "quality":       row.get("quality"),
            "growth":        row.get("growth"),
            "revisions":     row.get("revisions"),
            "short_interest": row.get("short_interest"),
            "insider":       row.get("insider"),
            "institutional": row.get("institutional"),
            "composite":     row.get("composite"),
            "signal":        row.get("signal"),
        })
    conn.executemany(
        """INSERT OR REPLACE INTO factor_scores
           (ticker, date, sector, momentum, value, quality, growth,
            revisions, short_interest, insider, institutional, composite, signal)
           VALUES (:ticker, :date, :sector, :momentum, :value, :quality, :growth,
                   :revisions, :short_interest, :insider, :institutional, :composite, :signal)""",
        rows,
    )
    conn.commit()


def get_top_candidates(result: pd.DataFrame, n: int = 5) -> tuple[pd.DataFrame, pd.DataFrame]:
    longs  = result[result["signal"] == "LONG"].nlargest(n, "composite")[
        ["ticker", "sector", "composite"] + FACTOR_NAMES]
    shorts = result[result["signal"] == "SHORT"].nsmallest(n, "composite")[
        ["ticker", "sector", "composite"] + FACTOR_NAMES]
    return longs, shorts
