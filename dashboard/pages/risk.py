"""Page III — RISK: Circuit breakers, factor risk model, stress tests, correlation."""

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import numpy as np
from pathlib import Path
import yaml, json

_CFG = yaml.safe_load((Path(__file__).parent.parent.parent / "config.yaml").read_text())
C    = _CFG["dashboard"]["colors"]
NAV  = _CFG["portfolio"]["nav"]


def render(conn, system_state: dict, state_json: str):
    risk   = system_state.get("risk", {})
    fm     = risk.get("factor_model", {}) or {}

    # ── Circuit breaker bars ─────────────────────────────────────────── #
    st.markdown("### Circuit Breakers")
    try:
        from risk.circuit_breakers import _compute_daily_pnl, _compute_weekly_pnl, _compute_drawdown
        daily_pct   = _compute_daily_pnl(conn)   / NAV
        weekly_pct  = _compute_weekly_pnl(conn)  / NAV
        drawdown    = abs(_compute_drawdown(conn))
    except Exception:
        daily_pct, weekly_pct, drawdown = 0.0, 0.0, 0.0

    cb_col1, cb_col2, cb_col3 = st.columns(3)
    _gauge(cb_col1, "Daily Loss",   abs(daily_pct)*100,  2.5,  "% NAV", C["short"])
    _gauge(cb_col2, "Weekly Loss",  abs(weekly_pct)*100, 4.0,  "% NAV", C["short"])
    _gauge(cb_col3, "Drawdown",     drawdown*100,         8.0,  "% NAV", C["short"])

    triggered = risk.get("circuit_breakers", [])
    if triggered:
        for cb in triggered:
            st.error(f"🚨 CIRCUIT BREAKER: {cb.get('breaker')} → {cb.get('action')} | {cb.get('detail')}")
    if risk.get("halt_active"):
        st.error("🔴 **HALT LOCK ACTIVE — ALL TRADING SUSPENDED**. Run: `python run_risk_check.py --clear-halt`")

    # ── Tail risk KPIs ───────────────────────────────────────────────── #
    st.markdown("### Tail Risk")
    tr_col1, tr_col2, tr_col3, tr_col4 = st.columns(4)
    vix = risk.get("vix")
    credit_z = risk.get("credit_z") if "credit_z" in risk else None
    tail_action = risk.get("tail_action", "NONE")
    tr_col1.metric("VIX", f"{vix:.1f}" if vix else "N/A",
                   delta="ELEVATED" if vix and vix >= 25 else "Normal")
    tr_col2.metric("Credit Spread Z", f"{credit_z:.2f}" if credit_z else "N/A")
    tr_col3.metric("Tail Action", tail_action or "NONE")
    tr_col4.metric("Portfolio Vol", f"{fm.get('portfolio_vol_pct','?')}% ann.")

    # ── Risk decomposition donut ─────────────────────────────────────── #
    col_donut, col_mctr = st.columns([1, 2])

    with col_donut:
        fv = fm.get("portfolio_factor_var", 0) or 0
        sv = fm.get("portfolio_specific_var", 0) or 0
        if fv + sv > 0:
            fig = go.Figure(go.Pie(
                labels=["Factor Risk", "Idiosyncratic"],
                values=[fv, sv],
                hole=0.65,
                marker_colors=[C["accent"], C["muted"]],
                textinfo="label+percent",
                textfont_color=C["text"],
            ))
            fig.update_layout(
                paper_bgcolor=C["bg"], font_color=C["text"],
                showlegend=False, height=220,
                margin=dict(l=0, r=0, t=20, b=0),
                annotations=[dict(
                    text=f"{fm.get('portfolio_vol_pct','?')}%",
                    x=0.5, y=0.5, showarrow=False,
                    font=dict(size=20, color=C["text"]),
                )],
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Run risk model for decomposition")

    with col_mctr:
        st.markdown("**MCTR — Top Risk Contributors**")
        mctr_flags = fm.get("mctr_flags", [])
        top_mctr   = fm.get("top_mctr", [])
        if top_mctr:
            mctr_df = pd.DataFrame(top_mctr)
            mctr_df["flag"] = mctr_df["ticker"].isin(
                [f["ticker"] for f in mctr_flags]
            ).map({True: "⚠️ >1.5x", False: "OK"})
            st.dataframe(
                mctr_df.rename(columns={"ticker":"Ticker","mctr_pct":"MCTR%","flag":"Flag"}),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("Run run_risk_check.py to compute MCTR")

    # ── Factor exposure bars ─────────────────────────────────────────── #
    st.markdown("### Factor Exposures (Long vs Short)")
    fa = system_state.get("factor_averages", {})
    if fa:
        factors   = list(fa.get("longs", {}).keys())
        long_vals = [fa["longs"].get(f, 50) for f in factors]
        sht_vals  = [fa["shorts"].get(f, 50) for f in factors]

        fig = go.Figure()
        fig.add_bar(name="Longs",  x=factors, y=long_vals,  marker_color=C["long"])
        fig.add_bar(name="Shorts", x=factors, y=sht_vals,   marker_color=C["short"])
        fig.update_layout(
            paper_bgcolor=C["bg"], plot_bgcolor=C["bg"],
            font_color=C["text"], barmode="group",
            height=220, margin=dict(l=20, r=20, t=10, b=40),
            legend=dict(orientation="h", y=1.1),
            yaxis=dict(range=[0,100]),
            shapes=[dict(type="line", y0=50, y1=50, x0=-0.5, x1=len(factors)-0.5,
                         line=dict(color=C["muted"], dash="dash", width=1))],
        )
        st.plotly_chart(fig, use_container_width=True)

    # ── Stress test table ────────────────────────────────────────────── #
    st.markdown("### Stress Tests")
    stress = system_state.get("risk", {}).get("stress_worst")
    try:
        risk_file = Path(__file__).parent.parent.parent / _CFG["risk"]["risk_state_file"]
        if risk_file.exists():
            rs = json.loads(risk_file.read_text())
            stress_list = rs.get("stress_tests", [])
            if stress_list:
                sdf = pd.DataFrame(stress_list)[
                    ["scenario","total_pct","long_pct","short_pct","total_pnl"]
                ].rename(columns={
                    "scenario": "Scenario",
                    "total_pct": "Total %",
                    "long_pct":  "Long %",
                    "short_pct": "Short %",
                    "total_pnl": "Total P&L ($)",
                })
                st.dataframe(
                    sdf.style.applymap(
                        lambda v: f"color:{C['short']}" if isinstance(v, (int,float)) and v < 0
                                  else f"color:{C['long']}"  if isinstance(v, (int,float)) and v > 0
                                  else "",
                        subset=["Total %","Long %","Short %"]
                    ),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.info("Run: python run_risk_check.py --stress")
    except Exception:
        st.info("Stress test results not available")

    # ── Correlation heatmap ──────────────────────────────────────────── #
    st.markdown("### Correlation — Within-Book")
    corr = system_state.get("risk", {}).get("correlation", {})
    col_cl, col_cs = st.columns(2)
    col_cl.metric("Long Book Avg Corr",  f"{corr.get('long_avg_corr','—')}")
    col_cl.metric("Long Book ENB",       f"{corr.get('long_enb','—')}")
    col_cs.metric("Short Book Avg Corr", f"{corr.get('short_avg_corr','—')}")


def _gauge(col, label: str, value: float, threshold: float, unit: str, color: str):
    pct = min(value / threshold, 1.0)
    bar_color = color if pct > 0.75 else (C["accent"] if pct > 0.5 else C["long"])
    col.markdown(
        f'<div style="background:linear-gradient(135deg,{C["card_from"]},{C["card_to"]});'
        f'border:1px solid {C["border"]};border-radius:10px;padding:14px;">'
        f'<div style="font-size:11px;color:{C["muted"]};text-transform:uppercase;'
        f'letter-spacing:0.08em;">{label}</div>'
        f'<div style="font-size:24px;font-weight:700;color:{bar_color};'
        f'font-family:JetBrains Mono,monospace;">{value:.2f}{unit}</div>'
        f'<div style="background:{C["border"]};border-radius:4px;height:4px;margin-top:8px;">'
        f'<div style="background:{bar_color};width:{pct*100:.0f}%;height:4px;'
        f'border-radius:4px;"></div></div>'
        f'<div style="font-size:10px;color:{C["muted"]};margin-top:4px;">Limit: {threshold}{unit}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
