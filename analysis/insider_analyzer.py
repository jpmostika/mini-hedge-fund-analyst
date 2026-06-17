"""
Insider Activity Analyzer.
Interprets Form 4 open-market transactions over the last 90 days.
Distinguishes routine selling (diversification, options, RSU vesting) from
meaningful buying (CEO/CFO paying market price with their own money).
Returns None if no insider data is available.
"""

import hashlib
import logging
import sqlite3
from datetime import date, timedelta
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

_ANALYZER   = "insider_activity"
_LOOKBACK   = 90

_SYSTEM = """You are a specialist in SEC Form 4 insider activity analysis at a long/short equity fund. You interpret insider transaction patterns to assess management conviction and corporate health.

YOUR FRAMEWORK:

SIGNAL HIERARCHY (most to least meaningful):
1. CEO/CFO open-market PURCHASE at current market price → STRONG BUY signal (they know the most and are using their own money)
2. Multiple insiders buying in the same 30-day window (cluster buying) → BUY signal
3. Director/VP open-market purchase → moderate BUY signal
4. No insider purchases despite recent stock decline → mildly BEARISH (lack of conviction)
5. Routine RSU vesting sales (code A/M/F grants, then code S sales) → NEUTRAL (tax-motivated, no signal)
6. Large discretionary CEO/CFO sales NOT part of 10b5-1 plan → BEARISH signal
7. Multiple insiders selling simultaneously without apparent compensation event → STRONG SELL signal

TRANSACTION CODES:
- P = Open-market purchase (HIGHEST conviction signal)
- S = Sale (context matters: routine diversification vs alarmed exit)
- A = Grant/award (IGNORE — compensation, not signal)
- M = Exercise of derivative (IGNORE — mechanistic)
- F = Tax withholding (IGNORE — mechanistic)

CONTEXT TO CONSIDER:
- Is the price purchased near 52-week highs or lows?
- Are purchases increasing or decreasing over time?
- Is the dollar amount material relative to the insider's known compensation?
- Is cluster buying happening before or after a significant event?

Output ONLY valid JSON:
{
  "signal_strength": "<STRONG_BUY|BUY|NEUTRAL|SELL|STRONG_SELL>",
  "confidence": "<HIGH|MEDIUM|LOW>",
  "net_dollar_flow_usd": <float, net purchases minus sales in USD, positive = net buying>,
  "key_transactions": [
    {"insider": "<name>", "title": "<title>", "action": "<bought/sold>", "shares": <int>, "price": <float>, "value_usd": <float>, "date": "<YYYY-MM-DD>", "significance": "<HIGH|MEDIUM|LOW>"}
  ],
  "cluster_buying_detected": <true|false>,
  "ceo_cfo_activity": "<bought|sold|none>",
  "reasoning": "<3-4 sentences explaining the interpretation and why this is the signal>",
  "one_line_summary": "<single sentence: signal direction, key actors, and confidence level>"
}"""


def _load_insider_data(ticker: str, conn: sqlite3.Connection) -> Optional[pd.DataFrame]:
    cutoff = (date.today() - timedelta(days=_LOOKBACK)).isoformat()
    # Codes P (open-market purchase) and S (open-market sale) are the open-market
    # transactions worth interpreting; A/M/F/G/etc. are compensation/mechanical.
    # (Do NOT additionally require is_open_market=1 — that flag is set for P only,
    # which would silently drop every open-market sale.)
    df = pd.read_sql(
        """SELECT insider_name, insider_title, transaction_code, shares, price, date,
                  is_open_market, is_ceo_cfo
           FROM insider_transactions
           WHERE ticker=? AND date >= ? AND transaction_code IN ('P','S')
           ORDER BY date DESC""",
        conn,
        params=(ticker, cutoff),
    )
    return df if not df.empty else None


def _format_transactions(df: pd.DataFrame) -> str:
    lines = ["Date       | Insider              | Title                | Code | Shares    | Price   | $ Value    | CEO/CFO"]
    lines.append("-" * 110)
    for _, row in df.iterrows():
        shares = int(row["shares"] or 0)
        price  = row["price"] or 0.0
        val    = shares * price
        ceo    = "YES" if row["is_ceo_cfo"] else "no"
        lines.append(
            f"{row['date']} | {str(row['insider_name'])[:20]:20s} | "
            f"{str(row['insider_title'])[:20]:20s} | {row['transaction_code']:4s} | "
            f"{shares:9,d} | ${price:6.2f} | ${val:10,.0f} | {ceo}"
        )
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
        df = _load_insider_data(ticker, conn)
        if df is None:
            logger.info(f"[{_ANALYZER}] {ticker}: no insider data (last {_LOOKBACK} days)")
            return None

        # artifact_id = hash of the transaction dates and amounts
        artifact_id = hashlib.sha256(
            df[["date", "shares", "transaction_code"]].to_json().encode()
        ).hexdigest()[:16]

        cached = cache_get(_ANALYZER, ticker, artifact_id)
        if cached:
            logger.info(f"[{_ANALYZER}] {ticker}: cache hit")
            return cached

        table = _format_transactions(df)
        user_prompt = (
            f"Analyze the insider trading activity for {ticker} over the last {_LOOKBACK} days.\n"
            f"Distinguish routine selling from meaningful signals. Focus on open-market "
            f"purchases and discretionary sales.\n\n"
            f"INSIDER TRANSACTIONS ({len(df)} transactions):\n{table}"
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
            f"[{_ANALYZER}] {ticker}: signal={result_dict.get('signal_strength')} "
            f"confidence={result_dict.get('confidence')} "
            f"net_flow=${result_dict.get('net_dollar_flow_usd', 0):,.0f}"
        )
        return result_dict

    finally:
        if close_conn:
            conn.close()
