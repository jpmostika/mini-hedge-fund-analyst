"""
Meridian Capital Partners — Layer 3 Claude AI Analysis Entry Point.

Usage:
    python run_analysis.py --estimate-cost          # estimate cost without running
    python run_analysis.py --ticker AAPL            # single stock deep-dive
    python run_analysis.py --sector Technology      # analyze one sector
    python run_analysis.py                          # full run (top 20 long + 20 short)
    python run_analysis.py --no-reports             # skip markdown report generation
    python run_analysis.py --model claude-haiku-4-5 # use cheaper/faster model
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────── #
_CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_ANALYSIS_CFG = _CFG["analysis"]
LOG_FILE = Path(__file__).parent / _ANALYSIS_CFG["analysis_log"]
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, _CFG["logging"]["level"], logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("run_analysis")


def _banner(title: str):
    logger.info("=" * 60)
    logger.info(f"  {title}")
    logger.info("=" * 60)


def _estimate_cost(n_tickers: int, model: str) -> float:
    """Rough cost estimate: 4 analyzers x ~3K tokens each."""
    from analysis.cost_tracker import _get_pricing
    rates = _get_pricing(model)
    avg_input_tokens  = 3_500  # system prompt (cached) + user prompt
    avg_output_tokens = 800
    # First call pays full input; subsequent calls pay cache_read rate
    cache_write_cost = (avg_input_tokens / 1_000_000) * rates["cache_write_per_mtok"]
    cache_read_cost  = (avg_input_tokens / 1_000_000) * rates["cache_read_per_mtok"]
    output_cost      = (avg_output_tokens / 1_000_000) * rates["output_per_mtok"]
    # 4 analyzers per ticker; 1 cache write + 3 cache reads per analyzer (amortized over run)
    per_ticker = 4 * (cache_write_cost + cache_read_cost + output_cost)
    # Plus sector analyses (~1 per sector, ~10 sectors)
    sector_cost = 10 * (cache_write_cost + output_cost * 2)
    return (per_ticker * n_tickers) + sector_cost


def main():
    parser = argparse.ArgumentParser(description="Meridian Capital Partners — Layer 3 Analysis")
    parser.add_argument("--estimate-cost", action="store_true",
                        help="Estimate cost without running any Claude calls")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Single-ticker mode (e.g. --ticker AAPL)")
    parser.add_argument("--sector", type=str, default=None,
                        help="Analyze one sector (e.g. --sector Technology)")
    parser.add_argument("--model", type=str,
                        default=_ANALYSIS_CFG["model"],
                        help="Override Claude model")
    parser.add_argument("--no-reports", action="store_true",
                        help="Skip markdown report generation")
    parser.add_argument("--top-n", type=int,
                        default=_ANALYSIS_CFG["top_candidates"],
                        help="Number of long + short candidates to analyze")
    args = parser.parse_args()

    run_start = datetime.utcnow()
    _banner(f"Meridian Layer 3 — Claude AI Analysis  [{run_start.strftime('%Y-%m-%d %H:%M UTC')}]")
    logger.info(f"Model: {args.model} | Cost ceiling: ${_ANALYSIS_CFG['cost_ceiling_per_run']}")

    # ── Load combined scores to get candidate list ─────────────────────── #
    from data.db import get_connection, init_db
    from analysis.combined_score import compute_combined, get_top_candidates, get_top_candidates_within

    init_db()
    conn = get_connection()

    # Candidate selection is quant-only so the analyzed set is stable across runs
    # (cached Claude scores must not feed back into who gets analyzed).
    combined_df = compute_combined(conn, include_claude=False)

    if combined_df.empty:
        logger.error("No quantitative scores found. Run run_scoring.py first.")
        conn.close()
        sys.exit(1)

    # ── Determine working ticker list ──────────────────────────────────── #
    if args.ticker:
        ticker_upper = args.ticker.upper()
        work_df = combined_df[combined_df["ticker"] == ticker_upper]
        if work_df.empty:
            logger.error(f"Ticker {ticker_upper} not found in scored universe")
            conn.close()
            sys.exit(1)
        tickers = [ticker_upper]
        logger.info(f"Single-ticker mode: {ticker_upper}")

    elif args.sector:
        work_df = combined_df[combined_df["sector"].str.contains(args.sector, case=False, na=False)]
        tickers = work_df["ticker"].tolist()
        logger.info(f"Sector mode: {args.sector} ({len(tickers)} tickers)")

    else:
        longs, shorts = get_top_candidates(combined_df, n=args.top_n)
        work_df = pd.concat([longs, shorts]).drop_duplicates("ticker")
        tickers = work_df["ticker"].tolist()
        logger.info(f"Full run: {len(tickers)} candidates (top {args.top_n} long + short)")

    # ── Cost estimate ──────────────────────────────────────────────────── #
    est = _estimate_cost(len(tickers), args.model)
    logger.info(f"Estimated cost: ${est:.2f} for {len(tickers)} tickers")

    if args.estimate_cost:
        print(f"\nEstimated cost: ${est:.2f} for {len(tickers)} tickers using {args.model}")
        print(f"Cost ceiling:   ${_ANALYSIS_CFG['cost_ceiling_per_run']:.2f}")
        conn.close()
        return

    if est > _ANALYSIS_CFG["cost_ceiling_per_run"]:
        logger.error(
            f"Estimated cost ${est:.2f} exceeds ceiling ${_ANALYSIS_CFG['cost_ceiling_per_run']:.2f}. "
            f"Use --top-n to reduce scope or raise cost_ceiling_per_run in config.yaml."
        )
        conn.close()
        sys.exit(1)

    # ── Initialize Claude client and tracker ───────────────────────────── #
    from analysis.api_client import ClaudeClient
    from analysis.cost_tracker import CostTracker, CostCeilingExceeded
    from analysis.cache import cache_evict_expired

    cache_evict_expired()
    client  = ClaudeClient(model=args.model)
    tracker = CostTracker(ceiling=_ANALYSIS_CFG["cost_ceiling_per_run"])

    # Import analyzers
    from analysis import (
        earnings_analyzer, filing_analyzer,
        risk_analyzer, insider_analyzer,
    )
    from analysis.sector_analysis import analyze_sector
    from analysis.combined_score import load_claude_scores

    # ── Run analyzers per ticker ────────────────────────────────────────── #
    _banner("RUNNING ANALYZERS")
    results_by_ticker: dict[str, dict] = {}
    errors = 0

    for ticker in tickers:
        logger.info(f"Analyzing {ticker}...")
        r: dict[str, dict] = {}
        try:
            r["earnings_call"]    = earnings_analyzer.analyze(ticker, client, tracker, conn)
            r["filing_quality"]   = filing_analyzer.analyze(ticker, client, tracker, conn)
            r["risk_factors"]     = risk_analyzer.analyze(ticker, client, tracker, conn)
            r["insider_activity"] = insider_analyzer.analyze(ticker, client, tracker, conn)
            results_by_ticker[ticker] = r
        except CostCeilingExceeded as e:
            logger.error(f"COST CEILING HIT: {e}")
            break
        except Exception as e:
            logger.error(f"Analyzer error for {ticker}: {e}", exc_info=True)
            errors += 1

    # ── Sector analysis ─────────────────────────────────────────────────── #
    _banner("SECTOR ANALYSIS")
    sector_analyses: dict[str, dict] = {}

    if not args.ticker:  # skip sector analysis in single-ticker mode
        sectors = work_df["sector"].unique()
        for sector in sectors:
            sector_tickers = work_df[work_df["sector"] == sector]["ticker"].tolist()
            if len(sector_tickers) < 2:
                continue

            sector_data = []
            for t in sector_tickers:
                row = work_df[work_df["ticker"] == t].iloc[0]
                r = results_by_ticker.get(t, {})
                sector_data.append({
                    "ticker":           t,
                    "composite_score":  float(row.get("combined_score", 50)),
                    "signal":           row.get("combined_signal", "NEUTRAL"),
                    "earnings_summary": r.get("earnings_call", {}).get("one_line_summary") if r.get("earnings_call") else None,
                    "filing_summary":   r.get("filing_quality", {}).get("one_line_summary") if r.get("filing_quality") else None,
                    "risk_summary":     r.get("risk_factors", {}).get("one_line_summary") if r.get("risk_factors") else None,
                    "insider_summary":  r.get("insider_activity", {}).get("one_line_summary") if r.get("insider_activity") else None,
                })

            try:
                sa = analyze_sector(sector, sector_data, client, tracker)
                if sa:
                    sector_analyses[sector] = sa
            except CostCeilingExceeded as e:
                logger.error(f"COST CEILING HIT during sector analysis: {e}")
                break
            except Exception as e:
                logger.error(f"Sector analysis failed for {sector}: {e}", exc_info=True)

    # ── Recompute combined scores with fresh Claude data ────────────────── #
    _banner("COMBINED SCORES")
    combined_df = compute_combined(conn)
    combined_df.to_csv(
        Path(__file__).parent / "output" / "combined_scores_latest.csv",
        index=False,
    )
    logger.info("Combined scores updated: output/combined_scores_latest.csv")

    # ── Generate reports ─────────────────────────────────────────────────── #
    if not args.no_reports:
        _banner("REPORT GENERATION")
        from analysis.report_generator import generate_reports
        # Report on the candidates we actually analyzed, not a fresh universe-wide
        # signal selection (which would surface un-analyzed names post-recompute).
        analyzed_df = combined_df[combined_df["ticker"].isin(tickers)].copy()
        written = generate_reports(
            combined_df, sector_analyses, conn,
            n_longs=args.top_n, n_shorts=args.top_n,
            candidates=analyzed_df,
        )
        logger.info(f"Generated {len(written)} reports")

    conn.close()

    # ── Final summary ─────────────────────────────────────────────────────── #
    _banner("SUMMARY")
    tracker.log_summary()

    # Rank within the analyzed pool by blended score (quant+claude), not against
    # the un-analyzed universe — see get_top_candidates_within.
    longs_final, shorts_final = get_top_candidates_within(combined_df, tickers, n=5)

    def _fmt(row):
        q = row["quant_score"]
        c = f"{row['claude_score']:.0f}" if pd.notna(row["claude_score"]) else "n/a"
        return (f"  {row['ticker']:6s}  {row['sector']:30s}  "
                f"blended={row['combined_raw']:.1f}  (quant={q:.0f} claude={c})")

    logger.info("\nTOP 5 LONGS (analyzed pool, by blended score):")
    for _, row in longs_final.iterrows():
        logger.info(_fmt(row))

    logger.info("\nTOP 5 SHORTS (analyzed pool, by blended score):")
    for _, row in shorts_final.iterrows():
        logger.info(_fmt(row))

    elapsed = (datetime.utcnow() - run_start).total_seconds()
    logger.info(f"\nTotal elapsed: {elapsed:.1f}s | Errors: {errors}")
    _banner("Done")


if __name__ == "__main__":
    main()
