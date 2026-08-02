"""
Meridian Capital Partners — Backtest Runner

Two modes:
  retrospective (default)
      Takes the current LONG/SHORT signals from the most recent scoring run
      and measures how those exact baskets performed over the last N trading
      days using stored price data. No lookahead bias — signals are fixed.

  walk-forward (--walk-forward)
      Requires multiple scoring runs. Uses each historical signal date as an
      entry, holds until the next signal date, then re-signals. Useful once
      run_scoring.py has been running daily for several weeks.

Usage:
    python run_backtest.py                     # retrospective, top 10, last 14 days
    python run_backtest.py --top-n 20          # widen to top 20 each side
    python run_backtest.py --days 30           # extend lookback to 30 trading days
    python run_backtest.py --walk-forward      # walk-forward across all scoring dates
    python run_backtest.py --save              # also write JSON report to output/
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from data.db import get_connection, init_db
from backtest.engine import run_retrospective_backtest, run_walk_forward_backtest


# ── Formatting helpers ────────────────────────────────────────────────── #

def _sign(val: float) -> str:
    return f"+{val:.2f}" if val >= 0 else f"{val:.2f}"


def _bar(val: float, width: int = 24) -> str:
    """ASCII bar scaled to ±width chars."""
    filled = min(int(abs(val)), width)
    if val >= 0:
        return "[" + "#" * filled + " " * (width - filled) + "]"
    else:
        return "[" + " " * (width - filled) + "#" * filled + "]"


def _fmt(val, suffix="", decimals=2) -> str:
    if val is None:
        return "N/A"
    return f"{val:+.{decimals}f}{suffix}" if val >= 0 else f"{val:.{decimals}f}{suffix}"


def _print_stat_block(stats: dict):
    if not stats or stats.get("n_days", 0) == 0:
        print("    — insufficient data —")
        return
    tr   = stats.get("total_return_pct")
    ann  = stats.get("ann_return_pct")
    vol  = stats.get("ann_vol_pct")
    sh   = stats.get("sharpe_ratio")
    dd   = stats.get("max_drawdown_pct")
    wr   = stats.get("win_rate_pct")
    n    = stats.get("n_days")
    if tr is None:
        print("    — no price data for this ticker —")
        return
    print(f"    Total return    : {_fmt(tr, '%')}   {_bar(tr)}")
    print(f"    Ann. return     : {_fmt(ann, '%')}")
    print(f"    Ann. volatility : {vol:.1f}%" if vol is not None else "    Ann. volatility : N/A")
    print(f"    Sharpe ratio    : {sh:.2f}" if sh is not None else "    Sharpe ratio    : N/A")
    print(f"    Max drawdown    : {_fmt(dd, '%')}")
    print(f"    Win rate        : {wr:.0f}%  ({n} trading days)")


def _print_individual(stocks: list[dict], label: str, invert: bool = False):
    """Print per-stock table. invert=True means negative price move is good (shorts)."""
    print(f"\n  {label}")
    print("  " + "-" * 40)
    for r in stocks:
        raw = r["return_pct"]
        effective = -raw if invert else raw
        bar_val   = effective
        flag = " *WINNER*" if effective > 0 else ("  loser" if effective < -3 else "")
        print(f"    {r['ticker']:<6}  {_sign(raw)}%  {_bar(bar_val, 16)}{flag}")


# ── Main ──────────────────────────────────────────────────────────────── #

def main():
    parser = argparse.ArgumentParser(
        description="Meridian Capital Partners — Backtest Runner"
    )
    parser.add_argument("--top-n", type=int, default=10,
                        help="Top N long + short candidates (default: 10)")
    parser.add_argument("--days", type=int, default=14,
                        help="Trading days to look back (default: 14)")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Use walk-forward mode across all scoring dates")
    parser.add_argument("--save", action="store_true",
                        help="Save JSON report to output/backtest_YYYYMMDD_HHMM.json")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    sep = "=" * 72
    print(f"\n{sep}")
    print("  Meridian Capital Partners — Backtest Report")
    print(f"  Generated : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    if args.walk_forward:
        print("  Mode      : Walk-forward (re-signals at each scoring date)")
    else:
        print(f"  Mode      : Retrospective | Top {args.top_n} L/S | {args.days} trading days")
    print(sep)

    # ── Run backtest ─────────────────────────────────────────────────── #
    if args.walk_forward:
        result = run_walk_forward_backtest(conn, top_n=args.top_n)
    else:
        result = run_retrospective_backtest(conn, top_n=args.top_n, lookback_days=args.days)

    conn.close()

    if "error" in result:
        print(f"\n  ERROR: {result['error']}\n")
        sys.exit(1)

    # ── Walk-forward report ───────────────────────────────────────────── #
    if result["mode"] == "walk_forward":
        periods = result["periods"]
        print(f"\n  Scoring dates in DB : {', '.join(result['scoring_dates'])}")
        print(f"  Complete periods    : {result['n_periods']}\n")
        print(f"  {'Signal Date':<14} {'Hold Until':<14} {'Long':>8} {'Short':>8} {'L/S':>8} {'SPY':>8}")
        print("  " + "-" * 62)
        for p in periods:
            lr  = f"{_sign(p['long_return'] * 100)}%" if p["long_return"] is not None else "N/A"
            sr  = f"{_sign(p['short_return'] * 100)}%" if p["short_return"] is not None else "N/A"
            lsr = f"{_sign(p['ls_return'] * 100)}%"    if p["ls_return"]   is not None else "N/A"
            spy = f"{_sign(p['spy_return'] * 100)}%"   if p["spy_return"]  is not None else "N/A"
            print(f"  {p['signal_date']:<14} {p['hold_until']:<14} {lr:>8} {sr:>8} {lsr:>8} {spy:>8}")
        print()

    # ── Retrospective report ──────────────────────────────────────────── #
    else:
        period = result["period"]
        print(f"\n  Signals as of  : {result['scoring_date']}")
        print(f"  Period         : {period['start']} -> {period['end']}")
        print(f"  Universe       : {result['n_longs']} longs, {result['n_shorts']} shorts")

        note = (
            "\n  NOTE: The L/S spread is dollar-neutral (2x gross leverage).\n"
            "  Longs gain from price appreciation; shorts gain from price declines.\n"
            "  'Short Basket (price)' shows raw stock returns — your P&L is the inverse."
        )
        print(note)

        print(f"\n{sep}")
        print("  LONG BASKET  (equal-weight, buy-and-hold)")
        print(sep)
        _print_stat_block(result["long_basket"])

        print(f"\n{sep}")
        print("  SHORT BASKET — raw price return (your P&L is the inverse)")
        print(sep)
        _print_stat_block(result["short_basket_raw"])

        sb = result["short_basket_raw"]
        if sb and sb.get("total_return_pct") is not None:
            inv_ret = -sb["total_return_pct"]
            print(f"\n  => Your short P&L:  {_sign(inv_ret)}%  (short basket fell, so you profited)")

        print(f"\n{sep}")
        print("  LONG/SHORT SPREAD  (long basket - short basket, dollar-neutral)")
        print(sep)
        _print_stat_block(result["long_short_spread"])

        print(f"\n{sep}")
        print("  SPY BENCHMARK")
        print(sep)
        _print_stat_block(result["spy_benchmark"])

        # ── Individual stock performance ──────────────────────────────── #
        print(f"\n{sep}")
        print("  INDIVIDUAL LONG POSITIONS  (higher = better)")
        print(sep)
        _print_individual(result["individual_longs"], "All longs ranked by return", invert=False)

        print(f"\n{sep}")
        print("  INDIVIDUAL SHORT POSITIONS  (lower price = better for you)")
        print(sep)
        shorts_sorted_by_effectiveness = sorted(
            result["individual_shorts"], key=lambda x: x["return_pct"]
        )
        _print_individual(shorts_sorted_by_effectiveness, "All shorts ranked (most declined first)", invert=True)

        # ── Alpha estimate ────────────────────────────────────────────── #
        ls  = result["long_short_spread"]
        spy = result["spy_benchmark"]
        print(f"\n{sep}")
        print("  ALPHA ESTIMATE (L/S at 1x, vs SPY)")
        print(sep)
        ls_half = ls["total_return_pct"] / 2 if ls.get("total_return_pct") is not None else None
        spy_ret = spy.get("total_return_pct")
        print(f"    L/S at 1x leverage : {_fmt(ls_half, '%') if ls_half is not None else 'N/A'}")
        print(f"    SPY                : {_fmt(spy_ret, '%') if spy_ret is not None else 'N/A  (SPY not in price DB — run run_data.py)'}")
        if ls_half is not None and spy_ret is not None:
            alpha = ls_half - spy_ret
            interpretation = "outperformed" if alpha > 0 else "underperformed"
            print(f"    Raw alpha          : {_fmt(alpha, '%')}")
            print(f"\n    => Portfolio {interpretation} SPY by {abs(alpha):.2f}pp over the period.")
        elif ls_half is not None:
            print(f"\n    => L/S delivered {_fmt(ls_half, '%')} at 1x leverage.")
            print("       Run run_data.py to add SPY prices for benchmark comparison.")
        print()

    # ── Save JSON ─────────────────────────────────────────────────────── #
    if args.save:
        out_dir  = Path(__file__).parent / "output"
        out_dir.mkdir(exist_ok=True)
        stamp    = datetime.utcnow().strftime("%Y%m%d_%H%M")
        out_path = out_dir / f"backtest_{stamp}.json"
        out_path.write_text(json.dumps(result, indent=2, default=str))
        print(f"  JSON report saved: {out_path}\n")


if __name__ == "__main__":
    main()
