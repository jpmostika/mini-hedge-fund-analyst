"""
Correlation Monitor — 60-day rolling pairwise correlations within each book.
Alerts if average within-book correlation > 0.60.
Computes Effective Number of Bets (ENB) = exp(entropy of eigenvalue distribution).
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml
from scipy.stats import entropy as scipy_entropy

from data.db import get_connection

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_R      = _CFG["risk"]
ALERT   = _R["within_book_corr_alert"]
WINDOW  = 60


def _effective_number_of_bets(corr_matrix: np.ndarray) -> float:
    """
    ENB = exp(H) where H is the entropy of the eigenvalue distribution.
    ENB = n means fully uncorrelated. ENB = 1 means fully correlated (one bet).
    """
    eigenvalues = np.linalg.eigvalsh(corr_matrix)
    eigenvalues = np.maximum(eigenvalues, 0)  # clip numerical negatives
    total = eigenvalues.sum()
    if total <= 0:
        return 1.0
    probs = eigenvalues / total
    probs = probs[probs > 1e-10]
    h = float(scipy_entropy(probs))
    return float(np.exp(h))


def monitor_correlations(
    weights: dict[str, float],
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """
    Compute pairwise correlations within long book and short book.
    Returns alerts and ENB for each book.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        long_tickers  = [t for t, w in weights.items() if w > 0]
        short_tickers = [t for t, w in weights.items() if w < 0]

        results = {"long": {}, "short": {}, "alerts": []}

        for book_name, tickers in [("long", long_tickers), ("short", short_tickers)]:
            if len(tickers) < 2:
                results[book_name] = {"enb": float(len(tickers)), "avg_corr": 0.0}
                continue

            placeholders = ",".join("?" * len(tickers))
            prices = pd.read_sql(
                f"""SELECT ticker, date, close FROM daily_prices
                    WHERE ticker IN ({placeholders})
                      AND date >= date('now', '-{WINDOW + 5} days')
                    ORDER BY date""",
                conn,
                params=tickers,
            )
            if prices.empty:
                continue

            pivot   = prices.pivot(index="date", columns="ticker", values="close")
            returns = pivot.pct_change().dropna(how="all").tail(WINDOW)

            # Keep only tickers with sufficient data
            good_tickers = returns.columns[returns.notna().sum() >= 30].tolist()
            if len(good_tickers) < 2:
                continue

            ret_clean = returns[good_tickers].dropna()
            if len(ret_clean) < 20:
                continue

            corr_matrix = ret_clean.corr().values
            n = len(good_tickers)

            # Average off-diagonal correlation
            off_diag = corr_matrix[np.triu_indices(n, k=1)]
            avg_corr = float(np.mean(np.abs(off_diag)))

            # ENB
            enb = _effective_number_of_bets(corr_matrix)

            results[book_name] = {
                "avg_corr":   round(avg_corr, 3),
                "enb":        round(enb, 1),
                "n_positions": len(good_tickers),
                "max_corr":   round(float(np.max(off_diag)), 3),
            }

            if avg_corr > ALERT:
                alert_msg = (
                    f"{book_name.upper()} book: avg pairwise correlation "
                    f"{avg_corr:.2f} > {ALERT} (ENB={enb:.1f} of {len(good_tickers)} positions)"
                )
                results["alerts"].append({
                    "book":     book_name,
                    "avg_corr": avg_corr,
                    "enb":      enb,
                    "message":  alert_msg,
                })
                logger.warning(f"[correlation_monitor] {alert_msg}")
            else:
                logger.info(
                    f"[correlation_monitor] {book_name}: avg_corr={avg_corr:.2f} ENB={enb:.1f}"
                )

        return results

    finally:
        if close_conn:
            conn.close()
