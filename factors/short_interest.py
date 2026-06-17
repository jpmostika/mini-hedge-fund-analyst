"""
Short Interest factor — 3 sub-factors.
Scored so that LOW / DECLINING short interest = HIGH score (bullish for longs).

Sub-factors:
  short_pct_float   short interest as % of float           (lower = better)
  days_to_cover     shares short / avg daily volume        (lower = better)
  short_change      change vs prior snapshot               (negative = declining = better)
"""

import logging
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from factors.base import BaseFactor

logger = logging.getLogger(__name__)


class ShortInterestFactor(BaseFactor):
    name = "short_interest"
    sub_factors = ["short_pct_float", "days_to_cover", "short_change"]
    # All inverted: lower short interest = higher quality for longs
    higher_is_better = {
        "short_pct_float": False,
        "days_to_cover":   False,
        "short_change":    False,   # most negative change = most bullish
    }

    def load_raw(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        today = date.today().isoformat()
        cutoff = (date.today() - timedelta(days=40)).isoformat()

        si = pd.read_sql(
            f"""SELECT ticker, date, shares_short, short_ratio, short_percent_of_float
                FROM short_interest
                WHERE date >= '{cutoff}'
                ORDER BY ticker, date""",
            conn,
        )

        # Average daily volume (last 30 days) for days-to-cover
        vol = pd.read_sql(
            """SELECT ticker, AVG(volume) AS avg_vol
               FROM daily_prices
               WHERE date >= date('now', '-30 days')
               GROUP BY ticker""",
            conn,
        ).set_index("ticker")

        rows = []
        for _, urow in universe.iterrows():
            ticker = urow["ticker"]
            sector = urow["sector"]
            r: dict = {"ticker": ticker, "sector": sector}

            ts = si[si["ticker"] == ticker].sort_values("date")
            if ts.empty:
                rows.append(r)
                continue

            latest = ts.iloc[-1]

            pct = latest["short_percent_of_float"]
            if not np.isnan(pct):
                r["short_pct_float"] = pct

            # Days to cover = shares_short / avg_daily_volume
            shares_short = latest["shares_short"]
            avg_vol = vol["avg_vol"].get(ticker, np.nan)
            if not np.isnan(shares_short) and not np.isnan(avg_vol) and avg_vol > 0:
                r["days_to_cover"] = shares_short / avg_vol

            # Change vs prior snapshot
            if len(ts) >= 2:
                prev_pct = ts.iloc[-2]["short_percent_of_float"]
                if not np.isnan(pct) and not np.isnan(prev_pct):
                    r["short_change"] = pct - prev_pct   # negative = improving

            rows.append(r)

        return pd.DataFrame(rows)
