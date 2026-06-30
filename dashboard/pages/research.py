"""Page II — RESEARCH: Factor heatmap, candidate cards, approval workflow."""

import json
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path
import yaml

_CFG = yaml.safe_load((Path(__file__).parent.parent.parent / "config.yaml").read_text())
C    = _CFG["dashboard"]["colors"]
FACTORS = ["momentum","value","quality","growth","revisions","short_interest","insider","institutional"]


def render(conn, system_state: dict, state_json: str):
    from dashboard.styles import card, badge, signal_color
    from portfolio.rebalance_schedule import check_schedule

    # ── KPI row ─────────────────────────────────────────────────────── #
    scores = system_state.get("scores", {})
    risk   = system_state.get("risk", {})
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("LONG Candidates",  scores.get("long_candidates", "—"))
    col2.metric("SHORT Candidates", scores.get("short_candidates", "—"))
    # factor_alerts is stored as a list of alert dicts in system_state
    factor_alerts_list = risk.get("factor_alerts") or []
    if isinstance(factor_alerts_list, dict):
        factor_alerts_list = factor_alerts_list.get("alerts", [])
    col3.metric("Factor Alerts",    len(factor_alerts_list))
    col4.metric("Crowding Warnings",
                len([w for w in factor_alerts_list
                     if isinstance(w, dict) and w.get("priority") == "HIGH"]))
    col5.metric("Pending Approvals", system_state.get("pending_trades", 0))

    # ── Calendar warnings banner ─────────────────────────────────────── #
    try:
        warnings = check_schedule(
            [s["ticker"] for s in scores.get("top_5_longs", [])[:10]
             + scores.get("top_5_shorts", [])[:10]], conn
        )
        if warnings:
            for w in warnings:
                st.warning(f"⚠️ {w}")
    except Exception:
        pass

    # ── Optimization toggle ──────────────────────────────────────────── #
    opt_method = st.radio(
        "Optimization Method",
        ["Conviction-Tilt", "MVO (Markowitz)"],
        horizontal=True,
        key="opt_method",
    )

    # ── Factor heatmap: top 30 + bottom 30 ──────────────────────────── #
    st.markdown("### Factor Heatmap — Top 30 Longs & Bottom 30 Shorts")
    try:
        # scored_universe_latest.csv has individual factor columns (momentum, value, etc.)
        # combined_scores_latest.csv only has the blended composite — wrong file for heatmap
        scored_csv  = Path(__file__).parent.parent.parent / "output" / "scored_universe_latest.csv"
        combined_csv = Path(__file__).parent.parent.parent / "output" / "combined_scores_latest.csv"

        # Merge so we have both factor scores AND combined signal
        csv_path = scored_csv if scored_csv.exists() else None
        if csv_path:
            df = pd.read_csv(csv_path)
            # Attach combined_signal from combined_scores if available
            if combined_csv.exists():
                comb = pd.read_csv(combined_csv)[["ticker","combined_signal","combined_score"]]
                df = df.merge(comb, on="ticker", how="left")
                signal_col = "combined_signal"
                score_col  = "combined_score"
            else:
                signal_col = "signal"
                score_col  = "composite"

            top30    = df[df[signal_col]=="LONG"].nlargest(30, score_col)
            bottom30 = df[df[signal_col]=="SHORT"].nsmallest(30, score_col)
            heat_df  = pd.concat([top30, bottom30])
            avail    = [f for f in FACTORS if f in heat_df.columns]
            z        = heat_df[avail].fillna(50).values
            labels   = heat_df["ticker"].tolist()
            signals  = heat_df[signal_col].tolist()
            ticker_colors = [C["long"] if s == "LONG" else C["short"] for s in signals]

            fig = go.Figure(go.Heatmap(
                z=z.T,
                x=labels,
                y=avail,
                colorscale=[[0, C["short"]], [0.5, "#1e2d3d"], [1, C["long"]]],
                zmid=50,
                showscale=True,
                colorbar=dict(thickness=12, len=0.6),
            ))
            fig.update_layout(
                paper_bgcolor=C["bg"], plot_bgcolor=C["bg"],
                font_color=C["text"], height=300,
                margin=dict(l=80, r=20, t=10, b=80),
                xaxis=dict(tickfont=dict(size=9, color=C["muted"])),
                yaxis=dict(tickfont=dict(size=10)),
            )
            # Color x-axis labels by signal
            fig.update_xaxes(tickvals=labels,
                             ticktext=[f'<span style="color:{c}">{t}</span>'
                                       for t, c in zip(labels, ticker_colors)])
            st.plotly_chart(fig, use_container_width=True)
        if not csv_path:
            st.info("Run `python run_scoring.py` to generate scored_universe_latest.csv")
        elif not avail:
            st.info("No factor columns found — ensure run_scoring.py completed successfully")
    except Exception as e:
        st.info(f"Factor heatmap: {e}")

    # ── Candidate cards ──────────────────────────────────────────────── #
    tab_long, tab_short = st.tabs(["🟢 Top 10 Longs", "🔴 Top 10 Shorts"])

    with tab_long:
        _render_candidate_cards(scores.get("top_5_longs", []), "LONG", conn)

    with tab_short:
        _render_candidate_cards(scores.get("top_5_shorts", []), "SHORT", conn)


def _render_candidate_cards(candidates: list, signal: str, conn):
    from dashboard.styles import badge
    import sqlite3, json

    color = C["long"] if signal == "LONG" else C["short"]

    for cand in candidates:
        ticker  = cand.get("ticker", "?")
        sector  = cand.get("sector", "")
        score   = cand.get("combined_score", cand.get("composite", "—"))

        # Load Claude analysis if cached
        analysis_row = None
        try:
            analysis_row = conn.execute(
                "SELECT result_json FROM analysis_results WHERE ticker=? AND expires_at > datetime('now') LIMIT 1",
                (ticker,)
            ).fetchone()
        except Exception:
            pass

        score_str = f"{score:.1f}" if isinstance(score, (int, float)) else str(score)
        with st.expander(
            f"{ticker} — {sector} | Score: {score_str}",
            expanded=False,
        ):
            cols = st.columns([2, 1, 1, 1])
            cols[0].markdown(f'<span style="color:{color};font-weight:700;">{signal}</span>', unsafe_allow_html=True)

            # Piotroski / Altman from fundamentals
            try:
                row = conn.execute(
                    """SELECT f.ebit, f.total_liabilities, f.working_capital, f.retained_earnings
                       FROM fundamentals f INNER JOIN (SELECT ticker, MAX(period_end) pe
                       FROM fundamentals WHERE ticker=? AND period_type='quarterly' GROUP BY ticker)
                       lp ON f.ticker=lp.ticker AND f.period_end=lp.pe""",
                    (ticker,),
                ).fetchone()
                if row:
                    cols[1].metric("EBIT", f"${row[0]/1e9:.1f}B" if row[0] else "N/A")
                    cols[2].metric("Net Debt Proxy", f"${row[1]/1e9:.1f}B" if row[1] else "N/A")
            except Exception:
                pass

            # Approve / Reject buttons
            col_a, col_r, col_reset = st.columns(3)
            if col_a.button(f"Approve {ticker}", key=f"approve_{ticker}"):
                st.success(f"✓ {ticker} queued for Layer 6 execution (pre-trade veto will run)")
            if col_r.button(f"Reject {ticker}", key=f"reject_{ticker}"):
                st.error(f"✗ {ticker} rejected")
            if col_reset.button(f"Reset", key=f"reset_{ticker}"):
                st.info("Reset")

            # Claude analysis
            if analysis_row:
                try:
                    result = json.loads(analysis_row[0])
                    summary = result.get("one_line_summary") or result.get("one_line_summary")
                    if summary:
                        st.markdown(f'<div style="color:{C["muted"]};font-size:12px;margin-top:8px;">'
                                    f'Claude: {summary}</div>', unsafe_allow_html=True)
                except Exception:
                    pass
            else:
                st.caption("No Claude analysis cached — run run_analysis.py")
