"""
Meridian Capital Partners — JARVIS Dashboard
Served at http://localhost:8502

6-page dark-theme Streamlit app with Roman-numeral nav pill bar.
Auto-refreshes every 5 minutes during market hours (9:30–16:00 ET).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
from streamlit_autorefresh import st_autorefresh
import yaml
from datetime import datetime, time as dtime
import pytz

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_CFG = yaml.safe_load(_CONFIG_PATH.read_text())
D    = _CFG["dashboard"]
C    = D["colors"]

st.set_page_config(
    page_title="JARVIS · Meridian Capital Partners",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS ──────────────────────────────────────────────────────────────────── #
from dashboard.styles import inject_css
inject_css()

# ── Auto-refresh during market hours ─────────────────────────────────────── #
ET   = pytz.timezone("America/New_York")
now  = datetime.now(ET).time()
mkt_open  = dtime(9, 30)
mkt_close = dtime(16, 0)
in_market = mkt_open <= now <= mkt_close

if in_market:
    st_autorefresh(interval=D["refresh_interval_seconds"] * 1000, key="market_refresh")

# ── DB connection ─────────────────────────────────────────────────────────── #
from data.db import get_connection, init_db
init_db()
conn = get_connection()

# ── System state (built once per session or refresh) ─────────────────────── #
@st.cache_data(ttl=60, show_spinner=False)
def _load_state(_conn_dummy: int) -> tuple[dict, str]:
    from dashboard.state import build_system_state, state_to_context
    state = build_system_state(conn)
    return state, state_to_context(state)

system_state, state_json = _load_state(id(conn))

# ── Navigation ────────────────────────────────────────────────────────────── #
st.markdown(
    f'<div style="display:flex;align-items:center;gap:16px;padding:12px 0 4px;">'
    f'<span style="font-size:18px;font-weight:700;color:{C["accent"]};'
    f'letter-spacing:-0.5px;">JARVIS</span>'
    f'<span style="color:{C["muted"]};font-size:11px;">·</span>'
    f'<span style="color:{C["muted"]};font-size:11px;">{_CFG["project"]["name"]}</span>'
    f'<span style="color:{C["muted"]};font-size:11px;margin-left:auto;">'
    f'{"🟢 Market Open" if in_market else "⚫ Market Closed"}</span>'
    f'</div>',
    unsafe_allow_html=True,
)

PAGES = [
    "I · PORTFOLIO",
    "II · RESEARCH",
    "III · RISK",
    "IV · PERFORMANCE",
    "V · EXECUTION",
    "VI · LETTER",
]

page = st.radio("nav", PAGES, horizontal=True, key="main_nav",
                label_visibility="collapsed")

st.markdown('<hr style="border-color:#1e2d3d;margin:8px 0 16px;">', unsafe_allow_html=True)

# ── Page routing ──────────────────────────────────────────────────────────── #
if "PORTFOLIO" in page:
    from dashboard.pages import portfolio
    portfolio.render(conn, system_state, state_json)

elif "RESEARCH" in page:
    from dashboard.pages import research
    research.render(conn, system_state, state_json)

elif "RISK" in page:
    from dashboard.pages import risk
    risk.render(conn, system_state, state_json)

elif "PERFORMANCE" in page:
    from dashboard.pages import performance
    performance.render(conn, system_state, state_json)

elif "EXECUTION" in page:
    from dashboard.pages import execution
    execution.render(conn, system_state, state_json)

elif "LETTER" in page:
    from dashboard.pages import letter
    letter.render(conn, system_state, state_json)

conn.close()
