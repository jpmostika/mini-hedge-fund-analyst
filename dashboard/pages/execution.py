"""Page V — EXECUTION: Order status, trade log, slippage, short availability."""

import streamlit as st
import pandas as pd
from pathlib import Path
import yaml

_CFG = yaml.safe_load((Path(__file__).parent.parent.parent / "config.yaml").read_text())
C    = _CFG["dashboard"]["colors"]
NAV  = _CFG["portfolio"]["nav"]


def render(conn, system_state: dict, state_json: str):
    from portfolio.state import get_pending_approvals, get_positions

    # ── KPI row ─────────────────────────────────────────────────────── #
    approvals = get_pending_approvals(conn)
    positions = get_positions(conn)
    history   = pd.read_sql(
        "SELECT * FROM portfolio_history ORDER BY timestamp DESC LIMIT 200", conn
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Filled Orders (30d)",  len(history[history["timestamp"] >= pd.Timestamp.now().strftime("%Y-%m-%d")
                                             .replace(pd.Timestamp.now().strftime("%d"),
                                                      str(max(1, pd.Timestamp.now().day - 30)))]) if not history.empty else 0)
    col2.metric("Avg Slippage",         "N/A (Layer 6 pending)")
    col3.metric("Open Orders",          len(approvals))
    col4.metric("Total Positions",      len(positions))

    # ── Open orders (pending approvals) ─────────────────────────────── #
    st.markdown("### Pending Approvals")
    if not approvals.empty:
        display_cols = ["ticker","book","action","shares","estimated_price","cost_bps","status","created_at"]
        avail = [c for c in display_cols if c in approvals.columns]
        st.dataframe(
            approvals[avail].rename(columns={
                "ticker": "Ticker", "book": "Book", "action": "Action",
                "shares": "Shares", "estimated_price": "Price",
                "cost_bps": "Cost (bps)", "status": "Status", "created_at": "Created",
            }),
            use_container_width=True, hide_index=True,
        )

        col_exec, col_clear = st.columns([1, 3])
        if col_exec.button("Execute All (→ Alpaca)", type="primary"):
            st.warning("Layer 6 (Alpaca execution) not yet built. Trades are queued.")
    else:
        st.info("No pending orders. Run `python run_portfolio.py --rebalance` to generate trades.")

    # ── Recent trade history ─────────────────────────────────────────── #
    st.markdown("### Trade History (Last 200)")
    if not history.empty:
        st.dataframe(
            history[["timestamp","ticker","book","action","shares","price","cost_bps","reason"]]
            .rename(columns={
                "timestamp": "Time", "ticker": "Ticker", "book": "Book",
                "action": "Action", "shares": "Shares", "price": "Price",
                "cost_bps": "Cost (bps)", "reason": "Reason",
            }),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No trade history yet. Trades appear here after approval and execution.")

    # ── Short availability panel ─────────────────────────────────────── #
    st.markdown("### Short Availability")
    try:
        short_positions = positions[positions["book"] == "short"]["ticker"].tolist() if not positions.empty else []
        if short_positions:
            placeholders = ",".join("?" * len(short_positions))
            si_data = pd.read_sql(
                f"""SELECT s.ticker, s.short_percent_of_float, s.short_ratio,
                           s.shares_short FROM short_interest s
                    INNER JOIN (SELECT ticker, MAX(date) md FROM short_interest
                                WHERE ticker IN ({placeholders}) GROUP BY ticker)
                    lp ON s.ticker=lp.ticker AND s.date=lp.md""",
                conn, params=short_positions,
            )
            if not si_data.empty:
                st.dataframe(
                    si_data.rename(columns={
                        "ticker": "Ticker",
                        "short_percent_of_float": "Short % Float",
                        "short_ratio": "Days-to-Cover",
                        "shares_short": "Shares Short",
                    }),
                    use_container_width=True, hide_index=True,
                )
        else:
            st.info("No short positions in portfolio.")
    except Exception as e:
        st.info(f"Short availability: {e}")

    # ── Note on Layer 6 ─────────────────────────────────────────────── #
    st.markdown(
        f'<div style="margin-top:20px;padding:16px;background:linear-gradient(135deg,'
        f'{C["card_from"]},{C["card_to"]});border:1px solid {C["border"]};border-radius:10px;">'
        f'<div style="color:{C["muted"]};font-size:12px;">'
        f'Layer 6 (Alpaca Execution) — coming next. When built, this page will show '
        f'live fills, slippage vs model, and real-time order status.</div></div>',
        unsafe_allow_html=True,
    )
