"""
Quality factor — 8 sub-factors including Piotroski F-Score and Altman Z-Score.

Sub-factors:
  roe_stability       std dev of 12Q ROEs (inverted — lower vol = higher quality)
  gross_margin        gross margin level
  gross_margin_trend  gross margin now minus 4Q ago
  debt_equity_inv     1 / debt-to-equity (lower leverage = better)
  cfo_to_ni           CFO / net income (> 1 = real earnings)
  accruals_inv        1 - accruals_ratio (lower accruals = better quality)
  piotroski           F-Score 1–9 (9 binary signals)
  altman_z            Altman Z-Score (financial distress predictor)
"""

import logging
import sqlite3

import numpy as np
import pandas as pd

from factors.base import BaseFactor
from factors.loader import load_market_metrics

logger = logging.getLogger(__name__)

_PIOTROSKI_MIN = 7   # green zone
_PIOTROSKI_WARN = 3  # amber zone


class QualityFactor(BaseFactor):
    name = "quality"
    sub_factors = [
        "roe_stability", "gross_margin", "gross_margin_trend",
        "debt_equity_inv", "cfo_to_ni", "accruals_inv",
        "piotroski", "altman_z",
    ]
    higher_is_better = {
        "roe_stability":      True,   # after inversion, higher = more stable
        "gross_margin":       True,
        "gross_margin_trend": True,
        "debt_equity_inv":    True,
        "cfo_to_ni":          True,
        "accruals_inv":       True,
        "piotroski":          True,
        "altman_z":           True,
    }

    def load_raw(self, universe: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
        # All quarterly fundamentals for last 12 periods
        fund_all = pd.read_sql(
            """SELECT ticker, period_end, roe, gross_margin, debt_to_equity,
                      cfo_to_ni, accruals_ratio, current_ratio, shares_outstanding,
                      asset_turnover, roa, working_capital, retained_earnings,
                      total_liabilities, ebit
               FROM fundamentals
               WHERE period_type='quarterly'
               ORDER BY ticker, period_end DESC""",
            conn,
        )

        # Market metrics for Altman Z market cap component
        metrics = load_market_metrics(conn, tickers=universe["ticker"].tolist())

        rows = []
        for _, urow in universe.iterrows():
            ticker = urow["ticker"]
            sector = urow["sector"]
            r: dict = {"ticker": ticker, "sector": sector}

            tf = fund_all[fund_all["ticker"] == ticker].sort_values("period_end", ascending=False)
            if tf.empty:
                rows.append(r)
                continue

            latest = tf.iloc[0]

            # --- ROE stability (12Q std dev, inverted) ---
            roes = tf["roe"].dropna().head(12)
            if len(roes) >= 4:
                r["roe_stability"] = -roes.std()   # negative so lower vol = higher score

            # --- Gross margin level ---
            gm = latest["gross_margin"]
            if not np.isnan(gm):
                r["gross_margin"] = gm

            # --- Gross margin trend (latest minus 4Q ago) ---
            if len(tf) >= 5:
                gm_4q = tf.iloc[4]["gross_margin"]
                if not np.isnan(gm) and not np.isnan(gm_4q):
                    r["gross_margin_trend"] = gm - gm_4q

            # --- Debt/equity inverted ---
            de = latest["debt_to_equity"]
            if not np.isnan(de) and de > 0:
                r["debt_equity_inv"] = 1.0 / de
            elif not np.isnan(de) and de <= 0:
                r["debt_equity_inv"] = 10.0   # no debt → high quality

            # --- CFO/NI ---
            cfo_ni = latest["cfo_to_ni"]
            if not np.isnan(cfo_ni):
                r["cfo_to_ni"] = cfo_ni

            # --- Accruals inverted ---
            acc = latest["accruals_ratio"]
            if not np.isnan(acc):
                r["accruals_inv"] = -acc   # lower accruals → higher quality

            # --- Piotroski F-Score ---
            r["piotroski"] = _compute_piotroski(tf)

            # --- Altman Z-Score ---
            mkt_cap = metrics["market_cap"].get(ticker, np.nan) if ticker in metrics.index else np.nan
            total_assets = metrics["total_assets"].get(ticker, np.nan) if ticker in metrics.index else np.nan
            revenue = metrics["total_revenue"].get(ticker, np.nan) if ticker in metrics.index else np.nan
            az = _compute_altman_z(latest, mkt_cap, total_assets, revenue)
            if az is not None:
                r["altman_z"] = az

            rows.append(r)

        return pd.DataFrame(rows)


def _compute_piotroski(tf: pd.DataFrame) -> float:
    """
    9 binary signals → F-Score 0–9.
    Needs at least 2 periods for trend signals.
    """
    if tf.empty:
        return np.nan

    latest = tf.iloc[0]
    prior  = tf.iloc[1] if len(tf) > 1 else None

    score = 0

    # 1. Positive ROA
    roa = latest.get("roa", np.nan)
    if not np.isnan(roa) and roa > 0:
        score += 1

    # 2. Positive CFO (approximated via accruals: accruals < 0 means CFO > NI > 0)
    acc = latest.get("accruals_ratio", np.nan)
    cfo_ni = latest.get("cfo_to_ni", np.nan)
    if not np.isnan(cfo_ni) and cfo_ni > 0 and not np.isnan(roa) and roa > 0:
        score += 1   # CFO > 0 when cfo_to_ni > 0 and net income > 0

    # 3. Rising ROA
    if prior is not None:
        roa_p = prior.get("roa", np.nan)
        if not np.isnan(roa) and not np.isnan(roa_p) and roa > roa_p:
            score += 1

    # 4. CFO > NI (accrual quality)
    if not np.isnan(cfo_ni) and cfo_ni > 1.0:
        score += 1

    # 5. Falling D/E
    if prior is not None:
        de = latest.get("debt_to_equity", np.nan)
        de_p = prior.get("debt_to_equity", np.nan)
        if not np.isnan(de) and not np.isnan(de_p) and de < de_p:
            score += 1

    # 6. Rising current ratio
    if prior is not None:
        cr = latest.get("current_ratio", np.nan)
        cr_p = prior.get("current_ratio", np.nan)
        if not np.isnan(cr) and not np.isnan(cr_p) and cr > cr_p:
            score += 1

    # 7. No dilution (shares outstanding not increased)
    if prior is not None:
        so = latest.get("shares_outstanding", np.nan)
        so_p = prior.get("shares_outstanding", np.nan)
        if not np.isnan(so) and not np.isnan(so_p) and so <= so_p:
            score += 1

    # 8. Rising gross margin
    if prior is not None:
        gm = latest.get("gross_margin", np.nan)
        gm_p = prior.get("gross_margin", np.nan)
        if not np.isnan(gm) and not np.isnan(gm_p) and gm > gm_p:
            score += 1

    # 9. Rising asset turnover
    if prior is not None:
        at_ = latest.get("asset_turnover", np.nan)
        at_p = prior.get("asset_turnover", np.nan)
        if not np.isnan(at_) and not np.isnan(at_p) and at_ > at_p:
            score += 1

    return float(score)


def _to_float(v) -> float:
    """Convert None/NA/string safely to float, returning nan on failure."""
    try:
        return float(v) if v is not None else np.nan
    except (TypeError, ValueError):
        return np.nan


def _compute_altman_z(
    latest: pd.Series,
    market_cap,
    total_assets,
    revenue,
) -> float:
    """
    Z = 1.2*(WC/TA) + 1.4*(RE/TA) + 3.3*(EBIT/TA) + 0.6*(MktCap/TL) + 1.0*(Sales/TA)
    Returns None if insufficient data.
    """
    wc   = _to_float(latest.get("working_capital"))
    re   = _to_float(latest.get("retained_earnings"))
    ebit = _to_float(latest.get("ebit"))
    tl   = _to_float(latest.get("total_liabilities"))
    total_assets = _to_float(total_assets)
    market_cap   = _to_float(market_cap)
    revenue      = _to_float(revenue)

    if any(np.isnan(v) for v in [wc, re, ebit, tl]) or np.isnan(total_assets) or total_assets <= 0:
        return None

    ta = total_assets
    z  = 1.2 * (wc / ta)
    z += 1.4 * (re / ta)
    z += 3.3 * (ebit / ta)

    if not np.isnan(market_cap) and market_cap > 0 and tl > 0:
        z += 0.6 * (market_cap / tl)

    if not np.isnan(revenue) and revenue > 0:
        z += 1.0 * (revenue / ta)

    return z
