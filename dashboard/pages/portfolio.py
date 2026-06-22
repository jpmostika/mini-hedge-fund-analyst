"""Page I — PORTFOLIO (Cover): JARVIS hero, metrics strip, chat."""

import streamlit as st
import pandas as pd
from pathlib import Path
import yaml, json

_CFG  = yaml.safe_load((Path(__file__).parent.parent.parent / "config.yaml").read_text())
C     = _CFG["dashboard"]["colors"]


def render(conn, system_state: dict, state_json: str):
    from dashboard.styles import card, badge, signal_color
    from dashboard.jarvis import get_jarvis_response

    # ── Two-column hero layout ──────────────────────────────────────── #
    left, right = st.columns([0.44, 0.56])

    with left:
        st.markdown(
            f"""<div style="padding:40px 20px 20px 0">
            <div style="font-size:92px;font-weight:800;letter-spacing:-4px;
                color:{C['accent']};line-height:1;font-family:'Plus Jakarta Sans',sans-serif;">
            JARVIS</div>
            <div style="font-size:11px;font-weight:600;letter-spacing:0.15em;
                color:{C['muted']};text-transform:uppercase;margin-top:4px;">
            Long/Short Hedge Fund Analyst</div>
            <div style="font-size:11px;color:{C['muted']};margin-top:8px;">
            {_CFG["project"]["name"]}</div>
            </div>""",
            unsafe_allow_html=True,
        )

        # JARVIS chat
        st.markdown(
            f'<div style="color:{C["muted"]};font-size:11px;font-weight:600;'
            f'letter-spacing:0.08em;text-transform:uppercase;margin:24px 0 8px;">Ask JARVIS</div>',
            unsafe_allow_html=True,
        )

        if "jarvis_history" not in st.session_state:
            st.session_state.jarvis_history = []

        # Display last 3 turns
        for msg in st.session_state.jarvis_history[-6:]:
            role_color = C["accent"] if msg["role"] == "user" else C["text"]
            role_label = "You" if msg["role"] == "user" else "JARVIS"
            st.markdown(
                f'<div style="margin:4px 0;font-size:13px;">'
                f'<span style="color:{role_color};font-weight:600;">{role_label}:</span> '
                f'{msg["content"]}</div>',
                unsafe_allow_html=True,
            )

        user_input = st.text_input("", placeholder="Ask about positions, risk, or market view...",
                                   key="jarvis_input", label_visibility="collapsed")
        if st.button("Ask", key="jarvis_ask") and user_input:
            with st.spinner("JARVIS thinking..."):
                reply, updated = get_jarvis_response(
                    user_input, st.session_state.jarvis_history, state_json
                )
                st.session_state.jarvis_history = updated
            st.rerun()

    with right:
        st.markdown(
            f"""<div style="height:300px;background:linear-gradient(135deg,
            {C['card_from']},{C['card_to']},{C['accent']}11);
            border-radius:20px;border:1px solid {C['border']};
            display:flex;align-items:center;justify-content:center;">
            <div style="text-align:center;color:{C['muted']};">
            <div style="font-size:80px;">🤖</div>
            <div style="font-size:12px;letter-spacing:0.1em;margin-top:8px;">
            MERIDIAN CAPITAL PARTNERS</div>
            </div></div>""",
            unsafe_allow_html=True,
        )

    # ── 10-metric status strip ──────────────────────────────────────── #
    st.markdown("---")
    scores = system_state.get("scores", {})
    risk   = system_state.get("risk", {})
    portfolio = system_state.get("portfolio", {})
    earnings_7d = system_state.get("earnings_next_7d", [])
    insider     = system_state.get("insider_30d", {})

    vix = risk.get("vix")
    vix_label = "N/A" if vix is None else f"{vix:.1f}"
    vix_regime = (
        "HIGH VOL" if vix and vix >= 35 else
        "ELEVATED" if vix and vix >= 25 else
        "NORMAL"   if vix and vix >= 15 else
        "LOW VOL"  if vix else "N/A"
    )

    cols = st.columns(10)
    metrics = [
        ("Universe",       f"{scores.get('total_scored', 503)}"),
        ("LONG Cand.",     f"{scores.get('long_candidates', '—')}"),
        ("SHORT Cand.",    f"{scores.get('short_candidates', '—')}"),
        ("Positions",      f"{portfolio.get('total_positions', 0)}"),
        ("Factor Alerts",  f"{len(risk.get('factor_alerts', []))}"),
        ("Insider Events", f"{insider.get('ceo_cfo_buys', 0)} CEO/CFO"),
        ("Cluster Buys",   f"{len(insider.get('cluster_buy_tickers', []))}"),
        ("VIX",            vix_label),
        ("Earnings 7d",    f"{len(earnings_7d)}"),
        ("Alert Level",    risk.get("alert_level", "GREEN")),
    ]
    for col, (label, val) in zip(cols, metrics):
        col.metric(label, val)

    # ── VIX regime badge + data sources ────────────────────────────── #
    alert_color = {
        "GREEN": "#10b981", "MEDIUM": "#f59e0b",
        "HIGH": "#ef4444",  "CRITICAL": "#dc2626",
    }.get(risk.get("alert_level", "GREEN"), C["accent"])

    st.markdown(
        f'<div style="display:flex;gap:12px;margin-top:8px;">'
        f'{badge("VIX: " + vix_regime, "#6366f1")}'
        f'{badge("yfinance", C["muted"])}'
        f'{badge("SEC EDGAR", C["muted"])}'
        f'{badge("Layer 1-5 Active", "#10b981")}'
        f'{badge(risk.get("alert_level","GREEN"), alert_color)}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Upcoming earnings strip ─────────────────────────────────────── #
    if earnings_7d:
        tickers_str = " · ".join(f"{e['ticker']} ({e['date']})" for e in earnings_7d[:8])
        st.markdown(
            f'<div style="margin-top:12px;font-size:11px;color:{C["muted"]};">'
            f'Earnings next 7 days: <span style="color:{C["text"]};">{tickers_str}</span></div>',
            unsafe_allow_html=True,
        )
