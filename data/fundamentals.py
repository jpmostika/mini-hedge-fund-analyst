"""
Fundamentals ingestion — quarterly + annual financials with 24 derived ratios.
Sources: yfinance (default) or FMP (if FMP_API_KEY set).
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml
from tqdm import tqdm

from data.db import get_connection, init_db
from data.providers import get_providers
from data.universe import get_equity_tickers

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

TABLE = _CFG["fundamentals"]["table"]


def _safe_div(a, b, default=np.nan):
    try:
        if b is None or b == 0 or pd.isna(b):
            return default
        return a / b
    except Exception:
        return default


def _pct_change(current, prior):
    if current is None or prior is None or prior == 0:
        return np.nan
    try:
        return (current - prior) / abs(prior)
    except Exception:
        return np.nan


def _extract_value(df: Optional[pd.DataFrame], *col_names, row: int = 0) -> Optional[float]:
    """Try multiple column name variants; return the first match from the given row
    (row=0 is the most recent period, row=1 the prior period, etc.)."""
    if df is None or df.empty or row < 0 or row >= len(df):
        return None
    for col in col_names:
        matches = [c for c in df.columns if col.lower() in c.lower()]
        if matches:
            try:
                val = df.iloc[row][matches[0]]
                return float(val) if not pd.isna(val) else None
            except Exception:
                continue
    return None


def _compute_ratios(data: dict, period_type: str, period_idx: int = 0) -> dict:
    """Compute all 24 ratios for a single period (period_idx=0 is most recent).

    Growth is measured against the next-older period (QoQ / prior-year for annual)
    and, for quarterly data, four periods back (YoY).
    """
    if period_type == "quarterly":
        income = data.get("income_q")
        balance = data.get("balance_q")
        cashflow = data.get("cashflow_q")
        qoq_prior_idx = period_idx + 1
        yoy_prior_idx = period_idx + 4
    else:
        income = data.get("income_a")
        balance = data.get("balance_a")
        cashflow = data.get("cashflow_a")
        qoq_prior_idx = period_idx + 1  # prior fiscal year for annual data
        yoy_prior_idx = None

    def ev(df, *cols):
        return _extract_value(df, *cols, row=period_idx)

    revenue = ev(income, "Total Revenue", "Revenue", "totalRevenue")
    net_income = ev(income, "Net Income", "netIncome")
    gross_profit = ev(income, "Gross Profit", "grossProfit")
    operating_income = ev(income, "Operating Income", "operatingIncome", "EBIT")
    ebit = ev(income, "EBIT", "Operating Income", "operatingIncome")
    rd = ev(income, "Research And Development", "researchAndDevelopment", "R&D")
    interest_expense = ev(income, "Interest Expense", "interestExpense")

    total_assets = ev(balance, "Total Assets", "totalAssets")
    total_equity = ev(balance, "Total Stockholder Equity", "Stockholders Equity", "totalStockholdersEquity")
    total_debt = ev(balance, "Total Debt", "Long Term Debt", "totalDebt")
    total_liabilities = ev(balance, "Total Liabilities Net Minority Interest", "Total Liabilities", "totalLiabilities")
    current_assets = ev(balance, "Current Assets", "totalCurrentAssets")
    current_liabilities = ev(balance, "Current Liabilities", "totalCurrentLiabilities")
    accounts_receivable = ev(balance, "Accounts Receivable", "Net Receivables", "accountsReceivable")
    retained = ev(balance, "Retained Earnings", "retainedEarnings")
    shares_out = ev(balance, "Ordinary Shares Number", "Common Stock Shares Outstanding", "sharesOutstanding")

    cfo = ev(cashflow, "Operating Cash Flow", "Cash From Operations", "operatingCashflow")
    capex = ev(cashflow, "Capital Expenditure", "capitalExpenditures")
    dividends = ev(cashflow, "Common Stock Dividend Paid", "Dividends Paid", "dividendsPaid")
    buybacks = ev(cashflow, "Repurchase Of Capital Stock", "Common Stock Repurchased", "repurchaseOfStock")

    # Market cap proxy from info (yfinance)
    market_cap = data.get("info", {}).get("marketCap") if data.get("source") == "yfinance" else None

    # --- Compute the 24 ratios --- #
    roe = _safe_div(net_income, total_equity)
    roa = _safe_div(net_income, total_assets)
    gross_margin = _safe_div(gross_profit, revenue)
    operating_margin = _safe_div(operating_income, revenue)
    net_margin = _safe_div(net_income, revenue)

    # Growth (QoQ and YoY) — read priors at fixed offsets from the current period
    prior_revenue = _extract_value(income, "Total Revenue", "Revenue", "totalRevenue", row=qoq_prior_idx)
    prior_net_income = _extract_value(income, "Net Income", "netIncome", row=qoq_prior_idx)
    if yoy_prior_idx is not None:
        prior_year_revenue = _extract_value(income, "Total Revenue", "Revenue", row=yoy_prior_idx)
        prior_year_net_income = _extract_value(income, "Net Income", row=yoy_prior_idx)
    else:
        prior_year_revenue = None
        prior_year_net_income = None

    revenue_growth_qoq = _pct_change(revenue, prior_revenue)
    revenue_growth_yoy = _pct_change(revenue, prior_year_revenue)
    earnings_growth_qoq = _pct_change(net_income, prior_net_income)
    earnings_growth_yoy = _pct_change(net_income, prior_year_net_income)

    debt_to_equity = _safe_div(total_debt, total_equity)

    # FCF yield requires market cap
    fcf = (cfo or 0) + (capex or 0)  # capex is usually negative in statements
    fcf_yield = _safe_div(fcf, market_cap) if market_cap else np.nan

    current_ratio = _safe_div(current_assets, current_liabilities)
    ar_to_revenue = _safe_div(accounts_receivable, revenue)
    cfo_to_ni = _safe_div(cfo, net_income)

    # Accruals ratio = (net_income - cfo) / avg_assets
    accruals_ratio = _safe_div((net_income or 0) - (cfo or 0), total_assets)

    working_capital = (current_assets or 0) - (current_liabilities or 0) if current_assets and current_liabilities else np.nan
    asset_turnover = _safe_div(revenue, total_assets)

    return {
        "roe": roe,
        "roa": roa,
        "gross_margin": gross_margin,
        "operating_margin": operating_margin,
        "net_margin": net_margin,
        "revenue_growth_yoy": revenue_growth_yoy,
        "revenue_growth_qoq": revenue_growth_qoq,
        "earnings_growth_yoy": earnings_growth_yoy,
        "earnings_growth_qoq": earnings_growth_qoq,
        "debt_to_equity": debt_to_equity,
        "fcf_yield": fcf_yield,
        "current_ratio": current_ratio,
        "ar_to_revenue": ar_to_revenue,
        "cfo_to_ni": cfo_to_ni,
        "accruals_ratio": accruals_ratio,
        "retained_earnings": retained,
        "working_capital": working_capital,
        "total_liabilities": total_liabilities,
        "ebit": ebit,
        "rd_expense": rd,
        "shares_outstanding": shares_out,
        "dividends_paid": dividends,
        "buybacks": buybacks,
        "asset_turnover": asset_turnover,
    }


def _get_period_end(data: dict, period_type: str, period_idx: int = 0) -> Optional[str]:
    key = "income_q" if period_type == "quarterly" else "income_a"
    df = data.get(key)
    if df is None or df.empty or period_idx >= len(df):
        return None
    try:
        idx = df.index[period_idx]
        if hasattr(idx, "date"):
            return str(idx.date())
        return str(idx)[:10]
    except Exception:
        return None


def _upsert_fundamentals(conn: sqlite3.Connection, ticker: str, ratios: dict, period_end: str, period_type: str, source: str):
    now = datetime.utcnow().isoformat()
    record = {
        "ticker": ticker,
        "period_end": period_end,
        "period_type": period_type,
        "source": source,
        "last_updated": now,
        **{k: (float(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None) for k, v in ratios.items()},
    }
    cols = ", ".join(record.keys())
    placeholders = ", ".join(f":{k}" for k in record.keys())
    conn.execute(
        f"INSERT OR REPLACE INTO {TABLE} ({cols}) VALUES ({placeholders})",
        record,
    )
    conn.commit()


def refresh_fundamentals(tickers: Optional[list[str]] = None) -> dict:
    init_db()
    conn = get_connection()
    providers = get_providers()

    if tickers is None:
        tickers = get_equity_tickers(conn)

    processed = 0
    errors = 0

    for ticker in tqdm(tickers, desc="Fundamentals"):
        try:
            data = providers.get_fundamentals(ticker)
            source = data.get("source", "yfinance")

            for period_type in ("quarterly", "annual"):
                stmt_key = "income_q" if period_type == "quarterly" else "income_a"
                stmt = data.get(stmt_key)
                n_periods = len(stmt) if stmt is not None and not stmt.empty else 0
                max_periods = 8 if period_type == "quarterly" else 5

                for period_idx in range(min(n_periods, max_periods)):
                    period_end = _get_period_end(data, period_type, period_idx)
                    if period_end is None:
                        continue

                    # Skip if already stored for this period
                    cur = conn.execute(
                        f"SELECT 1 FROM {TABLE} WHERE ticker=? AND period_end=? AND period_type=?",
                        (ticker, period_end, period_type),
                    )
                    if cur.fetchone():
                        continue

                    ratios = _compute_ratios(data, period_type, period_idx)
                    _upsert_fundamentals(conn, ticker, ratios, period_end, period_type, source)

            processed += 1
        except Exception as e:
            logger.error(f"Fundamentals failed for {ticker}: {e}")
            errors += 1

    conn.close()
    logger.info(f"Fundamentals refresh complete — {processed} tickers processed, {errors} errors")
    return {"processed": processed, "errors": errors}
