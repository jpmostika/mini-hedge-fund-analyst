"""
10-K Risk Factor Analyzer.
Extracts the Risk Factors section from cached 10-K filings, strips HTML boilerplate,
and flags new/material risks vs the prior year's filing.
Returns None if no 10-K is cached.
"""

import hashlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

import yaml

from analysis.api_client import ClaudeClient
from analysis.cache import cache_get, cache_set
from analysis.cost_tracker import CostTracker
from data.db import get_connection

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_MAX_CHARS = _CFG["analysis"]["risk_max_chars"]
_ANALYZER  = "risk_factors"

_SYSTEM = """You are a senior credit and equity risk analyst at a long/short hedge fund. Your job is to read 10-K Risk Factor sections and extract the signal that matters — distinguishing genuinely new or material risks from boilerplate legal language that appears in every filing.

YOUR FRAMEWORK:

BOILERPLATE vs MATERIAL:
Boilerplate = risks every company in every industry lists (cybersecurity in general, macroeconomic conditions in general, competition in general, regulatory changes in general). These have near-zero discriminatory power.
Material = company-specific, quantified, or newly disclosed risks with a plausible near-term impact on earnings, liquidity, or legal standing.

NEW RISKS (highest priority):
Risks that appear in this filing but not in prior filings — this is the most important signal. Management is required to disclose; new disclosures are almost always meaningful.

SEVERITY CLASSIFICATION:
- CRITICAL: Existential threat (solvency, regulatory shutdown, major litigation with significant damages)
- HIGH: Could cause >20% EPS impact within 2 years if materialized
- MEDIUM: Meaningful but manageable; likely priced in
- LOW: Minor; standard course of business

You must be specific. Vague language ("risks may adversely affect results") should be classified as boilerplate. Specific language ("we face potential fines of up to $X billion", "our largest customer represents 34% of revenue") is material.

Output ONLY valid JSON:
{
  "new_risks": [
    {"risk": "<concise description>", "severity": "<CRITICAL|HIGH|MEDIUM|LOW>", "excerpt": "<verbatim relevant phrase>"}
  ],
  "material_risks": [
    {"risk": "<concise description>", "severity": "<CRITICAL|HIGH|MEDIUM|LOW>", "excerpt": "<verbatim relevant phrase>"}
  ],
  "boilerplate_percentage": <int 0-100, estimate of what percent of the section is boilerplate>,
  "risk_severity": "<CRITICAL|HIGH|MEDIUM|LOW — overall assessment>",
  "top_risk": "<single most important risk in one sentence>",
  "one_line_summary": "<overall risk profile assessment in one sentence, including any new risks>"
}"""


def _strip_html(text: str) -> str:
    """Remove HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)  # HTML entities
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_risk_section(filing_text: str) -> Optional[str]:
    """Extract the Risk Factors section from a 10-K filing.

    Strips HTML *first* so tags interspersed within headings (e.g.
    "Item</b>&nbsp;1A") don't break the match, then takes the longest matching
    span — the table-of-contents entry yields a tiny stub, the real section is
    large, so "longest wins" reliably skips the TOC.
    """
    clean = _strip_html(filing_text)
    patterns = [
        r"(?i)item\s+1a\.?\s*risk\s+factors(.*?)(?=item\s+1b)",
        r"(?i)item\s+1a\.?\s*risk\s+factors(.*?)(?=item\s+2[\.\s:])",
        r"(?i)risk\s+factors(.*?)(?=unresolved\s+staff\s+comments)",
        r"(?i)risk\s+factors(.*?)(?=quantitative\s+and\s+qualitative)",
    ]
    best = ""
    for pat in patterns:
        for m in re.finditer(pat, clean, re.DOTALL):
            seg = m.group(1).strip()[:_MAX_CHARS]
            if len(seg) > len(best):
                best = seg
        if len(best) >= 2000:  # found a substantial section; no need for weaker patterns
            break

    if len(best) >= 500:
        return best

    # Fallback: return context around the first "risk factor" mention
    idx = clean.lower().find("risk factor")
    if idx != -1:
        return clean[idx : idx + _MAX_CHARS]
    return None


def _load_10k(ticker: str, conn: sqlite3.Connection) -> Optional[tuple[str, str]]:
    """
    Load the most recent 10-K from filings_cache.
    Returns (risk_section_text, artifact_id) or None.
    """
    rows = conn.execute(
        """SELECT content, accession FROM filings_cache
           WHERE ticker=? AND form_type='10-K'
           ORDER BY filing_date DESC LIMIT 2""",
        (ticker,),
    ).fetchall()

    if not rows:
        return None

    latest_content = rows[0][0] or ""
    risk_section = _extract_risk_section(latest_content)
    if not risk_section or len(risk_section) < 500:
        return None

    # Include prior year's risk section for comparison if available
    prior_risk = None
    if len(rows) > 1 and rows[1][0]:
        prior_risk = _extract_risk_section(rows[1][0] or "")

    combined = risk_section
    if prior_risk:
        combined = (
            f"CURRENT YEAR RISK FACTORS:\n{risk_section}\n\n"
            f"PRIOR YEAR RISK FACTORS (for comparison):\n{prior_risk[:30000]}"
        )

    artifact_id = rows[0][1].replace("-", "")[:16]  # accession number as artifact_id
    return combined, artifact_id


def analyze(
    ticker: str,
    client: ClaudeClient,
    tracker: CostTracker,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        result = _load_10k(ticker, conn)
        if result is None:
            logger.info(f"[{_ANALYZER}] {ticker}: no 10-K cached")
            return None

        content, artifact_id = result

        cached = cache_get(_ANALYZER, ticker, artifact_id)
        if cached:
            logger.info(f"[{_ANALYZER}] {ticker}: cache hit")
            return cached

        user_prompt = (
            f"Analyze the 10-K Risk Factors section for {ticker}. "
            f"Identify new risks (not in prior filing), material risks, "
            f"and estimate what percentage is boilerplate.\n\n"
            f"RISK FACTORS TEXT:\n{content}"
        )

        result_dict, usage = client.call_json(_SYSTEM, user_prompt)
        tracker.record(usage, client.model, label=f"{_ANALYZER}/{ticker}")

        if result_dict is None:
            logger.error(f"[{_ANALYZER}] {ticker}: JSON extraction failed")
            return None

        result_dict["ticker"] = ticker
        result_dict["analyzer"] = _ANALYZER
        cache_set(_ANALYZER, ticker, artifact_id, result_dict)
        logger.info(
            f"[{_ANALYZER}] {ticker}: severity={result_dict.get('risk_severity')} "
            f"new_risks={len(result_dict.get('new_risks', []))} "
            f"boilerplate={result_dict.get('boilerplate_percentage')}%"
        )
        return result_dict

    finally:
        if close_conn:
            conn.close()
