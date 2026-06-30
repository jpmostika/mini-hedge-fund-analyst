"""
Estimate Revisions factor — 3 sub-factors (30/60/90-day EPS consensus changes).

Degenerates to 50 (neutral) for all tickers until 30+ days of snapshots accumulate.
Equal-weights only available deltas when some windows lack data.
"""

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from factors.base import BaseFactor

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

DELTA_WINDOWS: list[int] = _CFG["estimates"]["delta_windows"]  # [30, 60, 90]


class RevisionsFactor(BaseFactor):
    name = "revisions"
    sub_factors = ["eps_rev_30d", "eps_rev_60d", "eps_rev_90d"]
    higher_is_better = {sf: True for sf in sub_factors}

    def load_raw(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        today = date.today()
        earliest = today - timedelta(days=max(DELTA_WINDOWS) + 5)

        snapshots = pd.read_sql(
            f"""SELECT ticker, date, eps_forward FROM analyst_estimates
                WHERE date >= '{earliest.isoformat()}'
                ORDER BY ticker, date""",
            conn,
        )
        snapshots["date"] = pd.to_datetime(
            snapshots["date"].astype(str).str[:10], format="%Y-%m-%d"
        ).dt.date

        # Check if we have at least 30 days of data for any ticker
        max_history = (snapshots.groupby("ticker")["date"].agg(lambda x: (x.max() - x.min()).days)
                       if not snapshots.empty else pd.Series())

        rows = []
        for _, urow in universe.iterrows():
            ticker = urow["ticker"]
            sector = urow["sector"]
            r: dict = {"ticker": ticker, "sector": sector}

            ts = snapshots[snapshots["ticker"] == ticker].sort_values("date")
            if ts.empty or max_history.get(ticker, 0) < 28:
                # Degenerate: not enough history — return NaN; base will fill with 50
                rows.append(r)
                continue

            # Current EPS estimate
            current_eps = ts[ts["date"] == ts["date"].max()]["eps_forward"].values
            if len(current_eps) == 0 or np.isnan(current_eps[0]):
                rows.append(r)
                continue
            current_eps = current_eps[0]

            for window in DELTA_WINDOWS:
                target_date = today - timedelta(days=window)
                past = ts[ts["date"] <= target_date]
                if past.empty:
                    continue
                past_eps = past.iloc[-1]["eps_forward"]
                if not np.isnan(past_eps):
                    r[f"eps_rev_{window}d"] = current_eps - past_eps

            rows.append(r)

        return pd.DataFrame(rows)
