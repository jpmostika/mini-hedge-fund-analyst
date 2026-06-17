"""
Combined Score — blends Layer 2 quantitative composite (60%) with
Layer 3 Claude fundamental score (40%), then re-ranks within sector.

If no Claude analysis is available for a ticker, falls back to 100% quant.
No penalty for missing Claude data.
"""

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from data.db import get_connection

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_SCORING   = _CFG["scoring"]
_ANALYSIS  = _CFG["analysis"]
_QUANT_W   = _ANALYSIS["quant_weight"]   # 0.60
_CLAUDE_W  = _ANALYSIS["claude_weight"]  # 0.40
_LONG_Q    = _SCORING["long_quintile"]   # 0.80
_SHORT_Q   = _SCORING["short_quintile"]  # 0.20

# Map analyzer results to a 0-100 fundamental score
_ANALYZER_SCORE_KEYS = {
    "earnings_call":    "composite_score",       # already 1-10, scale to 0-100
    "filing_quality":   "overall_accounting_quality",  # 1-10
    "risk_factors":     None,                    # derived from risk_severity
    "insider_activity": None,                    # derived from signal_strength
}

_RISK_MAP = {"LOW": 80, "MEDIUM": 55, "HIGH": 30, "CRITICAL": 10}
_INSIDER_MAP = {
    "STRONG_BUY": 95, "BUY": 75, "NEUTRAL": 50, "SELL": 25, "STRONG_SELL": 5
}


def _analyzer_to_0_100(result: dict, analyzer: str) -> Optional[float]:
    """Convert an analyzer result dict to a 0-100 score."""
    if not result:
        return None
    if analyzer == "earnings_call":
        v = result.get("composite_score")
        return float(v) * 10 if v is not None else None
    if analyzer == "filing_quality":
        v = result.get("overall_accounting_quality")
        return float(v) * 10 if v is not None else None
    if analyzer == "risk_factors":
        sev = result.get("risk_severity", "")
        return _RISK_MAP.get(sev)
    if analyzer == "insider_activity":
        sig = result.get("signal_strength", "")
        return _INSIDER_MAP.get(sig)
    return None


def load_quant_scores(conn: sqlite3.Connection, score_date: Optional[str] = None) -> pd.DataFrame:
    """Load most recent factor_scores rows."""
    if score_date:
        df = pd.read_sql(
            "SELECT * FROM factor_scores WHERE date=?", conn, params=(score_date,)
        )
    else:
        df = pd.read_sql(
            """SELECT fs.* FROM factor_scores fs
               INNER JOIN (SELECT ticker, MAX(date) AS md FROM factor_scores GROUP BY ticker)
               lp ON fs.ticker=lp.ticker AND fs.date=lp.md""",
            conn,
        )
    return df


def load_claude_scores(
    conn: sqlite3.Connection,
    tickers: list[str],
) -> dict[str, dict[str, Optional[float]]]:
    """
    Load all cached Claude analysis results for the given tickers.
    Returns {ticker: {analyzer: 0-100 score or None}}.
    """
    if not tickers:
        return {}

    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"""SELECT analyzer, ticker, result_json
            FROM analysis_results
            WHERE ticker IN ({placeholders})
              AND expires_at > datetime('now')""",
        tickers,
    ).fetchall()

    scores: dict[str, dict] = {t: {} for t in tickers}
    for analyzer, ticker, result_json in rows:
        import json
        try:
            result = json.loads(result_json)
        except Exception:
            continue
        score = _analyzer_to_0_100(result, analyzer)
        scores[ticker][analyzer] = score

    return scores


def compute_combined(
    conn: Optional[sqlite3.Connection] = None,
    include_claude: bool = True,
) -> pd.DataFrame:
    """
    Build the combined score DataFrame for all tickers with quant scores.
    Adds columns: claude_score, combined_score, combined_signal.

    include_claude=False ignores cached Claude scores (pure-quant). Use this for
    candidate *selection* so that which tickers get analyzed depends only on the
    quant layer — otherwise Claude scores from a prior run feed back into selection
    and the analyzed set drifts every run.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        quant = load_quant_scores(conn)
        if quant.empty:
            logger.warning("No quantitative scores found — run run_scoring.py first")
            return pd.DataFrame()

        tickers = quant["ticker"].tolist()
        claude_raw = load_claude_scores(conn, tickers) if include_claude else {}

        results = []
        for _, row in quant.iterrows():
            ticker = row["ticker"]
            quant_score = row.get("composite", 50.0) or 50.0

            # Average available Claude scores
            analyzer_scores = claude_raw.get(ticker, {})
            available = [s for s in analyzer_scores.values() if s is not None]
            claude_score = float(np.mean(available)) if available else None

            # Blend
            if claude_score is not None:
                combined = _QUANT_W * quant_score + _CLAUDE_W * claude_score
                score_basis = "quant+claude"
            else:
                combined = quant_score
                score_basis = "quant_only"

            results.append({
                "ticker":        ticker,
                "sector":        row.get("sector", ""),
                "quant_score":   round(quant_score, 1),
                "claude_score":  round(claude_score, 1) if claude_score is not None else None,
                "analyzers_used": list(analyzer_scores.keys()),
                "combined_raw":  combined,
                "score_basis":   score_basis,
            })

        df = pd.DataFrame(results)

        # Re-rank combined_raw within sector → combined_score 0-100
        df["combined_score"] = (
            df.groupby("sector")["combined_raw"]
            .rank(pct=True) * 100
        ).fillna(50.0)

        # Signals based on combined_score percentile
        df["combined_signal"] = "NEUTRAL"
        df.loc[df["combined_score"] >= _LONG_Q * 100, "combined_signal"] = "LONG"
        df.loc[df["combined_score"] <= _SHORT_Q * 100, "combined_signal"] = "SHORT"
        df["score_date"] = date.today().isoformat()

        logger.info(
            f"Combined scores computed: {len(df)} tickers | "
            f"LONG={len(df[df['combined_signal']=='LONG'])} "
            f"SHORT={len(df[df['combined_signal']=='SHORT'])} "
            f"with_claude={len(df[df['claude_score'].notna()])}"
        )
        return df.sort_values("combined_score", ascending=False).reset_index(drop=True)

    finally:
        if close_conn:
            conn.close()


def get_top_candidates(
    df: pd.DataFrame, n: int = 20
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return top n LONG and top n SHORT candidates."""
    longs  = df[df["combined_signal"] == "LONG"].head(n)
    shorts = df[df["combined_signal"] == "SHORT"].tail(n).sort_values("combined_score")
    return longs, shorts


def get_top_candidates_within(
    df: pd.DataFrame, tickers: list[str], n: int = 20
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rank a fixed set of *analyzed* tickers by their blended score (combined_raw).

    Used for the final ranking after Layer 3. Unlike get_top_candidates, this does
    NOT compare against the un-analyzed universe — only the candidates that received
    a Claude score are comparable on a quant+claude basis, so we rank within that
    pool. Highest blended score = strongest long, lowest = strongest short.
    """
    sub = df[df["ticker"].isin(tickers)].copy().sort_values("combined_raw", ascending=False)
    longs  = sub.head(n)
    shorts = sub.tail(n).sort_values("combined_raw")
    return longs, shorts
