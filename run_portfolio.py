"""
Meridian Capital Partners — Layer 4 Portfolio Construction Entry Point.

Usage:
    python run_portfolio.py --current                     # show current positions & P&L
    python run_portfolio.py --whatif                      # show proposed rebalance (no commit)
    python run_portfolio.py --rebalance                   # generate & queue trades for approval
    python run_portfolio.py --whatif --optimize-method mvo   # use MVO optimizer
    python run_portfolio.py --whatif --optimize-method conviction
"""

import argparse
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_P = _CFG["portfolio"]

LOG_FILE = Path(__file__).parent / _P["portfolio_log"]
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, _CFG["logging"]["level"], logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("run_portfolio")


def _banner(title: str):
    logger.info("=" * 60)
    logger.info(f"  {title}")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Meridian Capital Partners — Portfolio Construction")
    parser.add_argument("--current",  action="store_true", help="Show current portfolio state")
    parser.add_argument("--whatif",   action="store_true", help="Show proposed rebalance without committing")
    parser.add_argument("--rebalance",action="store_true", help="Generate and queue trades for approval")
    parser.add_argument("--optimize-method", choices=["mvo", "conviction"],
                        default=_P["default_method"],
                        help="Optimization method (default: %(default)s)")
    parser.add_argument("--top-n", type=int, default=_P["num_longs"],
                        help="Number of long + short candidates")
    args = parser.parse_args()

    if not any([args.current, args.whatif, args.rebalance]):
        parser.print_help()
        sys.exit(0)

    run_id = str(uuid.uuid4())[:8]
    _banner(f"Meridian Portfolio Construction  [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")
    logger.info(f"Method: {args.optimize_method} | NAV: ${_P['nav']:,.0f} | run_id: {run_id}")

    from data.db import get_connection, init_db
    init_db()
    conn = get_connection()

    # ── Current portfolio ─────────────────────────────────────────────── #
    from portfolio.state import portfolio_summary, get_positions, get_pending_approvals

    _banner("CURRENT PORTFOLIO")
    summary = portfolio_summary(conn)
    for k, v in summary.items():
        logger.info(f"  {k:<30} {v}")

    if args.current:
        positions = get_positions(conn)
        if not positions.empty:
            print("\nCurrent Positions:")
            print(positions[["ticker","book","shares","current_price","unrealized_pnl","sector"]].to_string(index=False))
        pending = get_pending_approvals(conn)
        if not pending.empty:
            print(f"\nPending approvals: {len(pending)}")
            print(pending[["ticker","book","action","shares","estimated_price","status"]].to_string(index=False))
        conn.close()
        return

    # ── Load scores ───────────────────────────────────────────────────── #
    _banner("LOADING SCORES")
    combined_path = Path(__file__).parent / "output" / "combined_scores_latest.csv"
    if not combined_path.exists():
        logger.error("combined_scores_latest.csv not found. Run run_scoring.py and run_analysis.py first.")
        conn.close()
        sys.exit(1)

    combined_df = pd.read_csv(combined_path)
    n = args.top_n

    long_candidates  = combined_df[combined_df["combined_signal"] == "LONG"].nlargest(n, "combined_score")
    short_candidates = combined_df[combined_df["combined_signal"] == "SHORT"].nsmallest(n, "combined_score")

    long_tickers  = long_candidates["ticker"].tolist()
    short_tickers = short_candidates["ticker"].tolist()

    logger.info(f"Candidates: {len(long_tickers)} longs, {len(short_tickers)} shorts")

    scores  = dict(zip(combined_df["ticker"], combined_df["combined_score"]))
    sectors = dict(zip(combined_df["ticker"], combined_df["sector"].fillna("Unknown")))

    # ── Beta calculation ──────────────────────────────────────────────── #
    _banner("BETA CALCULATION")
    from portfolio.beta import compute_betas, portfolio_beta
    all_candidates = long_tickers + short_tickers
    betas = compute_betas(all_candidates, conn)
    logger.info(f"Betas computed for {len(betas)} tickers")
    logger.info(f"  Avg long beta:  {sum(betas.get(t,1) for t in long_tickers)/max(len(long_tickers),1):.3f}")
    logger.info(f"  Avg short beta: {sum(betas.get(t,1) for t in short_tickers)/max(len(short_tickers),1):.3f}")

    # ── Transaction costs ─────────────────────────────────────────────── #
    _banner("TRANSACTION COSTS")
    from portfolio.transaction_costs import estimate_costs, costs_to_return_drag
    # Estimate costs assuming avg 2% position change per trade
    trade_w  = {t: 0.02 for t in all_candidates}
    tc_bps   = estimate_costs(all_candidates, trade_w, conn)
    tc_drag  = costs_to_return_drag(tc_bps)
    avg_tc   = sum(tc_bps.values()) / max(len(tc_bps), 1)
    logger.info(f"Average transaction cost: {avg_tc:.1f} bps")

    # ── Optimization ──────────────────────────────────────────────────── #
    _banner(f"OPTIMIZATION ({args.optimize_method.upper()})")
    target_weights: dict[str, float] = {}

    if args.optimize_method == "mvo":
        from portfolio.mvo_optimizer import run_mvo
        target_weights = run_mvo(
            long_tickers, short_tickers, scores, betas, sectors, tc_drag, conn
        )
        if target_weights is None:
            logger.warning("MVO failed — falling back to conviction-tilt")
            args.optimize_method = "conviction"

    if args.optimize_method == "conviction" or target_weights is None:
        from portfolio.optimizer import run_conviction
        target_weights = run_conviction(long_tickers, short_tickers, scores, betas, sectors, conn)

    pb = portfolio_beta(target_weights, betas)
    logger.info(f"Target portfolio: long_beta={pb['long_beta']} short_beta={pb['short_beta']} net_beta={pb['net_beta']}")

    # ── Calendar warnings ─────────────────────────────────────────────── #
    _banner("CALENDAR WARNINGS")
    from portfolio.rebalance_schedule import check_schedule
    warnings = check_schedule(all_candidates, conn)
    if warnings:
        for w in warnings:
            logger.warning(f"  ! {w}")
    else:
        logger.info("  No calendar events of concern")

    # ── Factor exposures ──────────────────────────────────────────────── #
    _banner("FACTOR EXPOSURES")
    from portfolio.factor_exposure import compute_factor_exposures, print_exposure_table
    exposures = compute_factor_exposures(target_weights, conn)
    print_exposure_table(exposures)

    # ── Generate trades ───────────────────────────────────────────────── #
    _banner("TRADE LIST" + (" (WHATIF)" if args.whatif else ""))
    from portfolio.rebalance import generate_trades, print_trade_table
    trades_df = generate_trades(
        target_weights = target_weights,
        scores         = scores,
        sectors        = sectors,
        conn           = conn,
        whatif         = args.whatif,
        run_id         = run_id,
    )
    print_trade_table(trades_df)

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────── #
    _banner("SUMMARY")
    if not trades_df.empty:
        opens  = len(trades_df[trades_df["action"].str.startswith("open")])
        closes = len(trades_df[trades_df["action"].str.startswith("close")])
        adjusts= len(trades_df) - opens - closes
        logger.info(f"  Opens:   {opens}")
        logger.info(f"  Closes:  {closes}")
        logger.info(f"  Adjusts: {adjusts}")
        logger.info(f"  Avg cost: {trades_df['cost_bps'].mean():.1f} bps")
    if warnings:
        logger.warning(f"  {len(warnings)} calendar warning(s) — review before trading")
    if args.whatif:
        logger.info("  WHATIF mode: no trades committed. Run --rebalance to queue for approval.")
    elif args.rebalance:
        logger.info(f"  {len(trades_df)} trades queued for approval (Layer 6 executes).")
    _banner("Done")


if __name__ == "__main__":
    main()
