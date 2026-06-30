"""
Insider Activity factor — 3 sub-factors from SEC Form 4 data.

Rules:
  - Only count P (purchase) and S (sale); ignore A/M/F (grants/exercises)
  - CEO/CFO open-market purchases weighted 3x vs other insiders
  - Cluster-buy flag (3+ insiders within 30 days) = bonus signal
  - No data → sector median (50); handled by base class fillna(50)

Sub-factors:
  net_dollar_flow   weighted net $ purchased over 90 days (buys positive, sales negative)
  ceo_cfo_flow      CEO/CFO only net $ flow (3x weight captured in composite weighting)
  cluster_flag      1 if cluster buying detected, 0 otherwise (bonus signal)
"""

import logging
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from factors.base import BaseFactor

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 90
_CLUSTER_WINDOW = 30
_CLUSTER_MIN = 3


class InsiderFactor(BaseFactor):
    name = "insider"
    sub_factors = ["net_dollar_flow", "ceo_cfo_flow", "cluster_flag"]
    higher_is_better = {sf: True for sf in sub_factors}

    def load_raw(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        cutoff = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()

        # Codes P (open-market buy) and S (open-market sale) are exactly the
        # signal-bearing open-market trades. Do NOT also require is_open_market=1:
        # that flag is set for P only, which would drop every sale and leave
        # net_dollar_flow unable to ever go negative.
        txns = pd.read_sql(
            f"""SELECT ticker, insider_name, insider_title, transaction_code,
                       shares, price, date, is_open_market, is_ceo_cfo
                FROM insider_transactions
                WHERE date >= '{cutoff}'
                  AND transaction_code IN ('P', 'S')""",
            conn,
        )
        txns["date"] = pd.to_datetime(
            txns["date"].astype(str).str[:10], format="%Y-%m-%d"
        )
        txns["dollar_flow"] = txns.apply(
            lambda row: (row["shares"] or 0) * (row["price"] or 0)
                        * (1 if row["transaction_code"] == "P" else -1),
            axis=1,
        )
        # CEO/CFO purchases weighted 3x
        txns["weighted_flow"] = txns["dollar_flow"] * txns["is_ceo_cfo"].apply(
            lambda x: 3.0 if x == 1 else 1.0
        )

        rows = []
        for _, urow in universe.iterrows():
            ticker = urow["ticker"]
            sector = urow["sector"]
            r: dict = {"ticker": ticker, "sector": sector}

            tt = txns[txns["ticker"] == ticker]
            if tt.empty:
                # No data → NaN → base fills with 50 (sector median)
                rows.append(r)
                continue

            # Net dollar flow (weighted)
            r["net_dollar_flow"] = tt["weighted_flow"].sum()

            # CEO/CFO only flow
            ceo_cfo = tt[tt["is_ceo_cfo"] == 1]
            r["ceo_cfo_flow"] = ceo_cfo["dollar_flow"].sum() if not ceo_cfo.empty else 0.0

            # Cluster buying flag
            purchases = tt[tt["transaction_code"] == "P"].copy()
            cluster = 0.0
            if not purchases.empty:
                for _, row in purchases.iterrows():
                    window_start = row["date"] - pd.Timedelta(days=_CLUSTER_WINDOW)
                    window_buyers = purchases[
                        (purchases["date"] >= window_start) &
                        (purchases["date"] <= row["date"])
                    ]["insider_name"].nunique()
                    if window_buyers >= _CLUSTER_MIN:
                        cluster = 1.0
                        break
            r["cluster_flag"] = cluster

            rows.append(r)

        return pd.DataFrame(rows)
