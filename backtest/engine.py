"""
Backtest engine — retrospective performance analysis of LONG/SHORT signals.

Modes:
  retrospective  Take current factor_scores signals, measure their price
                 performance backward over N trading days using daily_prices.
  walk_forward   For each historical scoring date in factor_scores, measure
                 forward returns until the next available scoring date.
"""

import sqlite3
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


# ── Price loading ─────────────────────────────────────────────────────── #

def _load_prices(conn: sqlite3.Connection, tickers: list[str], start_date: str) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(tickers))
    df = pd.read_sql(
        f"""SELECT ticker, date, close
            FROM daily_prices
            WHERE ticker IN ({placeholders}) AND date >= ?
            ORDER BY ticker, date""",
        conn,
        params=tickers + [start_date],
    )
    df["date"] = pd.to_datetime(df["date"].astype(str).str[:10])
    return df


# ── Return computations ───────────────────────────────────────────────── #

def _basket_returns(price_df: pd.DataFrame, tickers: list[str]) -> pd.Series:
    """Equal-weight daily returns for a basket; drops tickers missing >20% of dates."""
    available = [t for t in tickers if t in price_df["ticker"].values]
    if not available:
        return pd.Series(dtype=float)
    pivot = price_df[price_df["ticker"].isin(available)].pivot(
        index="date", columns="ticker", values="close"
    )
    min_obs = int(len(pivot) * 0.8)
    pivot = pivot.dropna(thresh=max(min_obs, 1), axis=1)
    if pivot.empty:
        return pd.Series(dtype=float)
    return pivot.pct_change().dropna().mean(axis=1)


def _individual_returns(price_df: pd.DataFrame, tickers: list[str]) -> list[dict]:
    """Per-ticker total return over the loaded period."""
    results = []
    for ticker in tickers:
        t_df = price_df[price_df["ticker"] == ticker].sort_values("date")
        if len(t_df) < 2:
            continue
        ret = t_df["close"].iloc[-1] / t_df["close"].iloc[0] - 1
        results.append({"ticker": ticker, "return_pct": round(ret * 100, 2)})
    return sorted(results, key=lambda x: x["return_pct"], reverse=True)


# ── Statistics ────────────────────────────────────────────────────────── #

def _compute_stats(returns: pd.Series, label: str = "") -> dict:
    # Drop NaN values — arise when a basket ticker has no prices for a date
    returns = returns.dropna()
    if returns.empty or len(returns) < 2:
        return {"label": label, "n_days": 0, "total_return_pct": None}

    n = len(returns)
    total_ret = (1 + returns).prod() - 1
    ann_ret   = (1 + total_ret) ** (TRADING_DAYS_PER_YEAR / n) - 1
    ann_vol   = returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe    = ann_ret / ann_vol if ann_vol > 0 else 0.0

    wealth   = (1 + returns).cumprod()
    peak     = wealth.cummax()
    max_dd   = ((wealth - peak) / peak).min()
    win_rate = (returns > 0).mean()

    return {
        "label":            label,
        "n_days":           n,
        "total_return_pct": round(float(total_ret) * 100, 2),
        "ann_return_pct":   round(float(ann_ret) * 100, 2),
        "ann_vol_pct":      round(float(ann_vol) * 100, 2) if not np.isnan(ann_vol) else None,
        "sharpe_ratio":     round(float(sharpe), 2) if not np.isnan(sharpe) else None,
        "max_drawdown_pct": round(float(max_dd) * 100, 2) if not np.isnan(max_dd) else None,
        "win_rate_pct":     round(float(win_rate) * 100, 1),
    }


# ── Public API ────────────────────────────────────────────────────────── #

def run_retrospective_backtest(
    conn: sqlite3.Connection,
    top_n: int = 10,
    lookback_days: int = 14,
) -> dict:
    """
    Take the most-recent LONG/SHORT signals from factor_scores and measure
    how those baskets actually performed over the last lookback_days trading days.

    This is a look-forward test on a FIXED signal (no rebalancing). It answers:
    "If we had entered these positions two weeks ago, how would we look today?"

    Returns a dict with stats, individual stock returns, and daily series.
    """
    # ── Load current signals ─────────────────────────────────────────── #
    scores_df = pd.read_sql(
        """SELECT fs.ticker, fs.composite, fs.signal
           FROM factor_scores fs
           INNER JOIN (
               SELECT ticker, MAX(date) AS md FROM factor_scores GROUP BY ticker
           ) lp ON fs.ticker = lp.ticker AND fs.date = lp.md
           WHERE fs.signal IN ('LONG','SHORT')""",
        conn,
    )

    if scores_df.empty:
        return {"error": "No factor scores found — run run_scoring.py first."}

    scoring_date = conn.execute("SELECT MAX(date) FROM factor_scores").fetchone()[0]

    long_tickers  = scores_df[scores_df["signal"] == "LONG"].nlargest(top_n, "composite")["ticker"].tolist()
    short_tickers = scores_df[scores_df["signal"] == "SHORT"].nsmallest(top_n, "composite")["ticker"].tolist()

    # ── Load prices ──────────────────────────────────────────────────── #
    all_tickers = list(set(long_tickers + short_tickers + ["SPY"]))
    # Extra buffer: weekends + holidays can inflate calendar days vs trading days
    start_date  = (date.today() - timedelta(days=lookback_days * 2 + 5)).isoformat()
    price_df    = _load_prices(conn, all_tickers, start_date)

    if price_df.empty:
        return {"error": "No price data found — run run_data.py first."}

    # Trim to exactly the last lookback_days trading days
    all_dates = sorted(price_df["date"].unique())
    if len(all_dates) > lookback_days:
        cutoff = all_dates[-lookback_days]
        price_df = price_df[price_df["date"] >= cutoff]

    # ── Basket returns ───────────────────────────────────────────────── #
    long_rets  = _basket_returns(price_df, long_tickers)
    short_rets = _basket_returns(price_df, short_tickers)
    spy_rets   = _basket_returns(price_df, ["SPY"])

    # Long-short spread: long books gain when long goes up, short books gain when
    # short goes down (negative return). Dollar-neutral: L-S on $1 equity, 2x gross.
    ls_rets = long_rets.sub(short_rets, fill_value=0)

    # Align all series to common dates
    all_idx   = long_rets.index.union(short_rets.index).union(spy_rets.index)
    long_rets  = long_rets.reindex(all_idx)
    short_rets = short_rets.reindex(all_idx)
    ls_rets    = ls_rets.reindex(all_idx)
    spy_rets   = spy_rets.reindex(all_idx)

    actual_start = str(price_df["date"].min().date()) if not price_df.empty else "N/A"
    actual_end   = str(price_df["date"].max().date()) if not price_df.empty else "N/A"

    return {
        "mode":         "retrospective",
        "scoring_date": scoring_date,
        "period":       {"start": actual_start, "end": actual_end},
        "n_longs":      len(long_tickers),
        "n_shorts":     len(short_tickers),
        "long_basket":       _compute_stats(long_rets,  "Long Basket"),
        "short_basket_raw":  _compute_stats(short_rets, "Short Basket (price)"),
        "long_short_spread": _compute_stats(ls_rets,    "L/S Spread (2x gross)"),
        "spy_benchmark":     _compute_stats(spy_rets,   "SPY Benchmark"),
        "individual_longs":  _individual_returns(price_df, long_tickers),
        "individual_shorts": _individual_returns(price_df, short_tickers),
        "daily": {
            "dates":        [str(d.date()) for d in all_idx.tolist()],
            "long_basket":  long_rets.fillna(0).round(6).tolist(),
            "short_basket": short_rets.fillna(0).round(6).tolist(),
            "long_short":   ls_rets.fillna(0).round(6).tolist(),
            "spy":          spy_rets.fillna(0).round(6).tolist(),
        },
        "long_tickers":  long_tickers,
        "short_tickers": short_tickers,
    }


def run_walk_forward_backtest(
    conn: sqlite3.Connection,
    top_n: int = 10,
) -> dict:
    """
    Walk-forward: for each historical scoring date in factor_scores, hold
    the top-N LONG/SHORT basket until the NEXT scoring date, then re-signal.

    Only useful when you have multiple scoring dates (run run_scoring.py daily).
    Returns error if fewer than 2 distinct scoring dates exist.
    """
    scoring_dates = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM factor_scores ORDER BY date"
        ).fetchall()
    ]

    if len(scoring_dates) < 2:
        return {
            "error": (
                f"Walk-forward requires at least 2 distinct scoring dates; "
                f"found {len(scoring_dates)}. Run run_scoring.py on multiple days."
            )
        }

    period_results = []

    for i, score_date in enumerate(scoring_dates[:-1]):
        next_date = scoring_dates[i + 1]

        # Get signals as of score_date
        scores_df = pd.read_sql(
            """SELECT ticker, composite, signal
               FROM factor_scores
               WHERE date = ? AND signal IN ('LONG','SHORT')""",
            conn,
            params=(score_date,),
        )
        if scores_df.empty:
            continue

        long_tickers  = scores_df[scores_df["signal"] == "LONG"].nlargest(top_n, "composite")["ticker"].tolist()
        short_tickers = scores_df[scores_df["signal"] == "SHORT"].nsmallest(top_n, "composite")["ticker"].tolist()
        all_tickers   = list(set(long_tickers + short_tickers + ["SPY"]))

        price_df = _load_prices(conn, all_tickers, score_date)
        price_df = price_df[price_df["date"] <= next_date]
        if len(price_df["date"].unique()) < 2:
            continue

        long_rets  = _basket_returns(price_df, long_tickers)
        short_rets = _basket_returns(price_df, short_tickers)
        spy_rets   = _basket_returns(price_df, ["SPY"])
        ls_rets    = long_rets.sub(short_rets, fill_value=0)

        period_results.append({
            "signal_date": score_date,
            "hold_until":  next_date,
            "long_return":  round((1 + long_rets).prod() - 1, 4) if not long_rets.empty else None,
            "short_return": round((1 + short_rets).prod() - 1, 4) if not short_rets.empty else None,
            "ls_return":    round((1 + ls_rets).prod() - 1, 4) if not ls_rets.empty else None,
            "spy_return":   round((1 + spy_rets).prod() - 1, 4) if not spy_rets.empty else None,
        })

    if not period_results:
        return {"error": "No complete holding periods found in price data."}

    return {
        "mode":          "walk_forward",
        "n_periods":     len(period_results),
        "scoring_dates": scoring_dates,
        "periods":       period_results,
    }
