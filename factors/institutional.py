"""
Institutional Flow factor — 3 sub-factors from 13-F holdings data.

Sub-factors:
  num_funds_holding       number of tracked hedge funds currently holding
  net_change_shares       aggregate net change in shares vs prior quarter
  simultaneous_open_flag  1 if 3+ funds opened new positions in same quarter
"""

import logging
import sqlite3

import numpy as np
import pandas as pd

from factors.base import BaseFactor

logger = logging.getLogger(__name__)


class InstitutionalFactor(BaseFactor):
    name = "institutional"
    sub_factors = ["num_funds_holding", "net_change_shares", "simultaneous_open_flag"]
    higher_is_better = {sf: True for sf in sub_factors}

    def load_raw(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        # Most recent report_date per fund
        holdings = pd.read_sql(
            """SELECT h.fund_name, h.ticker, h.shares_held, h.net_change, h.report_date, h.shares_prev
               FROM institutional_holdings h
               INNER JOIN (
                   SELECT fund_name, MAX(report_date) AS mr
                   FROM institutional_holdings
                   GROUP BY fund_name
               ) latest ON h.fund_name=latest.fund_name AND h.report_date=latest.mr""",
            conn,
        )

        if holdings.empty:
            logger.warning("No institutional holdings data available")
            return pd.DataFrame()

        # Aggregate per ticker
        agg = holdings.groupby("ticker").agg(
            num_funds_holding=("fund_name", "count"),
            net_change_shares=("net_change", "sum"),
        ).reset_index()

        # Simultaneous new positions: share_prev IS NULL or 0 = new position
        new_positions = holdings[
            holdings["shares_prev"].isna() | (holdings["shares_prev"] == 0)
        ]
        new_pos_count = new_positions.groupby("ticker")["fund_name"].count().rename("new_pos_count")
        agg = agg.merge(new_pos_count, on="ticker", how="left")
        agg["simultaneous_open_flag"] = (agg["new_pos_count"].fillna(0) >= 3).astype(float)

        rows = []
        for _, urow in universe.iterrows():
            ticker = urow["ticker"]
            sector = urow["sector"]
            r: dict = {"ticker": ticker, "sector": sector}

            row = agg[agg["ticker"] == ticker]
            if row.empty:
                rows.append(r)
                continue

            row = row.iloc[0]
            r["num_funds_holding"]    = row["num_funds_holding"]
            r["net_change_shares"]    = row["net_change_shares"]
            r["simultaneous_open_flag"] = row["simultaneous_open_flag"]

            rows.append(r)

        return pd.DataFrame(rows)
