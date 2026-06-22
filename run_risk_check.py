"""
Meridian Capital Partners — Layer 5 Risk Management Entry Point.

Usage:
    python run_risk_check.py                  # full daily risk check
    python run_risk_check.py --stress         # full check + all 6 stress scenarios
    python run_risk_check.py --tail-only      # only VIX/credit tail risk (fast)
    python run_risk_check.py --clear-halt     # remove KILL_SWITCH halt lock
    python run_risk_check.py --pre-trade AAPL long 100 185.50  # single trade check
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_R = _CFG["risk"]

LOG_FILE = Path(__file__).parent / _R["risk_log"]
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, _CFG["logging"]["level"], logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("run_risk_check")


def _banner(title: str):
    logger.info("=" * 60)
    logger.info(f"  {title}")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Meridian Risk Management")
    parser.add_argument("--stress",      action="store_true", help="Run all 6 stress scenarios")
    parser.add_argument("--tail-only",   action="store_true", help="Only tail risk check (fast)")
    parser.add_argument("--clear-halt",  action="store_true", help="Clear KILL_SWITCH halt lock")
    parser.add_argument("--pre-trade",   nargs=4,
                        metavar=("TICKER","BOOK","SHARES","PRICE"),
                        help="Check a single proposed trade")
    args = parser.parse_args()

    _banner(f"Meridian Risk Check  [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")

    from data.db import get_connection, init_db
    init_db()
    conn = get_connection()

    # ── Clear halt ──────────────────────────────────────────────────── #
    if args.clear_halt:
        from risk.circuit_breakers import clear_halt, halt_status
        status = halt_status()
        if status:
            logger.info(f"Clearing halt: {status}")
            clear_halt()
            logger.info("Halt lock removed. Trading may resume after manual review.")
        else:
            logger.info("No halt lock active.")
        conn.close()
        return

    # ── Check existing halt ─────────────────────────────────────────── #
    from risk.circuit_breakers import halt_status
    halt = halt_status()
    if halt:
        logger.critical(
            f"HALT LOCK ACTIVE: {halt.get('reason')} "
            f"(since {halt.get('timestamp')}). "
            "Run --clear-halt after manual review."
        )

    # ── Tail-only mode ──────────────────────────────────────────────── #
    if args.tail_only:
        _banner("TAIL RISK ONLY")
        from risk.tail_risk import check_tail_risk
        tail = check_tail_risk(conn)
        logger.info(f"VIX: {tail.get('vix')} | Credit z: {tail.get('credit_z')} | Action: {tail.get('action')}")
        for t in tail.get("triggers", []):
            logger.warning(f"  ! {t}")
        conn.close()
        return

    # ── Single pre-trade check ──────────────────────────────────────── #
    if args.pre_trade:
        ticker, book, shares_str, price_str = args.pre_trade
        shares = float(shares_str)
        price  = float(price_str)
        _banner(f"PRE-TRADE CHECK: {ticker} {book} {shares:.0f}@${price:.2f}")

        from risk.pre_trade import check_pre_trade
        from portfolio.beta import compute_betas
        betas = compute_betas([ticker], conn)

        approved, reason, final_shares = check_pre_trade(
            ticker=ticker,
            action=f"open_{book}",
            shares=shares,
            estimated_price=price,
            book=book,
            sector=conn.execute("SELECT sector FROM universe WHERE ticker=?", (ticker,)).fetchone()[0] if conn.execute("SELECT sector FROM universe WHERE ticker=?", (ticker,)).fetchone() else "Unknown",
            conn=conn,
            betas=betas,
        )
        result = "APPROVED" if approved else "REJECTED"
        logger.info(f"Result: {result} | {reason} | Approved shares: {final_shares:.0f}")
        conn.close()
        return

    # ── Full risk check ──────────────────────────────────────────────── #

    # Load current portfolio weights
    from portfolio.state import portfolio_summary, get_positions
    from portfolio.beta import compute_betas

    summary  = portfolio_summary(conn)
    positions = get_positions(conn)

    weights: dict[str, float] = {}
    sectors: dict[str, float] = {}
    if not positions.empty:
        nav = _CFG["portfolio"]["nav"]
        for _, row in positions.iterrows():
            cp   = row["current_price"] or row["entry_price"] or 1
            sign = 1.0 if row["book"] == "long" else -1.0
            weights[row["ticker"]] = sign * row["shares"] * cp / nav
            sectors[row["ticker"]] = row["sector"] or "Unknown"

    # If no live positions, use latest target weights from combined scores
    if not weights:
        logger.info("No live positions — using latest combined scores as proxy weights")
        csv = Path(__file__).parent / "output" / "combined_scores_latest.csv"
        if csv.exists():
            df = pd.read_csv(csv)
            p  = _CFG["portfolio"]
            longs  = df[df["combined_signal"] == "LONG"].nlargest(p["num_longs"], "combined_score")
            shorts = df[df["combined_signal"] == "SHORT"].nsmallest(p["num_shorts"], "combined_score")
            n_l, n_s = len(longs), len(shorts)
            for _, row in longs.iterrows():
                weights[row["ticker"]] = p["target_long_gross"] / max(n_l, 1)
                sectors[row["ticker"]] = row.get("sector", "Unknown")
            for _, row in shorts.iterrows():
                weights[row["ticker"]] = -p["target_short_gross"] / max(n_s, 1)
                sectors[row["ticker"]] = row.get("sector", "Unknown")

    tickers = list(weights.keys())
    betas   = compute_betas(tickers, conn)

    # ── 1. Factor Risk Model ─────────────────────────────────────────── #
    _banner("FACTOR RISK MODEL")
    from risk.factor_risk_model import run_factor_model
    factor_model = run_factor_model(conn, weights=weights)
    if factor_model.get("portfolio_vol_pct"):
        logger.info(f"Portfolio vol: {factor_model['portfolio_vol_pct']:.1f}% annualized")
        logger.info(f"Factor var: {factor_model.get('portfolio_factor_var'):.4f}% | Specific: {factor_model.get('portfolio_specific_var'):.4f}%")
        flags = factor_model.get("mctr_flags", [])
        if flags:
            logger.warning(f"MCTR concentration flags: {len(flags)}")
            for f in flags:
                logger.warning(f"  {f['ticker']}: MCTR={f['mctr']:.3f}% ({f['ratio']:.1f}x weight)")

    # ── 2. Circuit Breakers ──────────────────────────────────────────── #
    _banner("CIRCUIT BREAKERS")
    from risk.circuit_breakers import check_circuit_breakers
    circuit_results = check_circuit_breakers(conn)
    if circuit_results:
        for r in circuit_results:
            logger.critical(f"  TRIGGERED: {r['breaker']} → {r['action']} | {r['detail']}")

    # ── 3. Tail Risk ─────────────────────────────────────────────────── #
    _banner("TAIL RISK")
    from risk.tail_risk import check_tail_risk
    tail = check_tail_risk(conn)
    if tail["triggers"]:
        for t in tail["triggers"]:
            logger.warning(f"  {t}")

    # ── 4. Factor Monitor ────────────────────────────────────────────── #
    _banner("FACTOR MONITOR")
    from risk.factor_monitor import monitor_factor_spreads
    from factors.crowding import detect_crowding
    crowding = detect_crowding(conn)
    factor_alerts = monitor_factor_spreads(weights, conn, crowding.get("warnings", []))
    for a in factor_alerts.get("alerts", []):
        logger.warning(f"  [{a['priority']}] {a['message']}")
    if not factor_alerts.get("alerts"):
        logger.info("  Factor spreads OK")

    # ── 5. Correlation Monitor ───────────────────────────────────────── #
    _banner("CORRELATION MONITOR")
    from risk.correlation_monitor import monitor_correlations
    corr_results = monitor_correlations(weights, conn)
    for book in ["long", "short"]:
        d = corr_results.get(book, {})
        if d:
            logger.info(
                f"  {book.title()} book: avg_corr={d.get('avg_corr', 0):.2f} "
                f"ENB={d.get('enb', 0):.1f}/{d.get('n_positions', 0)}"
            )
    for a in corr_results.get("alerts", []):
        logger.warning(f"  ! {a['message']}")

    # ── 6. Stress Tests ──────────────────────────────────────────────── #
    stress_results = []
    if args.stress:
        _banner("STRESS TESTS (6 SCENARIOS)")
        from risk.stress_test import run_stress_tests, print_stress_table
        factor_scores_for_stress = {}
        if factor_alerts.get("z_scores"):
            factor_scores_for_stress = {}  # use momentum scores from DB
        # Load momentum scores for reversal scenario
        try:
            import sqlite3 as _sq
            rows = conn.execute(
                """SELECT ticker, momentum FROM factor_scores fs
                   INNER JOIN (SELECT ticker, MAX(date) md FROM factor_scores GROUP BY ticker)
                   lp ON fs.ticker=lp.ticker AND fs.date=lp.md"""
            ).fetchall()
            factor_scores_for_stress = {r[0]: r[1] for r in rows}
        except Exception:
            pass
        stress_results = run_stress_tests(weights, sectors, factor_scores_for_stress, conn)
        print_stress_table(stress_results)

    # ── 7. Update Risk State ─────────────────────────────────────────── #
    _banner("RISK STATE UPDATE")
    from risk.risk_state import update_risk_state, print_risk_state
    state = update_risk_state(
        portfolio_summary  = summary,
        factor_model       = factor_model,
        circuit_breakers   = circuit_results,
        factor_alerts      = factor_alerts,
        correlation_alerts = corr_results,
        tail_risk          = tail,
        stress_results     = stress_results,
    )
    print_risk_state(state)

    conn.close()
    _banner("Done")


if __name__ == "__main__":
    main()
