"""
Meridian Capital Partners — Layer 1 Data Ingestion Entry Point.

Run order: universe -> prices -> fundamentals -> short interest -> estimates
           -> earnings calendar -> transcripts -> SEC filings -> 13-F

Usage:
    python run_data.py                    # full run
    python run_data.py --no-filings       # skip SEC filings (fast daily run)
    python run_data.py --no-13f           # skip 13-F institutional (fast daily run)
    python run_data.py --no-filings --no-13f  # prices + fundamentals + short data only
    python run_data.py --forms 4          # selective form pull (Form 4 only)
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ──────────────────────────────────────────────────────────── #
_CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

LOG_FILE = Path(__file__).parent / _CFG["logging"]["log_file"]
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, _CFG["logging"]["level"], logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("run_data")


def _banner(title: str):
    logger.info("=" * 60)
    logger.info(f"  {title}")
    logger.info("=" * 60)


def _section(name: str, fn, *args, **kwargs) -> dict:
    _banner(name)
    t0 = time.time()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.time() - t0
        logger.info(f"[{name}] completed in {elapsed:.1f}s — {result}")
        return result if isinstance(result, dict) else {}
    except Exception as e:
        logger.error(f"[{name}] FAILED: {e}", exc_info=True)
        return {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Meridian Capital Partners — Data Layer")
    parser.add_argument("--no-filings", action="store_true",
                        help="Skip SEC EDGAR filings for fast daily runs")
    parser.add_argument("--no-13f", action="store_true",
                        help="Skip 13-F institutional holdings for fast daily runs")
    parser.add_argument("--forms", nargs="+", default=None,
                        help="Selective form pull, e.g. --forms 4 10-K")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Run only for specific tickers (overrides full universe)")
    args = parser.parse_args()

    run_start = datetime.utcnow()
    _banner(f"Meridian Capital Partners — Data Refresh  [{run_start.strftime('%Y-%m-%d %H:%M UTC')}]")

    summary = {}

    # 1. Universe
    from data.universe import refresh_universe, get_equity_tickers
    _section("Universe", refresh_universe)
    from data.db import get_connection as _get_conn
    with _get_conn() as _c:
        summary["universe_tickers"] = _c.execute("SELECT COUNT(*) FROM universe").fetchone()[0]

    # Resolve working ticker list
    tickers = args.tickers
    if tickers is None:
        from data.db import get_connection
        conn = get_connection()
        tickers = get_equity_tickers(conn)
        conn.close()

    summary["equity_tickers"] = len(tickers)
    logger.info(f"Working universe: {len(tickers)} equities")

    # 2. Prices
    from data.market_data import refresh_prices
    price_result = _section("Market Prices", refresh_prices, tickers)
    summary["bars_added"] = price_result.get("bars_added", 0)

    # 3. Fundamentals
    from data.fundamentals import refresh_fundamentals
    fund_result = _section("Fundamentals", refresh_fundamentals, tickers)
    summary["fundamentals_processed"] = fund_result.get("processed", 0)

    # 4. Short Interest
    from data.short_interest import refresh_short_interest
    si_result = _section("Short Interest", refresh_short_interest, tickers)
    summary["short_interest_updated"] = si_result.get("tickers_updated", 0)

    # 5. Analyst Estimates
    from data.estimates import refresh_estimates
    est_result = _section("Analyst Estimates", refresh_estimates, tickers)
    summary["estimates_updated"] = est_result.get("tickers_updated", 0)

    # 6. Earnings Calendar
    from data.earnings_calendar import refresh_earnings_calendar
    cal_result = _section("Earnings Calendar", refresh_earnings_calendar, tickers)
    summary["upcoming_earnings"] = cal_result.get("upcoming_earnings", 0)

    # 7. Transcripts (candidates only — pass empty list unless Layer 2 provides signals)
    from data.transcripts import refresh_transcripts
    transcript_result = _section("Earnings Transcripts", refresh_transcripts, [])
    summary["transcripts_fetched"] = transcript_result.get("transcripts_fetched", 0)

    # 8. SEC Filings + Form 4 (skip if --no-filings)
    if not args.no_filings:
        from data.sec_data import refresh_sec_data
        forms = args.forms  # None means all forms
        sec_result = _section("SEC Filings", refresh_sec_data, tickers, forms)
        summary["insider_transactions"] = sec_result.get("insider_transactions", 0)
        summary["filings_cached"] = sec_result.get("filings_cached", 0)
        summary["cluster_buy_signals"] = sec_result.get("cluster_buy_tickers", 0)
    else:
        logger.info("[SEC Filings] Skipped (--no-filings)")
        summary["insider_transactions"] = "skipped"

    # 9. 13-F Institutional Holdings (skip if --no-13f)
    if not args.no_13f:
        from data.institutional import refresh_institutional
        inst_result = _section("Institutional 13-F", refresh_institutional)
        summary["holdings_stored"] = inst_result.get("holdings_stored", 0)
        summary["new_position_signals"] = inst_result.get("new_position_signals", 0)
    else:
        logger.info("[Institutional 13-F] Skipped (--no-13f)")
        summary["holdings_stored"] = "skipped"

    # ── Final summary ─────────────────────────────────────────────────────── #
    elapsed_total = (datetime.utcnow() - run_start).total_seconds()
    _banner("Run Summary")
    for key, val in summary.items():
        logger.info(f"  {key:<30} {val}")
    logger.info(f"  {'total_elapsed_seconds':<30} {elapsed_total:.1f}")
    logger.info(f"  {'log_file':<30} {LOG_FILE}")
    _banner("Done")


if __name__ == "__main__":
    main()
