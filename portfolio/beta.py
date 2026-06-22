"""
Beta Calculator — rolling 60-day beta vs SPY for each stock.
Also computes portfolio-level long book beta, short book beta, and net beta.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from data.db import get_connection

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_PORT_CFG  = _CFG["portfolio"]
LOOKBACK   = _PORT_CFG["beta_lookback_days"]
SPY_TICKER = _PORT_CFG["spy_ticker"]


def _load_returns(tickers: list[str], conn: sqlite3.Connection) -> pd.DataFrame:
    """Load daily close prices and compute returns for given tickers + SPY."""
    all_tickers = list(set(tickers + [SPY_TICKER]))
    placeholders = ",".join("?" * len(all_tickers))
    prices = pd.read_sql(
        f"""SELECT ticker, date, close FROM daily_prices
            WHERE ticker IN ({placeholders})
              AND date >= date('now', '-{LOOKBACK + 10} days')
            ORDER BY date""",
        conn,
        params=all_tickers,
    )
    if prices.empty:
        return pd.DataFrame()

    pivot = prices.pivot(index="date", columns="ticker", values="close")
    returns = pivot.pct_change().dropna(how="all").tail(LOOKBACK)
    return returns


def compute_betas(
    tickers: list[str],
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, float]:
    """
    Compute rolling beta for each ticker vs SPY.
    Returns {ticker: beta}. Missing data → beta = 1.0 (market assumption).
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        returns = _load_returns(tickers, conn)
        if returns.empty or SPY_TICKER not in returns.columns:
            logger.warning("Beta: insufficient price data, defaulting all to 1.0")
            return {t: 1.0 for t in tickers}

        spy_ret = returns[SPY_TICKER]
        spy_var = spy_ret.var()
        if spy_var == 0:
            return {t: 1.0 for t in tickers}

        betas = {}
        for ticker in tickers:
            if ticker not in returns.columns:
                betas[ticker] = 1.0
                continue
            stock_ret = returns[ticker].dropna()
            aligned = pd.concat([stock_ret, spy_ret], axis=1).dropna()
            if len(aligned) < 20:
                betas[ticker] = 1.0
                continue
            cov = aligned.iloc[:, 0].cov(aligned.iloc[:, 1])
            betas[ticker] = cov / spy_var

        return betas

    finally:
        if close_conn:
            conn.close()


def portfolio_beta(
    weights: dict[str, float],
    betas: dict[str, float],
) -> dict[str, float]:
    """
    Compute long book beta, short book beta, and net portfolio beta.
    weights: {ticker: signed weight} (positive = long, negative = short)
    """
    long_beta = sum(w * betas.get(t, 1.0) for t, w in weights.items() if w > 0)
    short_beta = sum(w * betas.get(t, 1.0) for t, w in weights.items() if w < 0)
    net_beta = long_beta + short_beta

    return {
        "long_beta":  round(long_beta, 4),
        "short_beta": round(short_beta, 4),
        "net_beta":   round(net_beta, 4),
    }
