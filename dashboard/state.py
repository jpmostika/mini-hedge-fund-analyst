"""
System state builder — compiles ~19KB JSON snapshot of Meridian's current state
to be sent as cached context to JARVIS (Claude) for the chat interface.
"""

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_CFG = yaml.safe_load(_CONFIG_PATH.read_text())
NAV  = _CFG["portfolio"]["nav"]


def build_system_state(conn: sqlite3.Connection) -> dict:
    """Compile a comprehensive snapshot of all system state for JARVIS context."""
    state: dict = {
        "generated_at": datetime.utcnow().isoformat(),
        "fund":         _CFG["project"]["name"],
        "nav_usd":      NAV,
    }

    # ── Universe stats ──────────────────────────────────────────────── #
    try:
        n_equity  = conn.execute("SELECT COUNT(*) FROM universe WHERE type='equity'").fetchone()[0]
        state["universe"] = {"total_equities": n_equity}
    except Exception:
        pass

    # ── Scores ──────────────────────────────────────────────────────── #
    try:
        csv = Path(__file__).parent.parent / "output" / "combined_scores_latest.csv"
        if csv.exists():
            df = pd.read_csv(csv)
            state["scores"] = {
                "total_scored": len(df),
                "long_candidates": int((df["combined_signal"] == "LONG").sum()),
                "short_candidates": int((df["combined_signal"] == "SHORT").sum()),
                "top_5_longs": df[df["combined_signal"] == "LONG"]
                    .nlargest(5, "combined_score")[["ticker", "sector", "combined_score"]]
                    .to_dict("records"),
                "top_5_shorts": df[df["combined_signal"] == "SHORT"]
                    .nsmallest(5, "combined_score")[["ticker", "sector", "combined_score"]]
                    .to_dict("records"),
                "last_updated": df.get("score_date", pd.Series([None])).iloc[0]
                    if "score_date" in df.columns else None,
            }
    except Exception:
        pass

    # ── Portfolio ───────────────────────────────────────────────────── #
    try:
        from portfolio.state import portfolio_summary, get_positions
        summary   = portfolio_summary(conn)
        positions = get_positions(conn)
        state["portfolio"] = summary
        if not positions.empty:
            state["positions"] = positions[
                ["ticker", "book", "shares", "current_price", "unrealized_pnl", "sector"]
            ].to_dict("records")
    except Exception:
        pass

    # ── Risk state ──────────────────────────────────────────────────── #
    try:
        risk_file = Path(__file__).parent.parent / _CFG["risk"]["risk_state_file"]
        if risk_file.exists():
            risk = json.loads(risk_file.read_text())
            state["risk"] = {
                "alert_level":    risk.get("overall_alert_level"),
                "vix":            risk.get("tail_risk", {}).get("vix"),
                "tail_action":    risk.get("tail_risk", {}).get("action"),
                "circuit_breakers": risk.get("circuit_breakers", {}).get("triggered", []),
                "halt_active":    risk.get("circuit_breakers", {}).get("halt_active", False),
                "portfolio_vol":  risk.get("factor_model", {}).get("portfolio_vol_pct"),
                "drawdown":       risk.get("drawdown_pct"),
                "factor_alerts":  risk.get("factor_alerts", {}).get("alerts", []),
                "mctr_flags":     risk.get("factor_model", {}).get("mctr_flags", []),
                "top_mctr":       risk.get("factor_model", {}).get("top_mctr", []),
                "correlation":    {
                    "long_avg_corr":  risk.get("correlation", {}).get("long", {}).get("avg_corr"),
                    "short_avg_corr": risk.get("correlation", {}).get("short", {}).get("avg_corr"),
                    "long_enb":       risk.get("correlation", {}).get("long", {}).get("enb"),
                },
                "stress_worst":   _worst_stress(risk.get("stress_tests", [])),
            }
    except Exception:
        pass

    # ── Pending approvals ───────────────────────────────────────────── #
    try:
        from portfolio.state import get_pending_approvals
        approvals = get_pending_approvals(conn)
        state["pending_trades"] = len(approvals) if not approvals.empty else 0
    except Exception:
        state["pending_trades"] = 0

    # ── Upcoming earnings ───────────────────────────────────────────── #
    try:
        upcoming = conn.execute(
            """SELECT ticker, earnings_date FROM earnings_calendar
               WHERE earnings_date BETWEEN date('now') AND date('now','+7 days')
               ORDER BY earnings_date LIMIT 10"""
        ).fetchall()
        state["earnings_next_7d"] = [{"ticker": r[0], "date": r[1]} for r in upcoming]
    except Exception:
        pass

    # ── Insider activity summary ─────────────────────────────────────── #
    try:
        ceo_buys = conn.execute(
            """SELECT COUNT(*) FROM insider_transactions
               WHERE is_ceo_cfo=1 AND transaction_code='P'
               AND date >= date('now','-30 days')"""
        ).fetchone()[0]
        cluster_tickers = conn.execute(
            """SELECT DISTINCT ticker FROM insider_transactions
               WHERE is_open_market=1 AND date >= date('now','-30 days')
               GROUP BY ticker HAVING COUNT(DISTINCT insider_name) >= 3"""
        ).fetchall()
        state["insider_30d"] = {
            "ceo_cfo_buys": ceo_buys,
            "cluster_buy_tickers": [r[0] for r in cluster_tickers],
        }
    except Exception:
        pass

    # ── Factor exposures ─────────────────────────────────────────────── #
    try:
        factor_scores = pd.read_sql(
            """SELECT ticker, momentum, value, quality, growth, revisions,
                      short_interest, insider, institutional, composite, signal
               FROM factor_scores fs INNER JOIN (
                   SELECT ticker, MAX(date) md FROM factor_scores GROUP BY ticker
               ) lp ON fs.ticker=lp.ticker AND fs.date=lp.md
               WHERE signal IN ('LONG','SHORT')""",
            conn,
        )
        if not factor_scores.empty:
            state["factor_averages"] = {
                "longs":  factor_scores[factor_scores["signal"] == "LONG"]
                    [["momentum","value","quality","growth"]].mean().round(1).to_dict(),
                "shorts": factor_scores[factor_scores["signal"] == "SHORT"]
                    [["momentum","value","quality","growth"]].mean().round(1).to_dict(),
            }
    except Exception:
        pass

    # ── Attribution history ──────────────────────────────────────────── #
    try:
        from reporting.pnl_attribution import load_attribution_history
        attr = load_attribution_history()
        if not attr.empty:
            recent = attr.tail(5)[["date", "port_return", "alpha_return", "beta_return"]].to_dict("records")
            state["attribution_recent_5d"] = recent
    except Exception:
        pass

    return state


def _worst_stress(stress_tests: list) -> Optional[dict]:
    if not stress_tests:
        return None
    return min(stress_tests, key=lambda x: x.get("total_pct", 0))


def state_to_context(state: dict) -> str:
    """Compact JSON string for Claude context (<20KB)."""
    text = json.dumps(state, indent=None, default=str)
    # Trim to ~19KB if needed
    if len(text) > 19_000:
        text = text[:19_000] + '...(truncated)}'
    return text
