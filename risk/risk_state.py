"""
Risk State — persistent JSON snapshot of current risk posture.
Updated after each run_risk_check.py execution.
Stored at: cache/risk_state.json
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

STATE_FILE = Path(__file__).parent.parent / _CFG["risk"]["risk_state_file"]


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = datetime.utcnow().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    logger.debug(f"Risk state saved → {STATE_FILE}")


def update_risk_state(
    portfolio_summary:  dict,
    factor_model:       dict,
    circuit_breakers:   list,
    factor_alerts:      dict,
    correlation_alerts: dict,
    tail_risk:          dict,
    stress_results:     Optional[list] = None,
    mctr_top_n:         int = 5,
):
    """Build and persist the complete risk state snapshot."""
    state = load_state()

    # Daily P&L tracking
    today = datetime.utcnow().strftime("%Y-%m-%d")
    history = state.get("pnl_history", {})
    history[today] = {
        "unrealized_pnl": portfolio_summary.get("unrealized_pnl", 0),
        "unrealized_pnl_pct": portfolio_summary.get("unrealized_pnl_pct", 0),
        "gross_exposure_pct": portfolio_summary.get("gross_exposure_pct", 0),
        "net_exposure_pct": portfolio_summary.get("net_exposure_pct", 0),
    }

    # Compute max drawdown from history
    pnl_series = [v.get("unrealized_pnl_pct", 0) for v in history.values()]
    peak = 0.0
    max_dd = 0.0
    for p in pnl_series:
        peak = max(peak, p)
        dd = p - peak
        max_dd = min(max_dd, dd)

    # Top MCTR positions
    mctr = factor_model.get("mctr", {})
    top_mctr = sorted(mctr.items(), key=lambda x: abs(x[1]), reverse=True)[:mctr_top_n]

    new_state = {
        "as_of":              today,
        "portfolio":          portfolio_summary,
        "drawdown_pct":       round(max_dd, 3),
        "pnl_history":        history,
        "factor_model": {
            "portfolio_vol_pct":      factor_model.get("portfolio_vol_pct"),
            "portfolio_factor_var":   factor_model.get("portfolio_factor_var"),
            "portfolio_specific_var": factor_model.get("portfolio_specific_var"),
            "mctr_flags":             factor_model.get("mctr_flags", []),
            "top_mctr":               [{"ticker": t, "mctr_pct": m} for t, m in top_mctr],
            "n_regression_days":      factor_model.get("n_days"),
        },
        "circuit_breakers": {
            "triggered":      circuit_breakers,
            "halt_active":    (Path(__file__).parent.parent / _CFG["risk"]["halt_lock_file"]).exists(),
        },
        "factor_alerts": {
            "count":   len(factor_alerts.get("alerts", [])),
            "alerts":  factor_alerts.get("alerts", []),
            "z_scores": factor_alerts.get("z_scores", {}),
        },
        "correlation": {
            "long":    correlation_alerts.get("long", {}),
            "short":   correlation_alerts.get("short", {}),
            "alerts":  correlation_alerts.get("alerts", []),
        },
        "tail_risk": {
            "action":   tail_risk.get("action"),
            "vix":      tail_risk.get("vix"),
            "credit_z": tail_risk.get("credit_z"),
            "triggers": tail_risk.get("triggers", []),
        },
        "stress_tests": stress_results or [],
        "overall_alert_level": _compute_alert_level(
            circuit_breakers, factor_alerts, tail_risk,
        ),
    }

    save_state(new_state)
    logger.info(
        f"Risk state updated: alert_level={new_state['overall_alert_level']} "
        f"drawdown={max_dd:.1%} vol={factor_model.get('portfolio_vol_pct', '?')}%"
    )
    return new_state


def _compute_alert_level(
    circuit_breakers: list,
    factor_alerts: dict,
    tail_risk: dict,
) -> str:
    if circuit_breakers:
        return "CRITICAL"
    if tail_risk.get("action") not in (None, "NONE"):
        return "HIGH"
    high_priority = [a for a in factor_alerts.get("alerts", []) if a.get("priority") == "HIGH"]
    if high_priority:
        return "HIGH"
    if factor_alerts.get("alerts"):
        return "MEDIUM"
    return "GREEN"


def print_risk_state(state: dict):
    """Pretty-print the current risk state."""
    print(f"\n{'='*60}")
    print(f"  RISK STATE — {state.get('as_of', 'N/A')}")
    print(f"  Alert Level: {state.get('overall_alert_level', '?')}")
    print(f"{'='*60}")

    p = state.get("portfolio", {})
    print(f"\nPortfolio:")
    print(f"  Gross: {p.get('gross_exposure_pct', 0):.1f}%   Net: {p.get('net_exposure_pct', 0):.1f}%")
    print(f"  Unrealized P&L: ${p.get('unrealized_pnl', 0):,.0f} ({p.get('unrealized_pnl_pct', 0):.2f}%)")
    print(f"  Max Drawdown:   {state.get('drawdown_pct', 0):.1%}")

    fm = state.get("factor_model", {})
    if fm.get("portfolio_vol_pct"):
        print(f"\nFactor Risk Model:")
        print(f"  Portfolio Vol:  {fm['portfolio_vol_pct']:.1f}% annualized")
        if fm.get("top_mctr"):
            print(f"  Top MCTR:")
            for item in fm["top_mctr"][:3]:
                print(f"    {item['ticker']}: {item['mctr_pct']:.3f}%")

    tr = state.get("tail_risk", {})
    print(f"\nTail Risk: VIX={tr.get('vix', 'N/A')} | Action={tr.get('action', 'NONE')}")

    fa = state.get("factor_alerts", {})
    print(f"Factor Alerts: {fa.get('count', 0)}")

    cb = state.get("circuit_breakers", {})
    if cb.get("triggered"):
        print(f"\n!!! CIRCUIT BREAKERS: {len(cb['triggered'])} TRIGGERED !!!")
    if cb.get("halt_active"):
        print("!!! HALT LOCK IS ACTIVE — ALL TRADING SUSPENDED !!!")
