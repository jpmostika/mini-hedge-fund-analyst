"""Dark theme CSS for Meridian Capital Partners dashboard."""

import yaml
from pathlib import Path

_CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
C    = _CFG["dashboard"]["colors"]

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

/* ── Hide Streamlit chrome ── */
#MainMenu, footer, header {{visibility: hidden;}}
.stDeployButton {{display: none;}}
[data-testid="stToolbar"] {{display: none;}}
[data-testid="stDecoration"] {{display: none;}}
.stApp > header {{display: none;}}

/* ── Global ── */
html, body, .stApp, [data-testid="stAppViewContainer"] {{
    background-color: {C['bg']} !important;
    color: {C['text']};
    font-family: 'Plus Jakarta Sans', sans-serif;
}}
[data-testid="stSidebar"] {{display: none;}}

/* ── Nav pill bar ── */
div[data-testid="stHorizontalBlock"] .stRadio > div {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    background: transparent;
}}
div[data-testid="stHorizontalBlock"] .stRadio label {{
    background: {C['card_from']};
    border: 1px solid {C['border']};
    border-radius: 20px;
    padding: 6px 18px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.05em;
    color: {C['muted']};
    transition: all 0.2s;
}}
div[data-testid="stHorizontalBlock"] .stRadio label:hover {{
    border-color: {C['accent']};
    color: {C['text']};
}}
div[data-testid="stHorizontalBlock"] .stRadio [data-baseweb="radio"] input:checked + div + label,
div[data-testid="stHorizontalBlock"] .stRadio [aria-checked="true"] + label {{
    background: linear-gradient(135deg, {C['accent']}33, {C['accent']}22);
    border-color: {C['accent']};
    color: {C['accent']};
}}

/* ── Metric cards ── */
[data-testid="metric-container"] {{
    background: linear-gradient(135deg, {C['card_from']}, {C['card_to']});
    border: 1px solid {C['border']};
    border-radius: 10px;
    padding: 16px;
}}
[data-testid="metric-container"] [data-testid="stMetricLabel"] {{
    color: {C['muted']} !important;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
}}
[data-testid="metric-container"] [data-testid="stMetricValue"] {{
    color: {C['text']} !important;
    font-family: 'JetBrains Mono', monospace;
    font-size: 22px;
}}

/* ── Dataframes ── */
[data-testid="stDataFrame"] {{
    border: 1px solid {C['border']};
    border-radius: 8px;
}}

/* ── Section headers ── */
h1, h2, h3 {{
    color: {C['text']} !important;
    font-family: 'Plus Jakarta Sans', sans-serif;
}}

/* ── Input / selectbox ── */
[data-testid="stTextInput"] input,
[data-baseweb="select"] {{
    background-color: {C['card_from']} !important;
    border-color: {C['border']} !important;
    color: {C['text']} !important;
}}

/* ── Buttons ── */
.stButton button {{
    background: linear-gradient(135deg, {C['accent']}, {C['accent']}cc);
    border: none;
    border-radius: 6px;
    color: white;
    font-weight: 600;
    font-size: 12px;
    padding: 8px 20px;
}}
.stButton button:hover {{
    opacity: 0.85;
    transform: translateY(-1px);
}}

/* ── Alerts ── */
[data-testid="stAlert"] {{
    border-radius: 8px;
    border-left: 3px solid {C['accent']};
}}

/* ── Expander ── */
[data-testid="stExpander"] {{
    background: linear-gradient(135deg, {C['card_from']}, {C['card_to']});
    border: 1px solid {C['border']};
    border-radius: 8px;
}}

/* ── Long/short color helpers ── */
.long-text  {{ color: {C['long']}; }}
.short-text {{ color: {C['short']}; }}
.accent     {{ color: {C['accent']}; }}
.muted      {{ color: {C['muted']}; font-size: 12px; }}
.mono       {{ font-family: 'JetBrains Mono', monospace; }}

/* ── Card wrapper ── */
.mcp-card {{
    background: linear-gradient(135deg, {C['card_from']}, {C['card_to']});
    border: 1px solid {C['border']};
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 12px;
}}
</style>
"""


def inject_css():
    import streamlit as st
    st.markdown(CSS, unsafe_allow_html=True)


def card(content_html: str, padding: str = "20px") -> str:
    """Wrap HTML in a styled card."""
    bg   = C["card_from"]
    bg2  = C["card_to"]
    bdr  = C["border"]
    return (
        f'<div style="background:linear-gradient(135deg,{bg},{bg2});'
        f'border:1px solid {bdr};border-radius:12px;padding:{padding};margin-bottom:12px;">'
        f'{content_html}</div>'
    )


def badge(text: str, color: str = None) -> str:
    color = color or C["accent"]
    return (
        f'<span style="background:{color}22;color:{color};border:1px solid {color}44;'
        f'border-radius:12px;padding:2px 10px;font-size:11px;font-weight:600;">{text}</span>'
    )


def signal_color(signal: str) -> str:
    if signal == "LONG":  return C["long"]
    if signal == "SHORT": return C["short"]
    return C["muted"]
