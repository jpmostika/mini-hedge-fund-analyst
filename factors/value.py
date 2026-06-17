"""
Value factor — 6 sub-factors.

Sub-factors:
  fwd_earnings_yield  1 / forward P/E  (eps_forward / price)
  book_to_price       1 / price-to-book  (from market_metrics)
  fcf_yield           free cash flow / market cap  (stored in fundamentals)
  ev_ebitda_inv       1 / EV/EBITDA  (from market_metrics, lower EV/EBITDA = cheaper)
  shareholder_yield   (buybacks + dividends) / market_cap
  sales_to_ev         total_revenue / enterprise_value  (works when P/E is negative)
"""

import logging
import sqlite3

import numpy as np
import pandas as pd

from factors.base import BaseFactor
from factors.loader import load_market_metrics

logger = logging.getLogger(__name__)


class ValueFactor(BaseFactor):
    name = "value"
    sub_factors = [
        "fwd_earnings_yield", "book_to_price", "fcf_yield",
        "ev_ebitda_inv", "shareholder_yield", "sales_to_ev",
    ]
    higher_is_better = {sf: True for sf in sub_factors}

    def load_raw(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        # Latest fundamentals (quarterly)
        fund = pd.read_sql(
            """SELECT f.ticker, f.fcf_yield, f.shares_outstanding,
                      f.dividends_paid, f.buybacks
               FROM fundamentals f
               INNER JOIN (
                   SELECT ticker, MAX(period_end) AS pe
                   FROM fundamentals WHERE period_type='quarterly'
                   GROUP BY ticker
               ) latest ON f.ticker=latest.ticker AND f.period_end=latest.pe
               WHERE f.period_type='quarterly'""",
            conn,
        ).set_index("ticker")

        # Latest price
        prices = pd.read_sql(
            """SELECT p.ticker, p.close AS price
               FROM daily_prices p
               INNER JOIN (
                   SELECT ticker, MAX(date) AS md FROM daily_prices GROUP BY ticker
               ) lp ON p.ticker=lp.ticker AND p.date=lp.md""",
            conn,
        ).set_index("ticker")

        # Latest analyst estimates (for forward EPS)
        estimates = pd.read_sql(
            """SELECT e.ticker, e.eps_forward
               FROM analyst_estimates e
               INNER JOIN (
                   SELECT ticker, MAX(date) AS md FROM analyst_estimates GROUP BY ticker
               ) le ON e.ticker=le.ticker AND e.date=le.md""",
            conn,
        ).set_index("ticker")

        # Market metrics (enterprise value, price-to-book, total_revenue)
        metrics = load_market_metrics(conn, tickers=universe["ticker"].tolist())

        rows = []
        for _, urow in universe.iterrows():
            ticker = urow["ticker"]
            sector = urow["sector"]
            r: dict = {"ticker": ticker, "sector": sector}

            price = prices["price"].get(ticker, np.nan)
            shares = fund["shares_outstanding"].get(ticker, np.nan)
            mkt_cap = (price * shares) if (price > 0 and shares > 0) else np.nan

            # Forward earnings yield = eps_forward / price
            eps_fwd = estimates["eps_forward"].get(ticker, np.nan)
            if price > 0 and not np.isnan(eps_fwd):
                r["fwd_earnings_yield"] = eps_fwd / price

            # Book-to-price = 1 / price_to_book
            ptb = metrics["price_to_book"].get(ticker, np.nan) if ticker in metrics.index else np.nan
            if ptb and ptb > 0:
                r["book_to_price"] = 1.0 / ptb

            # FCF yield (already computed in fundamentals)
            fcf_y = fund["fcf_yield"].get(ticker, np.nan)
            if not np.isnan(fcf_y):
                r["fcf_yield"] = fcf_y

            # EV/EBITDA inverted
            ev_ebitda = metrics["enterprise_to_ebitda"].get(ticker, np.nan) if ticker in metrics.index else np.nan
            if ev_ebitda and ev_ebitda > 0:
                r["ev_ebitda_inv"] = 1.0 / ev_ebitda

            # Shareholder yield = (buybacks + dividends) / market_cap
            buybacks  = fund["buybacks"].get(ticker, np.nan)
            dividends = fund["dividends_paid"].get(ticker, np.nan)
            if not np.isnan(mkt_cap) and mkt_cap > 0:
                total_return = (abs(buybacks) if not np.isnan(buybacks) else 0) + \
                               (abs(dividends) if not np.isnan(dividends) else 0)
                r["shareholder_yield"] = total_return / mkt_cap

            # Sales-to-EV = total_revenue / enterprise_value
            ev      = metrics["enterprise_value"].get(ticker, np.nan) if ticker in metrics.index else np.nan
            revenue = metrics["total_revenue"].get(ticker, np.nan) if ticker in metrics.index else np.nan
            if ev and ev > 0 and not np.isnan(revenue):
                r["sales_to_ev"] = revenue / ev

            rows.append(r)

        return pd.DataFrame(rows)
