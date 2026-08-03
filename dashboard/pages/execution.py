"""Page V — EXECUTION: Order approval, Alpaca submission, fill tracking, slippage."""

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
import yaml

_CFG = yaml.safe_load((Path(__file__).parent.parent.parent / "config.yaml").read_text())
C    = _CFG["dashboard"]["colors"]
NAV  = _CFG["portfolio"]["nav"]


def render(conn, system_state: dict, state_json: str):
    from portfolio.state import get_positions

    positions = get_positions(conn)

    # ── Live Alpaca account bar ──────────────────────────────────────── #
    try:
        from execution.alpaca_client import get_account
        acct = get_account()
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Paper Equity",    f"${acct['equity']:,.0f}")
        col2.metric("Cash",            f"${acct['cash']:,.0f}")
        col3.metric("Buying Power",    f"${acct['buying_power']:,.0f}")
        col4.metric("Open Positions",  len(positions))
    except Exception as e:
        st.warning(f"Alpaca connection: {e}")
        col1, col2 = st.columns(2)
        col1.metric("Open Positions", len(positions))

    # ── Slippage KPI from filled orders ─────────────────────────────── #
    try:
        filled = pd.read_sql(
            """SELECT ticker, notes FROM position_approvals
               WHERE status='filled' ORDER BY decided_at DESC LIMIT 100""",
            conn,
        )
        if not filled.empty:
            # Parse slippage_bps from notes field  ("order_id=… fill=… slippage=+3.2bps")
            def _parse_slip(note):
                if not note:
                    return None
                for part in str(note).split():
                    if part.startswith("slippage="):
                        try:
                            return float(part.replace("slippage=","").replace("bps",""))
                        except Exception:
                            pass
                return None
            filled["slip"] = filled["notes"].apply(_parse_slip)
            avg_slip = filled["slip"].dropna().mean()
            st.caption(f"Avg slippage (last {len(filled)} fills): {avg_slip:+.1f} bps" if not pd.isna(avg_slip) else "")
    except Exception:
        pass

    # ── Pending approvals ────────────────────────────────────────────── #
    st.markdown("### Pending Approvals")
    st.caption("Review proposed trades from the portfolio optimizer. Approve individual trades, then click Execute.")

    pending = pd.read_sql(
        "SELECT * FROM position_approvals WHERE status='pending' ORDER BY created_at",
        conn,
    )
    approved_df = pd.read_sql(
        "SELECT * FROM position_approvals WHERE status='approved' ORDER BY created_at",
        conn,
    )

    if pending.empty and approved_df.empty:
        st.info("No pending orders. Run `python run_portfolio.py --rebalance` to generate trades.")
    else:
        # ── Pending (not yet approved) ─────────────────────────────── #
        if not pending.empty:
            st.markdown("**Awaiting approval:**")
            for _, row in pending.iterrows():
                col_info, col_apv, col_rej = st.columns([4, 1, 1])
                signal = "BUY" if (row["book"] == "long" and row["action"] == "open") or \
                                   (row["book"] == "short" and row["action"] == "close") else "SELL"
                color  = C["long"] if signal == "BUY" else C["short"]
                col_info.markdown(
                    f'<span style="color:{color};font-weight:700;">{signal}</span> '
                    f'**{row["ticker"]}** — {row["book"]} {row["action"]} '
                    f'{int(row["shares"])} shares @ ~${row["estimated_price"]:.2f} '
                    f'| est. cost {row["cost_bps"]:.1f} bps',
                    unsafe_allow_html=True,
                )
                if col_apv.button("Approve", key=f"apv_{row['id']}"):
                    conn.execute(
                        "UPDATE position_approvals SET status='approved', decided_at=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), int(row["id"])),
                    )
                    conn.commit()
                    st.rerun()
                if col_rej.button("Reject", key=f"rej_{row['id']}"):
                    conn.execute(
                        "UPDATE position_approvals SET status='rejected', decided_at=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), int(row["id"])),
                    )
                    conn.commit()
                    st.rerun()

        # ── Approved (ready to execute) ────────────────────────────── #
        if not approved_df.empty:
            st.markdown(f"**Ready to execute ({len(approved_df)} trades approved):**")
            disp_cols = ["ticker","book","action","shares","estimated_price","cost_bps"]
            st.dataframe(
                approved_df[[c for c in disp_cols if c in approved_df.columns]].rename(columns={
                    "ticker":"Ticker","book":"Book","action":"Action",
                    "shares":"Shares","estimated_price":"Est. Price","cost_bps":"Cost (bps)"
                }),
                use_container_width=True, hide_index=True,
            )

            col_exec, col_dry, col_gap = st.columns([1, 1, 3])

            if col_dry.button("Dry Run", help="Simulate without sending to Alpaca"):
                _run_execution_ui(conn, dry_run=True)

            if col_exec.button("Execute All", type="primary",
                               help="Submit approved trades to Alpaca paper trading"):
                _run_execution_ui(conn, dry_run=False)

        # ── Approve-all shortcut ───────────────────────────────────── #
        if not pending.empty:
            st.divider()
            if st.button("Approve All Pending", help="Mark all pending trades as approved in one click"):
                conn.execute(
                    "UPDATE position_approvals SET status='approved', decided_at=? WHERE status='pending'",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                conn.commit()
                st.rerun()

    # ── Fill history ─────────────────────────────────────────────────── #
    st.markdown("### Fill History")
    try:
        filled_hist = pd.read_sql(
            """SELECT ticker, book, action, shares, estimated_price,
                      notes, decided_at
               FROM position_approvals
               WHERE status='filled'
               ORDER BY decided_at DESC LIMIT 100""",
            conn,
        )
        if not filled_hist.empty:
            # Extract fill price and slippage from notes
            def _extract(note, key):
                for part in str(note or "").split():
                    if part.startswith(key + "="):
                        return part.split("=", 1)[1].replace("$","").replace("bps","")
                return "—"

            filled_hist["Fill Price"] = filled_hist["notes"].apply(lambda n: _extract(n, "fill"))
            filled_hist["Slippage (bps)"] = filled_hist["notes"].apply(lambda n: _extract(n, "slippage"))
            st.dataframe(
                filled_hist[["ticker","book","action","shares","estimated_price","Fill Price","Slippage (bps)","decided_at"]].rename(columns={
                    "ticker":"Ticker","book":"Book","action":"Action","shares":"Shares",
                    "estimated_price":"Est. Price","decided_at":"Fill Time",
                }),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No fills yet. Approve and execute trades above.")
    except Exception as e:
        st.info(f"Fill history: {e}")

    # ── Current positions from Alpaca (ground truth) ─────────────────── #
    st.markdown("### Positions (Alpaca)")
    try:
        from execution.alpaca_client import get_open_positions
        alpaca_positions = get_open_positions()
        if alpaca_positions:
            alpaca_df = pd.DataFrame(alpaca_positions)
            alpaca_df["unrealized_pnl"] = alpaca_df["unrealized_pnl"].map(lambda x: f"${x:+,.2f}")
            alpaca_df["avg_entry"]      = alpaca_df["avg_entry"].map(lambda x: f"${x:.2f}")
            alpaca_df["current_price"]  = alpaca_df["current_price"].map(lambda x: f"${x:.2f}")
            st.dataframe(
                alpaca_df[["ticker","side","qty","avg_entry","current_price","unrealized_pnl"]].rename(columns={
                    "ticker":"Ticker","side":"Side","qty":"Qty",
                    "avg_entry":"Avg Entry","current_price":"Price","unrealized_pnl":"Unrealized P&L",
                }),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No open positions in Alpaca account.")
    except Exception as e:
        st.info(f"Alpaca positions: {e}")

    # ── Short availability ────────────────────────────────────────────── #
    st.markdown("### Short Availability")
    try:
        short_tickers = positions[positions["book"] == "short"]["ticker"].tolist() if not positions.empty else []
        if short_tickers:
            placeholders = ",".join("?" * len(short_tickers))
            si_data = pd.read_sql(
                f"""SELECT s.ticker, s.short_percent_of_float, s.short_ratio, s.shares_short
                    FROM short_interest s
                    INNER JOIN (SELECT ticker, MAX(date) md FROM short_interest
                                WHERE ticker IN ({placeholders}) GROUP BY ticker)
                    lp ON s.ticker=lp.ticker AND s.date=lp.md""",
                conn, params=short_tickers,
            )
            if not si_data.empty:
                st.dataframe(si_data.rename(columns={
                    "ticker":"Ticker","short_percent_of_float":"Short % Float",
                    "short_ratio":"Days-to-Cover","shares_short":"Shares Short",
                }), use_container_width=True, hide_index=True)
        else:
            st.info("No short positions yet.")
    except Exception as e:
        st.info(f"Short availability: {e}")


def _run_execution_ui(conn, dry_run: bool):
    """Call the execution engine and surface results in the Streamlit UI."""
    from execution.order_manager import run_execution

    label = "Dry Run" if dry_run else "Execution"
    with st.spinner(f"Running {label}..."):
        try:
            summary = run_execution(conn, dry_run=dry_run)
        except ConnectionError as e:
            st.error(f"Alpaca connection failed: {e}")
            return
        except Exception as e:
            st.error(f"Execution error: {e}")
            return

    filled  = summary.get("filled", 0)
    vetoed  = summary.get("vetoed", 0)
    errors  = summary.get("errors", 0)

    if dry_run:
        st.success(f"Dry run complete — {summary.get('processed',0)} trade(s) simulated, no orders sent.")
    elif filled > 0:
        st.success(f"Execution complete — {filled} fill(s), {vetoed} vetoed, {errors} error(s).")
    else:
        st.warning(f"Execution finished — 0 fills. Vetoed: {vetoed}, Errors: {errors}.")

    for d in summary.get("details", []):
        if d["status"] == "filled":
            st.info(
                f"FILLED {d['ticker']}: {d['filled_qty']:.0f} shares @ ${d['fill_price']:.4f} "
                f"| slippage {d['slippage_bps']:+.1f} bps"
            )
        elif d["status"] == "vetoed":
            st.warning(f"VETOED {d['ticker']}: {d.get('reason','')}")
        elif d["status"] == "error":
            st.error(f"ERROR {d['ticker']}: {d.get('reason','')}")

    st.rerun()
