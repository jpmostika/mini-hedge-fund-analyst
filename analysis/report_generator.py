"""
Report Generator — writes one Markdown file per LONG/SHORT candidate.
Saved to output/reports_{timestamp}/{TICKER}.md
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

from data.db import get_connection

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_REPORTS_DIR = Path(__file__).parent.parent / _CFG["analysis"]["reports_dir"]


def _load_analysis(ticker: str, conn: sqlite3.Connection) -> dict[str, dict]:
    """Load all fresh cached analysis results for a ticker."""
    rows = conn.execute(
        """SELECT analyzer, result_json FROM analysis_results
           WHERE ticker=? AND expires_at > datetime('now')""",
        (ticker,),
    ).fetchall()
    return {row[0]: json.loads(row[1]) for row in rows}


def _load_factor_scores(ticker: str, conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute(
        """SELECT * FROM factor_scores WHERE ticker=?
           ORDER BY date DESC LIMIT 1""",
        (ticker,),
    ).fetchone()
    if row:
        cols = [d[0] for d in conn.execute("SELECT * FROM factor_scores LIMIT 0").description]
        return dict(zip(cols, row))
    return None


def _load_upcoming_earnings(ticker: str, conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        """SELECT earnings_date, eps_estimate FROM earnings_calendar
           WHERE ticker=? AND earnings_date >= date('now')
           ORDER BY earnings_date LIMIT 1""",
        (ticker,),
    ).fetchone()
    if row:
        est = f" (EPS est: ${row[1]:.2f})" if row[1] else ""
        return f"{row[0]}{est}"
    return None


def _score_bar(score: Optional[float], width: int = 20) -> str:
    if score is None:
        return "N/A"
    filled = int(round(score / 100 * width))
    return f"[{'#' * filled}{'.' * (width - filled)}] {score:.0f}/100"


def _render_report(
    ticker: str,
    signal: str,
    combined_score: float,
    quant_score: float,
    claude_score: Optional[float],
    sector: str,
    analyses: dict[str, dict],
    factor_scores: Optional[dict],
    upcoming_earnings: Optional[str],
    sector_analysis: Optional[dict],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    signal_emoji = {"LONG": "LONG", "SHORT": "SHORT", "NEUTRAL": "NEUTRAL"}.get(signal, signal)

    lines = [
        f"# {ticker} — {signal_emoji}",
        f"**Sector:** {sector}  |  **Generated:** {now}",
        "",
        "---",
        "",
        "## Score Summary",
        "",
        f"| Metric | Score |",
        f"|--------|-------|",
        f"| **Combined Score** | {_score_bar(combined_score)} |",
        f"| Quant Composite (Layer 2) | {_score_bar(quant_score)} |",
        f"| Claude Fundamental (Layer 3) | {_score_bar(claude_score)} |",
        "",
    ]

    # Factor scores breakdown
    if factor_scores:
        lines += [
            "### Quantitative Factor Scores",
            "",
            "| Factor | Score |",
            "|--------|-------|",
        ]
        for f in ["momentum", "value", "quality", "growth", "revisions", "short_interest", "insider", "institutional"]:
            v = factor_scores.get(f)
            if v is not None:
                lines.append(f"| {f.replace('_', ' ').title()} | {v:.1f} |")
        lines.append("")

    # Upcoming earnings
    if upcoming_earnings:
        lines += [
            "## Upcoming Catalyst",
            "",
            f"**Earnings Date:** {upcoming_earnings}",
            "",
        ]

    # Earnings call analysis
    ec = analyses.get("earnings_call")
    if ec:
        lines += [
            "## Earnings Call Analysis",
            "",
            f"**Overall:** {ec.get('one_line_summary', 'N/A')}",
            "",
            "| Dimension | Score | Reasoning |",
            "|-----------|-------|-----------|",
        ]
        for dim in ["management_confidence", "revenue_guidance", "margin_trajectory",
                    "competitive_position", "risk_factors", "capital_allocation"]:
            d = ec.get(dim, {})
            score = d.get("score", "N/A")
            reason = d.get("reasoning", "").replace("|", "/").replace("\n", " ")[:120]
            lines.append(f"| {dim.replace('_', ' ').title()} | {score}/10 | {reason} |")
        lines += [
            "",
            f"**Bull Case:** {ec.get('bull_case', 'N/A')}",
            "",
            f"**Bear Case:** {ec.get('bear_case', 'N/A')}",
            "",
        ]
        key_quotes = ec.get("key_quotes", [])
        if key_quotes:
            lines += ["**Key Quotes:**", ""]
            for q in key_quotes[:3]:
                lines.append(f"> {q}")
            lines.append("")

    # Filing / accounting quality
    fq = analyses.get("filing_quality")
    if fq:
        risk_level = fq.get("risk_level", "N/A")
        lines += [
            "## Accounting Quality",
            "",
            f"**Overall:** {fq.get('one_line_summary', 'N/A')}",
            f"**Risk Level:** {risk_level}  |  "
            f"**Earnings Quality:** {fq.get('earnings_quality_score', 'N/A')}/10  |  "
            f"**Balance Sheet:** {fq.get('balance_sheet_score', 'N/A')}/10",
            "",
        ]
        green = fq.get("green_flags", [])
        red   = fq.get("red_flags", [])
        if green:
            lines.append("**Green Flags:**")
            for g in green:
                lines.append(f"- {g}")
            lines.append("")
        if red:
            lines.append("**Red Flags:**")
            for r in red:
                lines.append(f"- {r}")
            lines.append("")

    # Risk factors
    rf = analyses.get("risk_factors")
    if rf:
        lines += [
            "## Risk Factors (10-K)",
            "",
            f"**Overall:** {rf.get('one_line_summary', 'N/A')}",
            f"**Severity:** {rf.get('risk_severity', 'N/A')}  |  "
            f"**Boilerplate:** {rf.get('boilerplate_percentage', 'N/A')}%",
            "",
        ]
        new_risks = rf.get("new_risks", [])
        if new_risks:
            lines.append("**New Risks (vs prior filing):**")
            for nr in new_risks[:3]:
                sev = nr.get("severity", "")
                lines.append(f"- [{sev}] {nr.get('risk', '')}")
            lines.append("")
        material = rf.get("material_risks", [])
        if material:
            lines.append("**Material Risks:**")
            for mr in material[:3]:
                lines.append(f"- [{mr.get('severity', '')}] {mr.get('risk', '')}")
            lines.append("")

    # Insider activity
    ia = analyses.get("insider_activity")
    if ia:
        lines += [
            "## Insider Activity (Last 90 Days)",
            "",
            f"**Signal:** {ia.get('signal_strength', 'N/A')}  |  "
            f"**Confidence:** {ia.get('confidence', 'N/A')}  |  "
            f"**Net Flow:** ${ia.get('net_dollar_flow_usd', 0):,.0f}",
            "",
            f"**Interpretation:** {ia.get('reasoning', 'N/A')}",
            "",
        ]
        key_txns = ia.get("key_transactions", [])
        if key_txns:
            lines += [
                "| Date | Insider | Action | Value |",
                "|------|---------|--------|-------|",
            ]
            for txn in key_txns[:4]:
                lines.append(
                    f"| {txn.get('date', '')} | {txn.get('insider', '')} | "
                    f"{txn.get('action', '')} | ${txn.get('value_usd', 0):,.0f} |"
                )
            lines.append("")

    # Sector context
    if sector_analysis:
        tl = sector_analysis.get("top_long_idea", {})
        ts = sector_analysis.get("top_short_idea", {})
        if tl.get("ticker") == ticker or ts.get("ticker") == ticker:
            lines += ["## Sector Context", ""]
            if tl.get("ticker") == ticker:
                lines.append(f"**Top Long in Sector:** {tl.get('thesis', '')}")
                cats = tl.get("key_catalysts", [])
                if cats:
                    lines.append(f"**Catalysts:** {', '.join(cats)}")
                lines.append("")
            if ts.get("ticker") == ticker:
                lines.append(f"**Top Short in Sector:** {ts.get('thesis', '')}")
                trigs = ts.get("key_triggers", [])
                if trigs:
                    lines.append(f"**Triggers:** {', '.join(trigs)}")
                lines.append("")
        lines.append(f"**Sector Outlook ({sector}):** {sector_analysis.get('sector_outlook', 'N/A')}")
        lines.append("")

    lines += [
        "---",
        f"*Generated by Meridian Capital Partners — Automated Research System*",
        f"*Layer 1 (Data) + Layer 2 (Scoring) + Layer 3 (Claude AI Analysis)*",
    ]

    return "\n".join(lines)


def generate_reports(
    combined_df: pd.DataFrame,
    sector_analyses: Optional[dict[str, dict]] = None,
    conn: Optional[sqlite3.Connection] = None,
    n_longs: int = 20,
    n_shorts: int = 20,
    candidates: Optional[pd.DataFrame] = None,
) -> list[Path]:
    """
    Generate one Markdown report per candidate.

    If `candidates` is provided (the analyzed pool), report on exactly those rows —
    this avoids the trap of re-selecting LONG/SHORT from the full universe after the
    Claude recompute, which surfaces un-analyzed names with empty reports. Otherwise
    fall back to signal-based selection over combined_df.
    Returns list of written file paths.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = _REPORTS_DIR / f"reports_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    sector_analyses = sector_analyses or {}

    # Select candidates
    if candidates is None:
        longs  = combined_df[combined_df["combined_signal"] == "LONG"].head(n_longs)
        shorts = combined_df[combined_df["combined_signal"] == "SHORT"].tail(n_shorts)
        candidates = pd.concat([longs, shorts]).drop_duplicates("ticker")

    written: list[Path] = []
    try:
        for _, row in candidates.iterrows():
            ticker = row["ticker"]
            try:
                analyses      = _load_analysis(ticker, conn)
                factor_scores = _load_factor_scores(ticker, conn)
                upcoming_earn = _load_upcoming_earnings(ticker, conn)
                sector        = row.get("sector", "")
                sector_anal   = sector_analyses.get(sector)

                md = _render_report(
                    ticker          = ticker,
                    signal          = row["combined_signal"],
                    combined_score  = row["combined_score"],
                    quant_score     = row["quant_score"],
                    claude_score    = row.get("claude_score"),
                    sector          = sector,
                    analyses        = analyses,
                    factor_scores   = factor_scores,
                    upcoming_earnings = upcoming_earn,
                    sector_analysis = sector_anal,
                )

                path = out_dir / f"{ticker}.md"
                path.write_text(md, encoding="utf-8")
                written.append(path)
                logger.info(f"Report written: {path}")

            except Exception as e:
                logger.error(f"Report generation failed for {ticker}: {e}", exc_info=True)

    finally:
        if close_conn:
            conn.close()

    logger.info(f"Generated {len(written)} reports in {out_dir}")
    return written
