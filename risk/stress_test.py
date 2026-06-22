"""
Stress Testing — 6 scenarios (3 historical, 3 synthetic).

Historical (actual stock returns from yfinance, cached as parquet):
  2008 Financial Crisis  Sep 15 2008 – Mar 09 2009
  2020 Covid Crash       Feb 19 2020 – Apr 20 2020
  2022 Rate Hikes        Jan 03 2022 – Oct 13 2022

Synthetic:
  Sector Shock      Most concentrated sector -30%
  Momentum Reversal Top quintile -20%, bottom quintile +20%
  Short Squeeze     All short positions +30%

Reports P&L ($, %) broken into long and short book contributions.
"""

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml
import yfinance as yf

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_R = _CFG["risk"]
_P = _CFG["portfolio"]
NAV = _P["nav"]

STRESS_DIR = Path(__file__).parent.parent / _R["stress_cache_dir"]
STRESS_DIR.mkdir(parents=True, exist_ok=True)


# ── Historical scenario loader ─────────────────────────────────────────────── #

def _load_historical_returns(label: str, start: str, end: str, tickers: list[str]) -> pd.DataFrame:
    """
    Download or load from parquet cache total returns for the scenario window.
    Returns DataFrame indexed by ticker with 'total_return' column.
    """
    cache_file = STRESS_DIR / f"{label.replace(' ', '_')}.parquet"

    if cache_file.exists():
        logger.info(f"[stress] Loading cached returns: {cache_file.name}")
        return pd.read_parquet(cache_file)

    logger.info(f"[stress] Downloading returns for '{label}' ({start} → {end})...")
    try:
        raw = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False)
        if raw.empty:
            return pd.DataFrame()

        if isinstance(raw.columns, pd.MultiIndex):
            closes = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw.xs("Close", axis=1, level=0)
        else:
            closes = raw[["Close"]] if "Close" in raw.columns else raw

        # Total return = (last_price / first_price) - 1
        first = closes.iloc[0]
        last  = closes.iloc[-1]
        total_ret = (last / first - 1).rename("total_return").to_frame()
        total_ret.index.name = "ticker"

        total_ret.to_parquet(cache_file)
        logger.info(f"[stress] Cached {len(total_ret)} ticker returns → {cache_file.name}")
        return total_ret

    except Exception as e:
        logger.error(f"[stress] Historical download failed for '{label}': {e}")
        return pd.DataFrame()


def _compute_pnl(
    weights: dict[str, float],
    ticker_returns: dict[str, float],
    scenario_label: str,
) -> dict:
    """Compute portfolio P&L from weights and per-ticker returns."""
    long_pnl  = 0.0
    short_pnl = 0.0
    long_contributions  = []
    short_contributions = []

    for ticker, w in weights.items():
        ret = ticker_returns.get(ticker, 0.0)
        contrib = w * ret * NAV  # signed dollar P&L
        if w > 0:
            long_pnl += contrib
            long_contributions.append((ticker, round(contrib, 0)))
        else:
            short_pnl += contrib
            short_contributions.append((ticker, round(contrib, 0)))

    total_pnl = long_pnl + short_pnl
    return {
        "scenario":    scenario_label,
        "total_pnl":   round(total_pnl, 0),
        "total_pct":   round(total_pnl / NAV * 100, 2),
        "long_pnl":    round(long_pnl, 0),
        "long_pct":    round(long_pnl / NAV * 100, 2),
        "short_pnl":   round(short_pnl, 0),
        "short_pct":   round(short_pnl / NAV * 100, 2),
        "top_long_contributors":  sorted(long_contributions,  key=lambda x: x[1])[:3],
        "top_short_contributors": sorted(short_contributions, key=lambda x: x[1], reverse=True)[:3],
    }


# ── Synthetic scenarios ────────────────────────────────────────────────────── #

def _sector_shock(weights: dict[str, float], sectors: dict[str, str], shock: float = -0.30) -> dict[str, float]:
    """Apply shock to the most concentrated sector (by gross weight)."""
    sector_exposure: dict[str, float] = {}
    for ticker, w in weights.items():
        sec = sectors.get(ticker, "Unknown")
        sector_exposure[sec] = sector_exposure.get(sec, 0) + abs(w)
    if not sector_exposure:
        return {}
    most_concentrated = max(sector_exposure, key=sector_exposure.get)
    return {t: (shock if sectors.get(t) == most_concentrated else 0.0) for t in weights}


def _momentum_reversal(
    weights: dict[str, float],
    factor_scores: dict[str, float],
    top_shock: float = -0.20,
    bot_shock: float = +0.20,
) -> dict[str, float]:
    """Top quintile momentum -20%, bottom quintile +20%."""
    scores = pd.Series(factor_scores).dropna()
    q80 = scores.quantile(0.80)
    q20 = scores.quantile(0.20)
    returns = {}
    for t in weights:
        s = factor_scores.get(t, 50.0)
        if s >= q80:
            returns[t] = top_shock
        elif s <= q20:
            returns[t] = bot_shock
        else:
            returns[t] = 0.0
    return returns


def _short_squeeze(weights: dict[str, float], squeeze: float = +0.30) -> dict[str, float]:
    """All short positions gain squeeze%."""
    return {t: (squeeze if w < 0 else 0.0) for t, w in weights.items()}


# ── Main stress test runner ────────────────────────────────────────────────── #

def run_stress_tests(
    weights: dict[str, float],
    sectors: dict[str, str],
    factor_scores: dict[str, float],
    conn=None,
) -> list[dict]:
    """
    Run all 6 stress scenarios. Returns list of result dicts.
    """
    all_tickers = list(weights.keys())
    results = []

    # ── Historical scenarios ─────────────────────────────────────────── #
    for key, params in _R["stress_periods"].items():
        label = params["label"]
        ret_df = _load_historical_returns(label, params["start"], params["end"], all_tickers)
        if ret_df.empty:
            logger.warning(f"[stress] No data for {label}")
            continue
        ticker_returns = ret_df["total_return"].to_dict()
        results.append(_compute_pnl(weights, ticker_returns, label))

    # ── Synthetic scenarios ──────────────────────────────────────────── #

    # 1. Sector shock
    sector_rets = _sector_shock(weights, sectors, shock=-0.30)
    if sector_rets:
        most_conc = max(
            {sec: sum(abs(w) for t, w in weights.items() if sectors.get(t) == sec)
             for sec in set(sectors.values())},
            key=lambda s: sum(abs(w) for t, w in weights.items() if sectors.get(t) == s),
        )
        results.append(_compute_pnl(weights, sector_rets, f"Sector Shock: {most_conc} -30%"))

    # 2. Momentum reversal (quant quake)
    mom_rets = _momentum_reversal(weights, factor_scores)
    results.append(_compute_pnl(weights, mom_rets, "Momentum Reversal (Quant Quake)"))

    # 3. Short squeeze
    squeeze_rets = _short_squeeze(weights)
    results.append(_compute_pnl(weights, squeeze_rets, "Short Squeeze (+30% on shorts)"))

    return results


def print_stress_table(results: list[dict]):
    print(f"\n{'Scenario':<40} {'Total $':>12} {'Total %':>8} {'Long $':>12} {'Short $':>12}")
    print("-" * 90)
    for r in results:
        print(
            f"  {r['scenario']:<38} "
            f"${r['total_pnl']:>11,.0f} "
            f"{r['total_pct']:>+7.1f}% "
            f"${r['long_pnl']:>11,.0f} "
            f"${r['short_pnl']:>11,.0f}"
        )
    print()
