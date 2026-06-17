"""
Earnings Call Transcript Analyzer.
Scores management quality across 6 dimensions (1-10 each).
Requires FMP_API_KEY; returns None gracefully if no transcript available.
"""

import hashlib
import logging
import sqlite3
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

_MAX_CHARS = _CFG["analysis"]["transcript_max_chars"]
_ANALYZER  = "earnings_call"

_SYSTEM = """You are a senior equity analyst at a long/short hedge fund specializing in forensic analysis of earnings call transcripts. Your job is to extract signal — not narrative — from management communication.

You evaluate transcripts across six dimensions, each scored 1-10:

1. MANAGEMENT CONFIDENCE (1=evasive/deflecting, 10=specific/accountable)
   Look for: specificity of guidance, willingness to address hard questions, ownership of misses, concrete plans vs platitudes.

2. REVENUE GUIDANCE (1=withdrawn/vague, 10=raised/specific with drivers)
   Look for: whether guidance was raised/maintained/lowered, qualitative color on demand trends, channel inventory, pricing power, and customer concentration.

3. MARGIN TRAJECTORY (1=deteriorating with no path, 10=expanding with clear catalysts)
   Look for: gross/operating margin commentary, cost structure, leverage potential, and mix shift dynamics.

4. COMPETITIVE POSITION (1=losing share/under pressure, 10=gaining share/widening moat)
   Look for: market share commentary, competitive wins/losses, product differentiation, pricing power vs peers.

5. RISK FACTORS (1=many undisclosed risks, 10=well-managed with clear mitigations)
   Look for: proactive risk disclosure, hedging, concentration risks, regulatory exposure, and supply chain vulnerabilities.

6. CAPITAL ALLOCATION (1=value-destructive, 10=shareholder-aligned and disciplined)
   Look for: buyback discipline (price sensitivity), dividend sustainability, M&A rationale, FCF conversion commentary.

SCORING DISCIPLINE:
- 8-10: Genuinely excellent — reserve for best-in-class
- 6-7:  Above average — solid with minor concerns
- 4-5:  Average — mixed signals
- 2-3:  Below average — meaningful concerns
- 1:    Red flag — evasive, deteriorating, or misleading

Extract verbatim quotes that most clearly support your bull and bear cases.

Output ONLY valid JSON matching this schema exactly:
{
  "management_confidence": {"score": <int 1-10>, "reasoning": "<2-3 sentences>"},
  "revenue_guidance": {"score": <int 1-10>, "reasoning": "<2-3 sentences>"},
  "margin_trajectory": {"score": <int 1-10>, "reasoning": "<2-3 sentences>"},
  "competitive_position": {"score": <int 1-10>, "reasoning": "<2-3 sentences>"},
  "risk_factors": {"score": <int 1-10>, "reasoning": "<2-3 sentences>"},
  "capital_allocation": {"score": <int 1-10>, "reasoning": "<2-3 sentences>"},
  "composite_score": <float, avg of 6 scores>,
  "bull_case": "<2-3 sentences of strongest positive signals>",
  "bear_case": "<2-3 sentences of most concerning signals>",
  "key_quotes": ["<verbatim quote 1>", "<verbatim quote 2>", "<verbatim quote 3>"],
  "one_line_summary": "<single sentence: tone, key takeaway, and signal direction>"
}"""


def _load_transcript(ticker: str, conn: sqlite3.Connection) -> Optional[tuple[str, str]]:
    """
    Returns (content, artifact_id) or None if no transcript.
    artifact_id is a hash of the content for cache keying.
    """
    row = conn.execute(
        "SELECT content, quarter, year FROM earnings_transcripts WHERE ticker=? ORDER BY year DESC, quarter DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if not row or not row[0]:
        return None
    content = row[0][:_MAX_CHARS]
    artifact_id = hashlib.sha256(content.encode()).hexdigest()[:16]
    return content, artifact_id


def analyze(
    ticker: str,
    client: ClaudeClient,
    tracker: CostTracker,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """
    Analyze the most recent earnings call transcript for a ticker.
    Returns scored dict or None if no transcript available.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        result = _load_transcript(ticker, conn)
        if result is None:
            logger.info(f"[{_ANALYZER}] {ticker}: no transcript available")
            return None

        content, artifact_id = result

        # Cache check
        cached = cache_get(_ANALYZER, ticker, artifact_id)
        if cached:
            logger.info(f"[{_ANALYZER}] {ticker}: cache hit")
            return cached

        user_prompt = (
            f"Analyze this earnings call transcript for {ticker}.\n\n"
            f"TRANSCRIPT:\n{content}"
        )

        result_dict, usage = client.call_json(_SYSTEM, user_prompt)
        tracker.record(usage, client.model, label=f"{_ANALYZER}/{ticker}")

        if result_dict is None:
            logger.error(f"[{_ANALYZER}] {ticker}: JSON extraction failed")
            return None

        result_dict["ticker"] = ticker
        result_dict["analyzer"] = _ANALYZER
        cache_set(_ANALYZER, ticker, artifact_id, result_dict)
        logger.info(f"[{_ANALYZER}] {ticker}: composite={result_dict.get('composite_score', '?'):.1f}")
        return result_dict

    finally:
        if close_conn:
            conn.close()
