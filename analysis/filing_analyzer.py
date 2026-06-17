"""
Filing / Fundamentals Analyzer — forensic accounting quality review.
Ingests 8 quarters of stored ratio data and flags earnings/balance sheet quality issues.
"""

import hashlib
import json
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

_ANALYZER = "filing_quality"

_SYSTEM = """You are a forensic accounting analyst at a long/short equity fund. You specialize in detecting earnings quality issues, balance sheet manipulation, and accrual accounting red flags that lead short-sellers to target companies.

Your job is to analyze 8 quarters of fundamental ratio data and provide a rigorous forensic assessment. You must be decisive — vague or balanced assessments have no alpha.

ANALYTICAL FRAMEWORK:

EARNINGS QUALITY (most important signal):
- CFO/NI ratio: >1.0 = high quality (cash > reported earnings), <0.7 = red flag (earnings not converting to cash)
- Accruals ratio = (NI - CFO) / Total Assets: >5% is a warning, >10% is a red flag (Sloan accruals anomaly)
- Rising AR/Revenue: suggests channel stuffing or aggressive revenue recognition
- Net margin vs operating cash flow divergence: a widening gap is a classic manipulation flag

BALANCE SHEET HEALTH:
- Debt/equity trends: rising leverage at peak cycle = elevated risk
- Working capital trajectory: declining WC with growing revenue = efficiency OR distress
- Retained earnings vs buybacks/dividends: are they distributing more than they earn?
- Current ratio trends: sustained decline below 1.0 is a distress signal

GROWTH QUALITY:
- Revenue growth vs accounts receivable growth: AR growing faster = revenue pull-forward
- Revenue acceleration vs deceleration trends
- Gross margin trajectory: widening = pricing power, narrowing = cost pressure or competition
- R&D intensity relative to sector: very low may indicate underinvestment

DISTRESS SIGNALS:
- Altman Z-Score: >2.99 safe, 1.81-2.99 grey zone, <1.81 distress
- Piotroski F-Score: ≥7 strong, ≤3 weak

Output ONLY valid JSON:
{
  "earnings_quality_score": <int 1-10, 10=pristine>,
  "balance_sheet_score": <int 1-10, 10=fortress>,
  "overall_accounting_quality": <int 1-10>,
  "green_flags": ["<flag 1>", "<flag 2>"],
  "red_flags": ["<flag 1>", "<flag 2>"],
  "risk_level": "<LOW|MEDIUM|HIGH|CRITICAL>",
  "key_concerns": "<2-3 sentences on top risks>",
  "key_strengths": "<2-3 sentences on top positives>",
  "altman_z_interpretation": "<safe|grey_zone|distress|insufficient_data>",
  "piotroski_interpretation": "<strong|average|weak|insufficient_data>",
  "one_line_summary": "<single sentence verdict on accounting quality and investment implications>"
}"""


def _load_fundamentals(ticker: str, conn: sqlite3.Connection) -> Optional[pd.DataFrame]:
    """Load last 8 quarterly fundamental rows for the ticker."""
    df = pd.read_sql(
        """SELECT * FROM fundamentals
           WHERE ticker=? AND period_type='quarterly'
           ORDER BY period_end DESC
           LIMIT 8""",
        conn,
        params=(ticker,),
    )
    return df if not df.empty else None


def _format_fundamentals(df: pd.DataFrame) -> str:
    """Format fundamentals DataFrame as a readable table for the prompt."""
    cols = [
        "period_end", "roe", "roa", "gross_margin", "operating_margin", "net_margin",
        "revenue_growth_yoy", "revenue_growth_qoq", "earnings_growth_yoy",
        "debt_to_equity", "fcf_yield", "current_ratio",
        "ar_to_revenue", "cfo_to_ni", "accruals_ratio",
        "ebit", "total_liabilities", "working_capital",
        "shares_outstanding", "asset_turnover",
    ]
    available = [c for c in cols if c in df.columns]
    sub = df[available].copy()

    lines = ["Quarter | " + " | ".join(available[1:])]
    lines.append("-" * 120)
    for _, row in sub.iterrows():
        vals = []
        for c in available[1:]:
            v = row[c]
            if v is None or (isinstance(v, float) and v != v):
                vals.append("N/A")
            elif isinstance(v, float):
                vals.append(f"{v:.3f}")
            else:
                vals.append(str(v))
        lines.append(f"{row['period_end']} | " + " | ".join(vals))

    return "\n".join(lines)


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
        df = _load_fundamentals(ticker, conn)
        if df is None or len(df) < 2:
            logger.info(f"[{_ANALYZER}] {ticker}: insufficient fundamental data")
            return None

        # Build deterministic artifact_id from the data
        artifact_id = hashlib.sha256(
            df[["period_end"]].to_json().encode()
        ).hexdigest()[:16]

        cached = cache_get(_ANALYZER, ticker, artifact_id)
        if cached:
            logger.info(f"[{_ANALYZER}] {ticker}: cache hit")
            return cached

        table = _format_fundamentals(df)
        user_prompt = (
            f"Perform a forensic accounting analysis for {ticker} using the following "
            f"8-quarter fundamental ratio data. Identify earnings quality issues, "
            f"balance sheet risks, and any accounting red flags.\n\n"
            f"FUNDAMENTAL DATA ({len(df)} quarters):\n{table}"
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
            f"[{_ANALYZER}] {ticker}: EQ={result_dict.get('earnings_quality_score')} "
            f"BS={result_dict.get('balance_sheet_score')} "
            f"risk={result_dict.get('risk_level')}"
        )
        return result_dict

    finally:
        if close_conn:
            conn.close()
