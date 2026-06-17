"""
Meridian Capital Partners — Layer 2 Scoring Engine Entry Point.

Runs all 8 factors, builds composite, flags LONG/SHORT candidates,
detects crowding, outputs scored_universe_latest.csv.

Usage:
    python run_scoring.py                    # full universe
    python run_scoring.py --ticker AAPL      # single-stock mode
    python run_scoring.py --no-market-fetch  # skip yfinance info refresh
"""

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────── #
_CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

LOG_FILE = Path(__file__).parent / _CFG["logging"].get("score_log", "output/run_scoring.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, _CFG["logging"]["level"], logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("run_scoring")


def _banner(title: str):
    logger.info("=" * 60)
    logger.info(f"  {title}")
    logger.info("=" * 60)


def _timed(label: str, fn, *args, **kwargs):
    t0 = time.time()
    result = fn(*args, **kwargs)
    logger.info(f"[{label}] {time.time()-t0:.1f}s")
    return result


def main():
    parser = argparse.ArgumentParser(description="Meridian Capital Partners — Scoring Engine")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Single ticker mode (e.g. --ticker AAPL)")
    parser.add_argument("--no-market-fetch", action="store_true",
                        help="Skip yfinance market metrics refresh")
    args = parser.parse_args()

    run_start = datetime.utcnow()
    _banner(f"Meridian Scoring Engine  [{run_start.strftime('%Y-%m-%d %H:%M UTC')}]")

    from data.db import get_connection, init_db
    init_db()
    conn = get_connection()

    # ── Load universe ──────────────────────────────────────────────────────── #
    universe = pd.read_sql(
        "SELECT ticker, company_name, sector, sub_industry FROM universe WHERE type='equity'",
        conn,
    )
    if args.ticker:
        universe = universe[universe["ticker"] == args.ticker.upper()]
        if universe.empty:
            logger.error(f"Ticker {args.ticker} not found in universe")
            conn.close()
            sys.exit(1)
        logger.info(f"Single-stock mode: {args.ticker}")
    logger.info(f"Scoring {len(universe)} tickers")

    # ── Market metrics refresh ─────────────────────────────────────────────── #
    if not args.no_market_fetch:
        from factors.loader import refresh_market_metrics
        _timed("Market metrics", refresh_market_metrics, universe["ticker"].tolist())

    # ── Compute each factor ────────────────────────────────────────────────── #
    from factors.momentum     import MomentumFactor
    from factors.value        import ValueFactor
    from factors.quality      import QualityFactor
    from factors.growth       import GrowthFactor
    from factors.revisions    import RevisionsFactor
    from factors.short_interest import ShortInterestFactor
    from factors.insider      import InsiderFactor
    from factors.institutional import InstitutionalFactor

    factor_instances = [
        MomentumFactor(),
        ValueFactor(),
        QualityFactor(),
        GrowthFactor(),
        RevisionsFactor(),
        ShortInterestFactor(),
        InsiderFactor(),
        InstitutionalFactor(),
    ]

    factor_dfs: dict[str, pd.DataFrame] = {}
    degenerate_warnings: list[str] = []

    for factor in factor_instances:
        _banner(factor.name.upper())
        try:
            df = _timed(factor.name, factor.compute, universe, conn)
            factor_dfs[factor.name] = df

            # Check for degenerate sub-factors (all 50)
            for sf in factor.sub_factors:
                if sf in df.columns:
                    spread = df[sf].std()
                    if spread < 1.0:
                        msg = f"DEGENERATE: {factor.name}.{sf} — no spread (std={spread:.2f})"
                        degenerate_warnings.append(msg)
                        logger.warning(msg)
        except Exception as e:
            logger.error(f"Factor {factor.name} failed: {e}", exc_info=True)
            factor_dfs[factor.name] = pd.DataFrame()

    # ── Build composite ────────────────────────────────────────────────────── #
    _banner("COMPOSITE")
    from factors.composite import build_composite, save_results, get_top_candidates
    result = _timed("Composite", build_composite, factor_dfs, conn, universe)
    save_results(result, conn)

    # ── Crowding detection ─────────────────────────────────────────────────── #
    _banner("CROWDING DETECTION")
    from factors.crowding import detect_crowding
    crowding = _timed("Crowding", detect_crowding, conn)

    conn.close()

    # ── Summary ────────────────────────────────────────────────────────────── #
    _banner("SUMMARY")
    longs, shorts = get_top_candidates(result, n=5)

    logger.info(f"\nTOP 5 LONGS:")
    for _, row in longs.iterrows():
        logger.info(f"  {row['ticker']:6s}  {row['sector']:30s}  composite={row['composite']:.1f}")

    logger.info(f"\nTOP 5 SHORTS:")
    for _, row in shorts.iterrows():
        logger.info(f"  {row['ticker']:6s}  {row['sector']:30s}  composite={row['composite']:.1f}")

    signal_counts = result["signal"].value_counts()
    logger.info(f"\nSignal breakdown: {signal_counts.to_dict()}")

    if crowding["warnings"]:
        logger.warning(f"\nCROWDING WARNINGS ({len(crowding['warnings'])}):")
        for w in crowding["warnings"]:
            logger.warning(f"  -> {w}")
    else:
        logger.info("\nCrowding: no anomalies")

    if degenerate_warnings:
        logger.warning(f"\nDEGENERATE FACTOR WARNINGS ({len(degenerate_warnings)}):")
        for w in degenerate_warnings:
            logger.warning(f"  -> {w}")

    elapsed = (datetime.utcnow() - run_start).total_seconds()
    logger.info(f"\nOutput: {_CFG['scoring']['output_file']}")
    logger.info(f"Total elapsed: {elapsed:.1f}s")
    _banner("Done")

    # Single-stock detailed printout
    if args.ticker:
        _print_single_stock(result, args.ticker.upper())


def _print_single_stock(result: pd.DataFrame, ticker: str):
    row = result[result["ticker"] == ticker]
    if row.empty:
        return
    row = row.iloc[0]
    print(f"\n{'='*50}")
    print(f"  {ticker} | {row.get('sector', '')} | {row.get('sub_industry', '')}")
    print(f"{'='*50}")
    print(f"  Signal:    {row.get('signal', '?')}")
    print(f"  Composite: {row.get('composite', 0):.1f} / 100")
    print(f"  Regime:    {row.get('regime', '?')}")
    print(f"\n  Factor Scores (0-100, sector-relative):")
    factor_names = [
        "momentum", "value", "quality", "growth",
        "revisions", "short_interest", "insider", "institutional",
    ]
    for f in factor_names:
        val = row.get(f, np.nan)
        bar = "|" * int(val // 10) if not pd.isna(val) else ""
        print(f"    {f:20s} {val:5.1f}  {bar}")
    print()


if __name__ == "__main__":
    import numpy as np
    main()
