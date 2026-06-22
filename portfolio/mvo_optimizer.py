"""
Mean-Variance Optimizer (Markowitz) via scipy SLSQP.

Objective: maximize  mu_net @ w  -  lambda * w @ Sigma @ w
Subject to:
  - sum of long weights  == target_long_gross
  - sum of short weights == -target_short_gross
  - per-position: |w_i| in [min_pct, max_pct]
  - net portfolio beta:  |sum(w_i * beta_i)| <= max_beta
  - sector net:          |sector_long - sector_short| <= max_sector_net_pct
  - single-side sector:  sector_one_side <= max_sector_one_side_pct

On non-convergence: logs warning and returns None (caller falls back to conviction-tilt).
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml
from scipy.optimize import minimize, OptimizeResult

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_P = _CFG["portfolio"]


def _build_covariance(
    tickers: list[str],
    conn: sqlite3.Connection,
    lookback: int,
) -> np.ndarray:
    """120-day historical covariance matrix (annualised). Falls back to diagonal."""
    from data.db import get_connection
    placeholders = ",".join("?" * len(tickers))
    prices = pd.read_sql(
        f"""SELECT ticker, date, close FROM daily_prices
            WHERE ticker IN ({placeholders})
              AND date >= date('now', '-{lookback + 20} days')
            ORDER BY date""",
        conn,
        params=tickers,
    )
    if prices.empty:
        return np.eye(len(tickers)) * 0.04  # 20% vol diagonal fallback

    pivot   = prices.pivot(index="date", columns="ticker", values="close")
    returns = pivot.pct_change().dropna(how="all").tail(lookback)

    # Align columns to tickers order
    available = [t for t in tickers if t in returns.columns]
    if len(available) < 2:
        return np.eye(len(tickers)) * 0.04

    ret_matrix = returns[available].dropna()
    Sigma_sub  = ret_matrix.cov().values * 252  # annualise

    # Expand back to full tickers x tickers (missing → 4% variance on diagonal)
    n = len(tickers)
    Sigma = np.eye(n) * 0.04
    idx_map = {t: i for i, t in enumerate(available)}
    for i, ti in enumerate(tickers):
        for j, tj in enumerate(tickers):
            if ti in idx_map and tj in idx_map:
                Sigma[i, j] = Sigma_sub[idx_map[ti], idx_map[tj]]

    return Sigma


def run_mvo(
    long_tickers: list[str],
    short_tickers: list[str],
    scores: dict[str, float],
    betas: dict[str, float],
    sectors: dict[str, str],
    tc_drag: dict[str, float],
    conn: sqlite3.Connection,
    lambda_ra: float = _P["mvo_risk_aversion"],
    return_scale: float = _P["mvo_return_scale"],
) -> Optional[dict[str, float]]:
    """
    Run MVO and return {ticker: signed_weight} or None if non-convergent.
    Positive weights = long, negative = short.
    """
    all_tickers = long_tickers + short_tickers
    n_long  = len(long_tickers)
    n_short = len(short_tickers)
    n       = n_long + n_short

    if n == 0:
        return {}

    # Expected returns: linear map from score to annualised return
    def score_to_return(score: float) -> float:
        return (score / 100.0 * 2.0 - 1.0) * return_scale

    mu_gross = np.array([score_to_return(scores.get(t, 50.0)) for t in all_tickers])
    tc_array = np.array([tc_drag.get(t, 0.0) for t in all_tickers])
    mu_net   = mu_gross - tc_array  # net-of-cost expected returns

    # Covariance matrix
    Sigma = _build_covariance(all_tickers, conn, _P["cov_lookback_days"])

    beta_arr   = np.array([betas.get(t, 1.0) for t in all_tickers])
    sector_arr = [sectors.get(t, "Unknown") for t in all_tickers]
    unique_sectors = list(set(sector_arr))

    # Initial guess: equal weight within each book
    w0 = np.zeros(n)
    if n_long  > 0: w0[:n_long]  =  _P["target_long_gross"]  / n_long
    if n_short > 0: w0[n_long:]  = -_P["target_short_gross"] / n_short

    # Bounds: longs [min, max], shorts [-max, -min]
    bounds = (
        [(_P["min_position_pct"], _P["max_position_pct"])] * n_long
        + [(-_P["max_position_pct"], -_P["min_position_pct"])] * n_short
    )

    # Constraints
    constraints = []

    # 1. Long gross == target
    if n_long > 0:
        long_mask = np.zeros(n); long_mask[:n_long] = 1.0
        constraints.append({
            "type": "eq",
            "fun": lambda w, m=long_mask: np.dot(m, w) - _P["target_long_gross"],
        })

    # 2. Short gross == -target
    if n_short > 0:
        short_mask = np.zeros(n); short_mask[n_long:] = 1.0
        constraints.append({
            "type": "eq",
            "fun": lambda w, m=short_mask: np.dot(m, w) + _P["target_short_gross"],
        })

    # 3. Net beta <= max_beta
    constraints.append({
        "type": "ineq",
        "fun": lambda w: _P["max_beta"] - np.dot(w, beta_arr),
    })
    constraints.append({
        "type": "ineq",
        "fun": lambda w: _P["max_beta"] + np.dot(w, beta_arr),
    })

    # 4. Sector constraints
    for sec in unique_sectors:
        sec_mask = np.array([1.0 if s == sec else 0.0 for s in sector_arr])
        # Sector net <= max_sector_net_pct
        constraints.append({
            "type": "ineq",
            "fun": lambda w, m=sec_mask: _P["max_sector_net_pct"] - np.dot(m, w),
        })
        constraints.append({
            "type": "ineq",
            "fun": lambda w, m=sec_mask: _P["max_sector_net_pct"] + np.dot(m, w),
        })
        # Long-side sector <= max_sector_one_side_pct
        long_sec_mask = np.array([1.0 if (s == sec and i < n_long) else 0.0
                                  for i, s in enumerate(sector_arr)])
        constraints.append({
            "type": "ineq",
            "fun": lambda w, m=long_sec_mask: _P["max_sector_one_side_pct"] - np.dot(m, w),
        })
        # Short-side sector <= max_sector_one_side_pct (in absolute terms)
        short_sec_mask = np.array([1.0 if (s == sec and i >= n_long) else 0.0
                                   for i, s in enumerate(sector_arr)])
        constraints.append({
            "type": "ineq",
            "fun": lambda w, m=short_sec_mask: _P["max_sector_one_side_pct"] + np.dot(m, w),
        })

    def neg_objective(w: np.ndarray) -> float:
        return -(mu_net @ w - lambda_ra * w @ Sigma @ w)

    def grad_neg_objective(w: np.ndarray) -> np.ndarray:
        return -(mu_net - 2 * lambda_ra * Sigma @ w)

    result: OptimizeResult = minimize(
        neg_objective,
        x0=w0,
        jac=grad_neg_objective,
        method="SLSQP",
        bounds=bounds,
        constraints=constraints,
        options={"ftol": 1e-9, "maxiter": 1000, "disp": False},
    )

    if not result.success:
        logger.warning(f"MVO did not converge: {result.message} — falling back to conviction-tilt")
        return None

    weights = {t: float(result.x[i]) for i, t in enumerate(all_tickers)}
    logger.info(
        f"MVO converged: obj={-result.fun:.4f} "
        f"long_gross={sum(v for v in weights.values() if v>0):.3f} "
        f"short_gross={abs(sum(v for v in weights.values() if v<0)):.3f}"
    )
    return weights
