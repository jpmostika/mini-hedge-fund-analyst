"""
Barra-Style Factor Risk Model.

Algorithm (for each day t in 120-day lookback):
  1. Load cross-sectional factor exposures F_k,i (z-scored sector-relative ranks 0-100)
  2. Run OLS: r_i,t = alpha_t + sum_k beta_k,t * F_k,i + epsilon_i,t
  3. Record factor returns beta_k,t and specific returns epsilon_i,t

Output:
  - Factor covariance matrix (annualized)
  - Specific variance per stock (annualized)
  - Predicted covariance: Sigma = X*F_cov*X' + diag(spec_var)
  - Portfolio variance decomposition: factor_var + specific_var
  - MCTR per position: w_i * cov(r_i, r_p) / sigma_p
  - Flag MCTR > 1.5x weight%
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml
from numpy.linalg import lstsq

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_RISK_CFG  = _CFG["risk"]
LOOKBACK   = _RISK_CFG["factor_lookback_days"]
MCTR_FLAG  = _RISK_CFG["mctr_concentration_threshold"]

FACTOR_NAMES = [
    "momentum", "value", "quality", "growth",
    "revisions", "short_interest", "insider", "institutional",
]


def _load_factor_scores(conn: sqlite3.Connection) -> pd.DataFrame:
    """Latest factor scores for all scored tickers."""
    return pd.read_sql(
        """SELECT fs.ticker, fs.sector, fs.momentum, fs.value, fs.quality,
                  fs.growth, fs.revisions, fs.short_interest, fs.insider,
                  fs.institutional
           FROM factor_scores fs
           INNER JOIN (
               SELECT ticker, MAX(date) AS md FROM factor_scores GROUP BY ticker
           ) lp ON fs.ticker=lp.ticker AND fs.date=lp.md""",
        conn,
    ).set_index("ticker")


def _load_returns(tickers: list[str], conn: sqlite3.Connection) -> pd.DataFrame:
    """Daily returns for tickers over the lookback window."""
    placeholders = ",".join("?" * len(tickers))
    prices = pd.read_sql(
        f"""SELECT ticker, date, close FROM daily_prices
            WHERE ticker IN ({placeholders})
              AND date >= date('now', '-{LOOKBACK + 20} days')
            ORDER BY date""",
        conn,
        params=tickers,
    )
    if prices.empty:
        return pd.DataFrame()
    pivot = prices.pivot(index="date", columns="ticker", values="close")
    returns = pivot.pct_change().dropna(how="all").tail(LOOKBACK)
    return returns


def _z_score(series: pd.Series) -> pd.Series:
    """Cross-sectional z-score, robust to NaN."""
    mu  = series.mean()
    std = series.std()
    return (series - mu) / std if std > 0 else series * 0


def run_factor_model(
    conn: sqlite3.Connection,
    weights: Optional[dict[str, float]] = None,
) -> dict:
    """
    Run the full Barra-style factor risk model.

    weights: {ticker: signed_weight} for portfolio decomposition.
             If None, only model parameters are returned.
    """
    scores_df = _load_factor_scores(conn)
    if scores_df.empty:
        logger.warning("No factor scores available for risk model")
        return {}

    tickers = scores_df.index.tolist()
    returns = _load_returns(tickers, conn)

    if returns.empty or len(returns) < 20:
        logger.warning(f"Insufficient return history ({len(returns)} days) for factor risk model")
        return {}

    # ── Factor exposure matrix X (n_stocks x n_factors) ─────────────── #
    available_factors = [f for f in FACTOR_NAMES if f in scores_df.columns]
    X_raw = scores_df[available_factors].copy()

    # Z-score each factor cross-sectionally (convert 0-100 ranks to z-scores)
    X = X_raw.apply(_z_score, axis=0)

    # Align tickers present in both returns and factor scores
    common = [t for t in returns.columns if t in X.index and not X.loc[t].isna().all()]
    if len(common) < 10:
        logger.warning(f"Only {len(common)} tickers in common — risk model unreliable")
        return {}

    X_aligned = X.loc[common].fillna(0).values          # (n, k)
    X_const   = np.hstack([np.ones((len(common), 1)), X_aligned])  # add intercept

    # ── Cross-sectional regression for each day ──────────────────────── #
    factor_returns_list = []
    specific_returns: dict[str, list] = {t: [] for t in common}

    for date in returns.index:
        r_t = returns.loc[date, common].values  # (n,)
        valid = ~np.isnan(r_t)
        if valid.sum() < max(10, len(available_factors) + 2):
            continue

        X_t = X_const[valid]
        r_t_valid = r_t[valid]

        # OLS: r = X @ beta + eps
        coeffs, _, _, _ = lstsq(X_t, r_t_valid, rcond=None)
        fitted = X_t @ coeffs
        residuals = r_t_valid - fitted

        factor_returns_list.append(coeffs[1:])  # exclude intercept

        # Store specific returns (NaN for missing)
        valid_idx = np.where(valid)[0]
        eps_full = np.full(len(common), np.nan)
        eps_full[valid_idx] = residuals
        for i, ticker in enumerate(common):
            specific_returns[ticker].append(eps_full[i])

    if len(factor_returns_list) < 20:
        logger.warning("Too few regression days for reliable covariance estimation")
        return {}

    # ── Factor covariance matrix (annualized) ────────────────────────── #
    F_returns = pd.DataFrame(factor_returns_list, columns=available_factors)
    F_cov     = F_returns.cov().values * 252          # (k x k), annualized

    # ── Specific variance per stock (annualized) ─────────────────────── #
    spec_var = {}
    for ticker in common:
        eps = [x for x in specific_returns[ticker] if not np.isnan(x)]
        spec_var[ticker] = np.var(eps) * 252 if len(eps) > 5 else 0.04

    # ── Predicted covariance: Sigma = X*F*X' + diag(spec_var) ────────── #
    X_mat    = X.loc[common].fillna(0).values         # (n x k)
    spec_arr = np.array([spec_var.get(t, 0.04) for t in common])
    Sigma    = X_mat @ F_cov @ X_mat.T + np.diag(spec_arr)

    result = {
        "tickers":      common,
        "factor_names": available_factors,
        "factor_cov":   F_cov,
        "spec_var":     spec_var,
        "predicted_cov": Sigma,
        "X":            X_mat,
        "n_days":       len(factor_returns_list),
    }

    # ── Portfolio decomposition ──────────────────────────────────────── #
    if weights:
        w = np.array([weights.get(t, 0.0) for t in common])
        w_factor = X_mat.T @ w                          # factor exposures of portfolio

        factor_var   = float(w_factor @ F_cov @ w_factor)
        specific_var = float(w @ np.diag(spec_arr) @ w)
        total_var    = factor_var + specific_var
        sigma_p      = np.sqrt(max(total_var, 1e-10))

        # MCTR: w_i * (Sigma @ w)_i / sigma_p
        Sigma_w = Sigma @ w
        mctr    = {t: float(w[i] * Sigma_w[i] / sigma_p) for i, t in enumerate(common)}

        # Flag concentration: MCTR > MCTR_FLAG * |weight|
        flags = []
        for t, m in mctr.items():
            wt = abs(weights.get(t, 0.0))
            if wt > 0 and abs(m) > MCTR_FLAG * wt:
                flags.append({
                    "ticker": t,
                    "mctr":   round(m * 100, 3),
                    "weight_pct": round(wt * 100, 2),
                    "ratio":  round(abs(m) / wt, 2),
                })

        result.update({
            "portfolio_factor_var":   round(factor_var * 100, 4),
            "portfolio_specific_var": round(specific_var * 100, 4),
            "portfolio_total_var":    round(total_var * 100, 4),
            "portfolio_vol_pct":      round(sigma_p * 100, 2),
            "mctr":                   {t: round(v * 100, 4) for t, v in mctr.items()},
            "mctr_flags":             flags,
        })

        logger.info(
            f"Factor risk model: vol={sigma_p*100:.1f}% "
            f"(factor={factor_var**0.5*100:.1f}% + idio={specific_var**0.5*100:.1f}%) "
            f"| MCTR flags: {len(flags)}"
        )

    return result
