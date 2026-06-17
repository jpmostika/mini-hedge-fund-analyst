"""
Sector Analysis — synthesizes all Claude analyzer results within a sector,
ranks stocks by fundamental quality, and surfaces the top long/short idea.
"""

import hashlib
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

from analysis.api_client import ClaudeClient
from analysis.cache import cache_get, cache_set
from analysis.cost_tracker import CostTracker
from data.db import get_connection

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_ANALYZER = "sector_analysis"

_SYSTEM = """You are the head of fundamental research at a long/short equity hedge fund. You synthesize individual stock analysis into sector-level convictions.

Given a set of stocks in the same GICS sector with their quantitative scores and qualitative Claude analysis summaries, you must:

1. RANK stocks from highest to lowest fundamental quality
2. Identify the BEST LONG candidate (strongest fundamental + momentum combination)
3. Identify the BEST SHORT candidate (weakest fundamentals, most accounting concerns, or deteriorating competitive position)
4. Provide a SECTOR OUTLOOK on the macro/fundamental environment for this sector

RANKING CRITERIA (in priority order):
1. Accounting quality (no red flags is table stakes)
2. Earnings quality (cash conversion, low accruals)
3. Revenue and earnings growth trajectory
4. Competitive position (moat, pricing power)
5. Management quality (from transcript analysis)
6. Insider signal (CEO buying = conviction)
7. Quantitative composite score (momentum, value, quality factors)

SHORT CANDIDATES should have at least one of: high accruals, declining margins, elevated insider selling, deteriorating competitive position, or HIGH/CRITICAL risk severity.

Output ONLY valid JSON:
{
  "sector": "<sector name>",
  "rankings": [
    {"ticker": "<ticker>", "rank": <int>, "reasoning": "<2-3 sentences>", "signal": "<LONG|NEUTRAL|SHORT>"}
  ],
  "top_long_idea": {
    "ticker": "<ticker>",
    "thesis": "<3-4 sentence bull thesis>",
    "key_catalysts": ["<catalyst 1>", "<catalyst 2>"],
    "key_risks": ["<risk 1>", "<risk 2>"]
  },
  "top_short_idea": {
    "ticker": "<ticker>",
    "thesis": "<3-4 sentence bear thesis>",
    "key_triggers": ["<trigger 1>", "<trigger 2>"],
    "what_would_make_me_wrong": "<1-2 sentences>"
  },
  "sector_outlook": "<2-3 sentences on the macro/fundamental backdrop for this sector>",
  "sector_positioning": "<OVERWEIGHT|NEUTRAL|UNDERWEIGHT>"
}"""


def analyze_sector(
    sector: str,
    tickers_data: list[dict],
    client: ClaudeClient,
    tracker: CostTracker,
) -> Optional[dict]:
    """
    tickers_data: list of dicts with {ticker, composite_score, signal,
                   earnings_summary, filing_summary, risk_summary, insider_summary}
    """
    if not tickers_data:
        return None

    # Build artifact_id from tickers + today's date (re-analyze daily)
    tickers_str = "-".join(sorted(d["ticker"] for d in tickers_data))
    artifact_id = hashlib.sha256(
        f"{tickers_str}-{date.today().isoformat()}".encode()
    ).hexdigest()[:16]

    cached = cache_get(_ANALYZER, sector.replace(" ", "_"), artifact_id)
    if cached:
        logger.info(f"[{_ANALYZER}] {sector}: cache hit")
        return cached

    # Format the data for Claude
    lines = [f"SECTOR: {sector}", f"STOCKS TO RANK: {len(tickers_data)}", ""]
    for item in sorted(tickers_data, key=lambda x: x.get("composite_score", 50), reverse=True):
        ticker = item["ticker"]
        lines.append(f"--- {ticker} ---")
        lines.append(f"Quant Composite Score: {item.get('composite_score', 'N/A'):.1f}/100")
        lines.append(f"Quant Signal: {item.get('signal', 'N/A')}")
        if item.get("earnings_summary"):
            lines.append(f"Earnings Call: {item['earnings_summary']}")
        if item.get("filing_summary"):
            lines.append(f"Accounting Quality: {item['filing_summary']}")
        if item.get("risk_summary"):
            lines.append(f"Risk Profile: {item['risk_summary']}")
        if item.get("insider_summary"):
            lines.append(f"Insider Activity: {item['insider_summary']}")
        lines.append("")

    user_prompt = (
        f"Rank these {sector} sector stocks by fundamental quality and identify "
        f"the best long and short ideas. Synthesize the quantitative scores "
        f"with the qualitative analysis summaries.\n\n"
        + "\n".join(lines)
    )

    result_dict, usage = client.call_json(_SYSTEM, user_prompt)
    tracker.record(usage, client.model, label=f"{_ANALYZER}/{sector}")

    if result_dict is None:
        logger.error(f"[{_ANALYZER}] {sector}: JSON extraction failed")
        return None

    result_dict["sector"] = sector
    result_dict["analyzer"] = _ANALYZER
    cache_set(_ANALYZER, sector.replace(" ", "_"), artifact_id, result_dict)
    logger.info(
        f"[{_ANALYZER}] {sector}: "
        f"long={result_dict.get('top_long_idea', {}).get('ticker')} "
        f"short={result_dict.get('top_short_idea', {}).get('ticker')} "
        f"outlook={result_dict.get('sector_positioning')}"
    )
    return result_dict
