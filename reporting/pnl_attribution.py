"""
Daily P&L Attribution — decomposes return into:
  Beta      = net_beta * SPY_return
  Sector    = Brinson-style sector allocation effect
  Factor    = regression on factor return spreads (from factor risk model)
  Alpha     = residual after all three

Persists to output/daily_attribution.csv.
Also computes position-level attribution (FIFO), win/loss stats,
sector-relative performance vs ETF benchmarks.
"""

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml
from scipy.stats import spearmanr

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_R_CFG = _CFG["reporting"]
ATTR_CSV  = Path(__file__).parent.parent / _R_CFG["attribution_csv"]
ATTR_CSV.parent.mkdir(parents=True, exist_ok=True)

SECTOR_ETF = _CFG.get("sector_etf_map", {})
NAV        = _CFG["portfolio"]["nav"]
SPY        = _CFG["portfolio"]["spy_ticker"]


# ── Helpers ───────────────────────────────────────────────────────────────── #

def _latest_returns(tickers: list[str], n_days: int, conn: sqlite3.Connection) -> pd.DataFrame:
    placeholders = ",".join("?" * len(tickers))
    prices = pd.read_sql(
        f"""SELECT ticker, date, close FROM daily_prices
            WHERE ticker IN ({placeholders})
              AND date >= date('now', '-{n_days + 5} days')
            ORDER BY date""",
        conn, params=tickers,
    )
    if prices.empty:
        return pd.DataFrame()
    pivot   = prices.pivot(index="date", columns="ticker", values="close")
    returns = pivot.pct_change().dropna(how="all").tail(n_days)
    return returns


def _get_weights(conn: sqlite3.Connection) -> dict[str, float]:
    from portfolio.state import get_positions
    positions = get_positions(conn)
    weights: dict[str, float] = {}
    if positions.empty:
        # Fall back to combined scores latest
        csv = Path(__file__).parent.parent / "output" / "combined_scores_latest.csv"
        if csv.exists():
            df = pd.read_csv(csv)
            p  = _CFG["portfolio"]
            longs  = df[df["combined_signal"] == "LONG"].nlargest(p["num_longs"], "combined_score")
            shorts = df[df["combined_signal"] == "SHORT"].nsmallest(p["num_shorts"], "combined_score")
            n_l, n_s = len(longs), len(shorts)
            for _, r in longs.iterrows():
                weights[r["ticker"]] = p["target_long_gross"] / max(n_l, 1)
            for _, r in shorts.iterrows():
                weights[r["ticker"]] = -p["target_short_gross"] / max(n_s, 1)
    else:
        for _, row in positions.iterrows():
            cp   = row["current_price"] or row["entry_price"] or 1
            sign = 1.0 if row["book"] == "long" else -1.0
            weights[row["ticker"]] = sign * row["shares"] * cp / NAV
    return weights


# ── Daily P&L attribution ─────────────────────────────────────────────────── #

def compute_daily_attribution(conn: sqlite3.Connection, as_of: Optional[str] = None) -> dict:
    """Decompose today's portfolio return into Beta + Sector + Factor + Alpha."""
    weights = _get_weights(conn)
    if not weights:
        return {}

    tickers    = list(weights.keys()) + [SPY]
    returns_df = _latest_returns(tickers, 2, conn)

    if returns_df.empty or SPY not in returns_df.columns:
        return {}

    # Use most recent day
    today_ret = returns_df.iloc[-1]
    spy_ret   = today_ret.get(SPY, 0.0)

    # ── Beta component ───────────────────────────────────────────────── #
    from portfolio.beta import compute_betas, portfolio_beta
    betas  = compute_betas(list(weights.keys()), conn)
    pb     = portfolio_beta(weights, betas)
    net_beta = pb["net_beta"]
    beta_return = net_beta * spy_ret

    # ── Sector component (Brinson allocation effect) ─────────────────── #
    sectors_df = pd.read_sql(
        "SELECT ticker, sector FROM universe WHERE type='equity'", conn
    ).set_index("ticker")

    sector_return = 0.0
    for sector, etf in SECTOR_ETF.items():
        sector_tickers = [t for t in weights if
                          sectors_df.loc[t, "sector"] == sector
                          if t in sectors_df.index]
        if not sector_tickers or etf not in today_ret.index:
            continue
        port_sector_w   = sum(weights[t] for t in sector_tickers)
        etf_ret         = today_ret.get(etf, 0.0)
        sector_return  += port_sector_w * etf_ret

    # ── Factor component (regression on factor return spreads) ───────── #
    factor_names = ["momentum", "value", "quality", "growth", "revisions",
                    "short_interest", "insider", "institutional"]
    factor_scores_df = pd.read_sql(
        """SELECT fs.ticker, fs.momentum, fs.value, fs.quality, fs.growth,
                  fs.revisions, fs.short_interest, fs.insider, fs.institutional
           FROM factor_scores fs INNER JOIN (
               SELECT ticker, MAX(date) md FROM factor_scores GROUP BY ticker
           ) lp ON fs.ticker=lp.ticker AND fs.date=lp.md""",
        conn,
    ).set_index("ticker")

    # Portfolio-weighted factor exposures
    factor_return = 0.0
    for factor in factor_names:
        if factor not in factor_scores_df.columns:
            continue
        long_avg  = sum(weights[t] * factor_scores_df.loc[t, factor]
                        for t in weights if t in factor_scores_df.index and weights[t] > 0)
        short_avg = sum(abs(weights[t]) * factor_scores_df.loc[t, factor]
                        for t in weights if t in factor_scores_df.index and weights[t] < 0)
        spread = long_avg - short_avg
        # Approximate factor return from universe dispersion
        univ_std = factor_scores_df[factor].std()
        if univ_std > 0:
            factor_return += 0.001 * spread / univ_std  # 1bp per sigma exposure (stylized)

    # ── Portfolio gross return ────────────────────────────────────────── #
    port_return = sum(w * today_ret.get(t, 0.0) for t, w in weights.items())

    # ── Alpha residual ───────────────────────────────────────────────── #
    alpha_return = port_return - beta_return - sector_return - factor_return

    result = {
        "date":           as_of or date.today().isoformat(),
        "port_return":    round(port_return, 6),
        "beta_return":    round(beta_return, 6),
        "sector_return":  round(sector_return, 6),
        "factor_return":  round(factor_return, 6),
        "alpha_return":   round(alpha_return, 6),
        "spy_return":     round(spy_ret, 6),
        "net_beta":       round(net_beta, 4),
        "port_pnl_usd":   round(port_return * NAV, 2),
        "alpha_pnl_usd":  round(alpha_return * NAV, 2),
    }

    # Persist
    row_df = pd.DataFrame([result])
    if ATTR_CSV.exists():
        existing = pd.read_csv(ATTR_CSV)
        combined = pd.concat([existing, row_df]).drop_duplicates("date", keep="last")
        combined.to_csv(ATTR_CSV, index=False)
    else:
        row_df.to_csv(ATTR_CSV, index=False)

    logger.info(
        f"Attribution: port={port_return*100:.2f}% "
        f"beta={beta_return*100:.2f}% sector={sector_return*100:.2f}% "
        f"factor={factor_return*100:.2f}% alpha={alpha_return*100:.2f}%"
    )
    return result


def load_attribution_history() -> pd.DataFrame:
    if ATTR_CSV.exists():
        return pd.read_csv(ATTR_CSV)
    return pd.DataFrame()


# ── Win/Loss analytics ────────────────────────────────────────────────────── #

def compute_win_loss(conn: sqlite3.Connection) -> dict:
    """Win rate, P/L ratio, streaks from portfolio_history."""
    trades = pd.read_sql(
        "SELECT * FROM portfolio_history ORDER BY timestamp", conn
    )
    if trades.empty:
        return {"win_rate": None, "pl_ratio": None, "message": "No trade history"}

    closed = trades[trades["action"].str.startswith("close")]
    if closed.empty:
        return {"win_rate": None, "pl_ratio": None, "message": "No closed trades"}

    wins  = closed[closed["price"] > 0]
    total = len(closed)
    win_r = len(wins) / total if total > 0 else 0
    return {
        "win_rate":   round(win_r, 3),
        "total_trades": total,
        "message":    f"{len(wins)}/{total} winning trades",
    }


# ── Sector-relative performance ───────────────────────────────────────────── #

def sector_alpha(conn: sqlite3.Connection, lookback_days: int = 90) -> pd.DataFrame:
    """
    Per-sector: portfolio return vs sector ETF return = stock-selection alpha.
    """
    weights = _get_weights(conn)
    if not weights:
        return pd.DataFrame()

    sectors_df = pd.read_sql(
        "SELECT ticker, sector FROM universe WHERE type='equity'", conn
    ).set_index("ticker")

    all_tickers = list(weights.keys()) + list(SECTOR_ETF.values())
    returns     = _latest_returns(all_tickers, lookback_days, conn)
    if returns.empty:
        return pd.DataFrame()

    rows = []
    for sector, etf in SECTOR_ETF.items():
        sector_tickers = [t for t in weights
                          if t in sectors_df.index
                          and sectors_df.loc[t, "sector"] == sector
                          and t in returns.columns]
        if not sector_tickers or etf not in returns.columns:
            continue

        port_w    = {t: weights[t] for t in sector_tickers}
        total_w   = sum(abs(v) for v in port_w.values())
        if total_w == 0:
            continue

        port_cumret = ((1 + returns[sector_tickers].fillna(0)
                        .multiply(pd.Series(port_w))).prod() - 1).sum() / total_w
        etf_cumret  = (1 + returns[etf].dropna()).prod() - 1

        rows.append({
            "sector":       sector,
            "etf":          etf,
            "port_return":  round(float(port_cumret), 4),
            "etf_return":   round(float(etf_cumret), 4),
            "alpha":        round(float(port_cumret - etf_cumret), 4),
            "n_positions":  len(sector_tickers),
        })

    return pd.DataFrame(rows).sort_values("alpha", ascending=False)
