"""
JARVIS Commentary Engine.
  - Weekly commentary (fires on configured weekday, default Friday)
  - Daily LP letter (3-4 paragraphs, letterhead, compliance footer)
Both authored by Claude in the JARVIS analyst persona.
Results are cached by date.
"""

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_R_CFG = _CFG["reporting"]
_D_CFG = _CFG.get("dashboard", {})
_P_CFG = _CFG["portfolio"]

LETTER_CACHE = Path(__file__).parent.parent / _R_CFG["letter_cache"]
LETTER_CACHE.mkdir(parents=True, exist_ok=True)

FUND_NAME      = _R_CFG["fund_name"]
FUND_DOMICILE  = _R_CFG["fund_domicile"]
FUND_INCEPTION = _R_CFG["fund_inception"]

_JARVIS_PERSONA = """You are JARVIS — the AI analyst for Meridian Capital Partners,
a long/short equity hedge fund. You are precise, institutional, and data-driven.
Your voice is that of a senior PM who has seen multiple market cycles. You write
in tight, declarative sentences with no filler. You cite specific numbers. You do
not use phrases like "it's worth noting" or "in conclusion." Every paragraph must
add new information. You sign off as JARVIS."""


def _call_claude(system: str, user: str) -> str:
    """Direct Anthropic API call for report generation."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        resp = client.messages.create(
            model=_CFG["analysis"]["model"],
            max_tokens=2048,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text if resp.content else ""
    except Exception as e:
        logger.error(f"Claude call failed: {e}")
        return ""


def generate_weekly_commentary(risk_state: dict, attribution_history, top_longs: list, top_shorts: list) -> str:
    """Generate JARVIS weekly market commentary. Returns markdown string."""
    today = date.today()
    cache_path = LETTER_CACHE / f"weekly_{today.isocalendar()[1]}_{today.year}.md"
    if cache_path.exists():
        return cache_path.read_text()

    context = {
        "date":       today.isoformat(),
        "risk_level": risk_state.get("overall_alert_level", "GREEN"),
        "vix":        risk_state.get("tail_risk", {}).get("vix"),
        "portfolio_vol": risk_state.get("factor_model", {}).get("portfolio_vol_pct"),
        "top_longs":  [t.get("ticker") for t in (top_longs or [])[:5]],
        "top_shorts": [t.get("ticker") for t in (top_shorts or [])[:5]],
        "factor_alerts": len(risk_state.get("factor_alerts", {}).get("alerts", [])),
    }

    user_msg = (
        f"Write a 3-paragraph weekly market commentary for {FUND_NAME}. "
        f"Context: {json.dumps(context, indent=2)}\n\n"
        "Cover: (1) market environment and VIX regime, "
        "(2) what drove portfolio performance this week, "
        "(3) key risks and positioning for next week. "
        "Be specific. Use numbers. Write in JARVIS voice."
    )

    text = _call_claude(_JARVIS_PERSONA, user_msg)
    if text:
        cache_path.write_text(text)
    return text or "_Weekly commentary not available — ANTHROPIC_API_KEY required._"


def generate_lp_letter(risk_state: dict, attribution: dict, nav: float = _P_CFG["nav"]) -> str:
    """
    Generate daily LP letter. Cached by date.
    Returns full markdown with letterhead, body, signature, compliance footer.
    """
    today = date.today()
    cache_path = LETTER_CACHE / f"lp_letter_{today.isoformat()}.md"
    if cache_path.exists():
        return cache_path.read_text()

    doc_id    = f"MCP-IM-{today.year}-{today.strftime('%m%d')}"
    inception = _R_CFG["fund_inception"]

    # Generate body paragraphs from Claude
    context = {
        "date":         today.isoformat(),
        "nav_usd":      f"${nav:,.0f}",
        "daily_pnl":    attribution.get("port_pnl_usd", 0),
        "daily_return": attribution.get("port_return", 0),
        "alpha_pnl":    attribution.get("alpha_pnl_usd", 0),
        "risk_level":   risk_state.get("overall_alert_level", "GREEN"),
        "vix":          risk_state.get("tail_risk", {}).get("vix"),
        "drawdown":     risk_state.get("drawdown_pct", 0),
        "factor_vol":   risk_state.get("factor_model", {}).get("portfolio_vol_pct"),
        "circuit_breakers_triggered": len(risk_state.get("circuit_breakers", {}).get("triggered", [])),
    }

    user_msg = (
        f"Write 3-4 paragraphs for a daily LP letter for {FUND_NAME}. "
        f"Context: {json.dumps(context, indent=2)}\n\n"
        "Start directly with content (no 'Dear Limited Partners' — that's added separately). "
        "Cover: (1) daily performance and attribution, "
        "(2) key positions driving results, "
        "(3) risk posture and any alerts, "
        "(4) outlook for next session. "
        "Be precise and institutional. JARVIS voice."
    )

    body = _call_claude(_JARVIS_PERSONA, user_msg)
    if not body:
        body = "_Letter body not available — ANTHROPIC_API_KEY required._"

    letter = f"""---

**{FUND_NAME}**
*{FUND_DOMICILE} Limited Partnership · Inception {inception}*
*AUM: ${nav:,.0f} · Doc ID: {doc_id} · {today.strftime('%B %d, %Y')}*

> **CONFIDENTIAL · LIMITED PARTNERS ONLY**

---

Dear Limited Partners,

{body}

Sincerely,

**JARVIS**
*AI Analyst, {FUND_NAME}*

---

*This communication is intended solely for the named recipient(s) and may contain
confidential and privileged information. Any unauthorized review, use, disclosure,
or distribution is prohibited. Past performance is not indicative of future results.
This is not an offer to sell or a solicitation to buy any security.*

*{FUND_NAME} is not registered as an investment adviser with the SEC.*
*{doc_id}*
"""

    cache_path.write_text(letter)
    return letter


def clear_letter_cache(as_of: Optional[str] = None):
    """Delete cached letter for a specific date (or today) to force regeneration."""
    d = as_of or date.today().isoformat()
    p = LETTER_CACHE / f"lp_letter_{d}.md"
    if p.exists():
        p.unlink()
        logger.info(f"Letter cache cleared for {d}")
