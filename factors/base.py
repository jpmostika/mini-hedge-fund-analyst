"""
Base class and sector-ranking utilities for all factor modules.

Core invariant: every factor returns 0-100 percentile scores ranked
WITHIN each GICS sector. NaN raw values receive 50 (sector median).
"""

import logging
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)


def sector_percentile_rank(
    series: pd.Series,
    sectors: pd.Series,
    ascending: bool = True,
) -> pd.Series:
    """
    Rank values within each sector using percentile rank (0–100).
    ascending=True  → higher raw value = higher score (e.g. ROE)
    ascending=False → lower raw value  = higher score (e.g. D/E, short %)
    NaN inputs stay NaN; caller fills with 50 after.
    """
    df = pd.DataFrame({"value": series, "sector": sectors})
    ranked = df.groupby("sector")["value"].rank(
        pct=True, ascending=ascending, na_option="keep"
    ) * 100
    return ranked


def equal_weight_avg(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    """Mean of available sub-factor columns; NaN sub-factors are skipped."""
    return df[cols].mean(axis=1, skipna=True)


class BaseFactor(ABC):
    """
    Each subclass implements `load_raw()` returning a DataFrame with
    columns [ticker, sector, <sub-factor raw values>].
    `compute()` handles sector ranking and equal-weight averaging.
    """

    name: str           # factor name, matches config composite_weights key
    sub_factors: list[str]
    # True  → higher raw value → higher score
    # False → lower raw value  → higher score (invert rank)
    higher_is_better: dict[str, bool] = {}

    @abstractmethod
    def load_raw(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        """
        Returns DataFrame with at minimum: ticker (str), sector (str),
        plus one column per sub-factor with raw float values.
        """
        ...

    def compute(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        """
        1. Call load_raw()
        2. Sector-percentile-rank each sub-factor
        3. Fill missing ranks with 50 (neutral)
        4. Equal-weight average → factor score
        Returns DataFrame: ticker, sector, <sub-factors as 0-100>, <name as 0-100>
        """
        try:
            raw = self.load_raw(universe, conn)
        except Exception as e:
            logger.error(f"[{self.name}] load_raw failed: {e}", exc_info=True)
            raw = pd.DataFrame()

        # Neutral fallback if load completely failed
        if raw.empty or "ticker" not in raw.columns:
            result = universe[["ticker", "sector"]].copy()
            for sf in self.sub_factors:
                result[sf] = 50.0
            result[self.name] = 50.0
            logger.warning(f"[{self.name}] returning neutral 50 for all tickers (no data)")
            return result

        result = raw[["ticker", "sector"]].copy().reset_index(drop=True)

        for sf in self.sub_factors:
            if sf not in raw.columns or raw[sf].isna().all():
                result[sf] = 50.0
                logger.debug(f"[{self.name}.{sf}] degenerate — all NaN, using 50")
                continue

            ascending = self.higher_is_better.get(sf, True)
            ranked = sector_percentile_rank(raw[sf], raw["sector"], ascending=ascending)
            result[sf] = ranked.fillna(50.0).values

        result[self.name] = equal_weight_avg(result, self.sub_factors)
        return result
