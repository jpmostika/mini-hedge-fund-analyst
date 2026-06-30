"""
Momentum factor — 6 sub-factors (George & Hwang 2004 framework).

Sub-factors:
  ret_12_1          12-1 month return (skip recent month to avoid reversal)
  ret_6m            6-month return
  ret_3m            3-month return
  acceleration      recent 3m minus prior 3m (momentum acceleration)
  proximity_52w     price / 52-week high
  rel_strength      6m stock return minus 6m sector ETF return (stock-specific alpha)
"""

import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from factors.base import BaseFactor

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

SECTOR_ETF_MAP: dict[str, str] = _CFG.get("sector_etf_map", {})

# Trading-day approximations
_1M  = 21
_3M  = 63
_6M  = 126
_12M = 252


class MomentumFactor(BaseFactor):
    name = "momentum"
    sub_factors = ["ret_12_1", "ret_6m", "ret_3m", "acceleration", "proximity_52w", "rel_strength"]
    higher_is_better = {sf: True for sf in sub_factors}

    def load_raw(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        prices = pd.read_sql(
            "SELECT ticker, date, close FROM daily_prices "
            "WHERE date >= date('now', '-400 days') ORDER BY ticker, date",
            conn,
        )
        if prices.empty:
            return pd.DataFrame()

        prices["date"] = pd.to_datetime(
            prices["date"].astype(str).str[:10], format="%Y-%m-%d"
        )
        pivot = prices.pivot(index="date", columns="ticker", values="close").sort_index()

        all_tickers = list(pivot.columns)
        today_row = pivot.iloc[-1]

        def price_n_days_ago(n: int) -> pd.Series:
            """Closest available close approximately n trading days ago."""
            cal_days = int(n * 365 / 252)
            target = pivot.index[-1] - pd.Timedelta(days=cal_days)
            candidates = pivot.index[pivot.index <= target]
            if len(candidates) == 0:
                return pd.Series(np.nan, index=all_tickers)
            return pivot.loc[candidates[-1]]

        p_1m  = price_n_days_ago(_1M)
        p_3m  = price_n_days_ago(_3M)
        p_6m  = price_n_days_ago(_6M)
        p_12m = price_n_days_ago(_12M)

        # 52-week high per ticker
        high_52w = pivot.tail(252).max()

        rows = []
        for _, urow in universe.iterrows():
            ticker = urow["ticker"]
            sector = urow["sector"]
            r: dict = {"ticker": ticker, "sector": sector}

            p_now = today_row.get(ticker, np.nan)

            # --- 12-1 month return ---
            p12 = p_12m.get(ticker, np.nan)
            p1  = p_1m.get(ticker, np.nan)
            if p12 > 0 and p1 > 0:
                r["ret_12_1"] = p1 / p12 - 1

            # --- 6-month return ---
            p6 = p_6m.get(ticker, np.nan)
            if p6 > 0 and p_now > 0:
                r["ret_6m"] = p_now / p6 - 1

            # --- 3-month return ---
            p3 = p_3m.get(ticker, np.nan)
            if p3 > 0 and p_now > 0:
                r["ret_3m"] = p_now / p3 - 1

            # --- Acceleration: recent 3m minus prior 3m ---
            if p3 > 0 and p6 > 0 and "ret_3m" in r:
                prior_3m = p3 / p6 - 1
                r["acceleration"] = r["ret_3m"] - prior_3m

            # --- 52-week high proximity ---
            h52 = high_52w.get(ticker, np.nan)
            if h52 > 0 and p_now > 0:
                r["proximity_52w"] = p_now / h52

            # --- Relative strength vs sector ETF ---
            etf = SECTOR_ETF_MAP.get(sector)
            if etf and "ret_6m" in r:
                etf_now = today_row.get(etf, np.nan)
                etf_6m  = p_6m.get(etf, np.nan)
                if etf_6m > 0 and etf_now > 0:
                    etf_ret = etf_now / etf_6m - 1
                    r["rel_strength"] = r["ret_6m"] - etf_ret

            rows.append(r)

        return pd.DataFrame(rows)
