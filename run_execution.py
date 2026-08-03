"""
Meridian Capital Partners — Layer 6 Execution Entry Point.

Reads approved trades from position_approvals, runs pre-trade risk veto,
submits market orders to Alpaca, polls for fills, and updates portfolio_positions.

Usage:
    python run_execution.py --dry-run        # simulate without sending orders
    python run_execution.py                  # execute all approved trades
    python run_execution.py --status         # show account balance + open positions
    python run_execution.py --halt-check     # abort immediately if HALT.lock exists
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_EXEC = _CFG["execution"]
LOG_FILE = Path(__file__).parent / _EXEC["execution_log"]
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, _CFG["logging"]["level"], logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("run_execution")


def _banner(title: str):
    logger.info("=" * 60)
    logger.info(f"  {title}")
    logger.info("=" * 60)


def _check_halt():
    halt = Path(__file__).parent / _CFG["risk"]["halt_lock_file"]
    if halt.exists():
        logger.error("HALT LOCK ACTIVE — execution blocked. Run: python run_risk_check.py --clear-halt")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Meridian Layer 6 — Alpaca Execution")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate execution without sending orders to Alpaca")
    parser.add_argument("--status", action="store_true",
                        help="Print Alpaca account status and open positions, then exit")
    parser.add_argument("--halt-check", action="store_true",
                        help="Abort if HALT.lock exists (default when running from automation)")
    args = parser.parse_args()

    run_start = datetime.utcnow()
    _banner(f"Meridian Layer 6 — Alpaca Execution  [{run_start.strftime('%Y-%m-%d %H:%M UTC')}]")

    if args.halt_check or not args.dry_run:
        _check_halt()

    # ── Account status mode ───────────────────────────────────────────── #
    from execution.alpaca_client import test_connection, get_open_positions

    if args.status:
        _banner("Alpaca Account Status")
        acct = test_connection()
        if not acct["ok"]:
            logger.error(f"Connection failed: {acct.get('error')}")
            sys.exit(1)
        logger.info(f"  Portfolio value : ${acct['portfolio_value']:>12,.2f}")
        logger.info(f"  Cash            : ${acct['cash']:>12,.2f}")
        logger.info(f"  Buying power    : ${acct['buying_power']:>12,.2f}")
        logger.info(f"  Shorting enabled: {acct['shorting_enabled']}")
        logger.info(f"  Account status  : {acct['status']}")

        positions = get_open_positions()
        if positions:
            _banner("Open Positions")
            logger.info(f"  {'Ticker':<8} {'Side':<6} {'Qty':>8} {'Entry':>10} {'Price':>10} {'PnL':>12}")
            logger.info("  " + "-" * 60)
            for p in positions:
                logger.info(
                    f"  {p['ticker']:<8} {p['side']:<6} {p['qty']:>8.0f} "
                    f"${p['avg_entry']:>9.2f} ${p['current_price']:>9.2f} "
                    f"${p['unrealized_pnl']:>+11,.2f}"
                )
        else:
            logger.info("No open positions.")
        return

    # ── Execution mode ────────────────────────────────────────────────── #
    from data.db import get_connection, init_db
    from execution.order_manager import run_execution

    init_db()
    conn = get_connection()

    if args.dry_run:
        _banner("DRY RUN — no orders will be sent")

    try:
        summary = run_execution(conn, dry_run=args.dry_run)
    except ConnectionError as e:
        logger.error(str(e))
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    # ── Print summary ─────────────────────────────────────────────────── #
    _banner("Execution Summary")
    logger.info(f"  Processed : {summary['processed']}")
    logger.info(f"  Filled    : {summary['filled']}")
    logger.info(f"  Vetoed    : {summary['vetoed']}")
    logger.info(f"  Errors    : {summary['errors']}")

    if summary.get("details"):
        _banner("Trade Details")
        for d in summary["details"]:
            if d["status"] == "filled":
                logger.info(
                    f"  FILLED  {d['ticker']:<6} "
                    f"fill=${d['fill_price']:.4f}  "
                    f"qty={d['filled_qty']:.0f}  "
                    f"slippage={d['slippage_bps']:+.1f}bps"
                )
            elif d["status"] == "vetoed":
                logger.warning(f"  VETOED  {d['ticker']:<6} {d.get('reason','')}")
            elif d["status"] == "dry_run":
                logger.info(f"  DRY RUN {d['ticker']:<6}")
            else:
                logger.error(f"  {d['status'].upper():<7} {d['ticker']:<6} {d.get('reason','')}")

    elapsed = (datetime.utcnow() - run_start).total_seconds()
    logger.info(f"  Elapsed   : {elapsed:.1f}s")
    _banner("Done")


if __name__ == "__main__":
    main()
