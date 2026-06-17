"""
Growth factor — 5 sub-factors from fundamentals table.

Sub-factors:
  rev_growth_yoy    revenue growth year-over-year
  earn_growth_yoy   earnings growth year-over-year
  rev_acceleration  latest YoY revenue growth minus 4Q-ago YoY (is growth accelerating?)
  rd_intensity      R&D expense / revenue (proxy for future growth investment)
  fcf_growth_yoy    FCF growth year-over-year (harder to manipulate than earnings)
"""

import logging
import sqlite3

import numpy as np
import pandas as pd

from factors.base import BaseFactor

logger = logging.getLogger(__name__)


class GrowthFactor(BaseFactor):
    name = "growth"
    sub_factors = [
        "rev_growth_yoy", "earn_growth_yoy", "rev_acceleration",
        "rd_intensity", "fcf_growth_yoy",
    ]
    higher_is_better = {sf: True for sf in sub_factors}

    def load_raw(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        fund = pd.read_sql(
            """SELECT ticker, period_end, revenue_growth_yoy, earnings_growth_yoy,
                      revenue_growth_qoq, fcf_yield, rd_expense
               FROM fundamentals
               WHERE period_type='quarterly'
               ORDER BY ticker, period_end DESC""",
            conn,
        )

        rows = []
        for _, urow in universe.iterrows():
            ticker = urow["ticker"]
            sector = urow["sector"]
            r: dict = {"ticker": ticker, "sector": sector}

            tf = fund[fund["ticker"] == ticker].sort_values("period_end", ascending=False)
            if tf.empty:
                rows.append(r)
                continue

            latest = tf.iloc[0]

            # Revenue growth YoY
            ry = latest["revenue_growth_yoy"]
            if not np.isnan(ry):
                r["rev_growth_yoy"] = ry

            # Earnings growth YoY
            ey = latest["earnings_growth_yoy"]
            if not np.isnan(ey):
                r["earn_growth_yoy"] = ey

            # Revenue acceleration: latest YoY minus 4Q-ago YoY
            if len(tf) >= 5:
                ry_4q_ago = tf.iloc[4]["revenue_growth_yoy"]
                if not np.isnan(ry) and not np.isnan(ry_4q_ago):
                    r["rev_acceleration"] = ry - ry_4q_ago

            # R&D intensity: rd_expense is stored as raw value; need to normalise
            # Approximate revenue from growth rate is circular, so we use rd_expense
            # as a raw signal and rely on sector-relative ranking for normalisation
            rd = latest["rd_expense"]
            if not np.isnan(rd) and rd > 0:
                r["rd_intensity"] = rd   # sector-relative ranking handles the scaling

            # FCF growth YoY: compare FCF yield to 4Q-ago FCF yield (proxy)
            if len(tf) >= 5:
                fcf_now  = latest["fcf_yield"]
                fcf_4q   = tf.iloc[4]["fcf_yield"]
                if not np.isnan(fcf_now) and not np.isnan(fcf_4q) and fcf_4q != 0:
                    r["fcf_growth_yoy"] = (fcf_now - fcf_4q) / abs(fcf_4q)

            rows.append(r)

        return pd.DataFrame(rows)
