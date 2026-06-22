"""Page VI — LETTER: Daily LP letter with JARVIS-authored body, letterhead, compliance."""

import streamlit as st
from pathlib import Path
import yaml, json

_CFG = yaml.safe_load((Path(__file__).parent.parent.parent / "config.yaml").read_text())
C    = _CFG["dashboard"]["colors"]


def render(conn, system_state: dict, state_json: str):
    from reporting.commentary import generate_lp_letter, clear_letter_cache

    risk_file = Path(__file__).parent.parent.parent / _CFG["risk"]["risk_state_file"]
    risk_state = {}
    if risk_file.exists():
        try:
            risk_state = json.loads(risk_file.read_text())
        except Exception:
            pass

    # Attribution for today
    attribution = {}
    try:
        from reporting.pnl_attribution import load_attribution_history
        attr_df = load_attribution_history()
        if not attr_df.empty:
            attribution = attr_df.iloc[-1].to_dict()
    except Exception:
        pass

    col_title, col_btn = st.columns([3, 1])
    with col_title:
        st.markdown("### Daily LP Letter")
        st.caption(
            f"Fund: {_CFG['reporting']['fund_name']} · "
            f"Domicile: {_CFG['reporting']['fund_domicile']} · "
            f"Inception: {_CFG['reporting']['fund_inception']}"
        )
    with col_btn:
        if st.button("🔄 Regenerate Letter"):
            clear_letter_cache()
            st.rerun()

    with st.spinner("Generating LP letter via JARVIS..."):
        letter = generate_lp_letter(
            risk_state  = risk_state,
            attribution = attribution,
            nav         = _CFG["portfolio"]["nav"],
        )

    # Render in a styled card
    st.markdown(
        f'<div style="background:linear-gradient(135deg,{C["card_from"]},{C["card_to"]});'
        f'border:1px solid {C["border"]};border-radius:12px;padding:32px 40px;'
        f'max-width:720px;margin:0 auto;font-family:Georgia,serif;line-height:1.7;">'
        f'{_md_to_html(letter)}</div>',
        unsafe_allow_html=True,
    )

    # Download button
    st.download_button(
        label="Download as Markdown",
        data=letter,
        file_name=f"MCP_LP_Letter_{__import__('datetime').date.today().isoformat()}.md",
        mime="text/markdown",
    )


def _md_to_html(text: str) -> str:
    """Minimal markdown-to-HTML for letter display."""
    import re
    C_local = _CFG["dashboard"]["colors"]
    lines = []
    for line in text.split("\n"):
        if line.startswith("**") and line.endswith("**"):
            lines.append(f'<strong>{line[2:-2]}</strong>')
        elif line.startswith("*") and line.endswith("*") and not line.startswith("**"):
            lines.append(f'<em style="color:{C_local["muted"]}">{line[1:-1]}</em>')
        elif line.startswith("---"):
            lines.append('<hr style="border-color:#1e2d3d;margin:20px 0;">')
        elif line.startswith("> "):
            lines.append(f'<div style="border-left:3px solid #6366f1;padding:4px 12px;'
                         f'color:{C_local["muted"]};font-size:12px;">{line[2:]}</div>')
        elif line.strip() == "":
            lines.append("<br>")
        else:
            # Bold inline
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            lines.append(f'<p style="margin:8px 0;">{line}</p>')
    return "\n".join(lines)
