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

    # ── Market status banner ─────────────────────────────────────────── #
    try:
        from execution.alpaca_client import get_market_clock
        clock = get_market_clock()
        if clock.get("ok"):
            if clock["is_open"]:
                st.success("Market is **OPEN** — orders submitted now will fill immediately.", icon="🟢")
            else:
                raw = clock.get("next_open", "")
                try:
                    dt = datetime.fromisoformat(raw)
                    next_str = dt.strftime("%A %b %d at %I:%M %p ET").replace(" 0", " ")
                except Exception:
                    next_str = raw
                st.warning(
                    f"Market is **CLOSED** — orders will queue and fill at next open: **{next_str}**",
                    icon="🔴",
                )
    except Exception:
        pass

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
            # Header row
            h1, h2, h3, h4, h5, h6, h7 = st.columns([1, 1.2, 1.2, 1, 1.2, 1, 1])
            for h, label in zip(
                [h1, h2, h3, h4, h5, h6, h7],
                ["Direction", "Ticker", "Book", "Shares", "Notional", "", ""],
            ):
                h.caption(label)
            st.divider()

            for _, row in pending.iterrows():
                action_str = str(row["action"])
                book       = str(row["book"])
                is_open    = "open" in action_str

                if book == "long" and is_open:
                    signal, color = "BUY",   C["long"]
                elif book == "short" and is_open:
                    signal, color = "SHORT",  C["short"]
                elif book == "long" and not is_open:
                    signal, color = "SELL",   C["short"]
                else:
                    signal, color = "COVER",  C["long"]

                shares   = float(row["shares"])
                price    = float(row["estimated_price"])
                notional = shares * price

                c1, c2, c3, c4, c5, c6, c7 = st.columns([1, 1.2, 1.2, 1, 1.2, 1, 1])
                c1.markdown(f'<span style="color:{color};font-weight:700;">{signal}</span>', unsafe_allow_html=True)
                c2.markdown(f"**{row['ticker']}**")
                c3.markdown(f"{book}")
                c4.markdown(f"{int(shares):,}")
                c5.markdown(f"${notional:,.0f}")
                if c6.button("Approve", key=f"apv_{row['id']}"):
                    conn.execute(
                        "UPDATE position_approvals SET status='approved', decided_at=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), int(row["id"])),
                    )
                    conn.commit()
                    st.rerun()
                if c7.button("Reject", key=f"rej_{row['id']}"):
                    conn.execute(
                        "UPDATE position_approvals SET status='rejected', decided_at=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), int(row["id"])),
                    )
                    conn.commit()
                    st.rerun()

        # ── Approved (ready to execute) ────────────────────────────── #
        if not approved_df.empty:
            st.markdown(f"**Ready to execute ({len(approved_df)} trades approved):**")
            disp = approved_df[["ticker","book","action","shares","estimated_price"]].copy()
            disp["notional"] = (disp["shares"] * disp["estimated_price"]).map("${:,.0f}".format)
            disp["estimated_price"] = disp["estimated_price"].map("${:.2f}".format)
            st.dataframe(
                disp.rename(columns={
                    "ticker":"Ticker","book":"Book","action":"Action",
                    "shares":"Shares","estimated_price":"Price","notional":"Notional",
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
    st.caption("Click Close to queue a closing order — it will appear in Pending Approvals above.")
    try:
        from execution.alpaca_client import get_open_positions
        alpaca_positions = get_open_positions()
        if alpaca_positions:
            # Header
            h1,h2,h3,h4,h5,h6,h7 = st.columns([1.2, 1, 1, 1.2, 1.2, 1.5, 0.8])
            for h, lbl in zip([h1,h2,h3,h4,h5,h6,h7],
                               ["Ticker","Side","Qty","Avg Entry","Price","Unrealized P&L",""]):
                h.caption(lbl)
            st.divider()

            from portfolio.state import queue_approval
            for pos in alpaca_positions:
                c1,c2,c3,c4,c5,c6,c7 = st.columns([1.2, 1, 1, 1.2, 1.2, 1.5, 0.8])
                pnl   = pos["unrealized_pnl"]
                color = C["long"] if pnl >= 0 else C["short"]
                c1.markdown(f"**{pos['ticker']}**")
                c2.markdown(pos["side"])
                c3.markdown(f"{int(pos['qty']):,}")
                c4.markdown(f"${pos['avg_entry']:.2f}")
                c5.markdown(f"${pos['current_price']:.2f}")
                c6.markdown(
                    f'<span style="color:{color}">${pnl:+,.2f}</span>',
                    unsafe_allow_html=True,
                )
                if c7.button("Close", key=f"close_{pos['ticker']}"):
                    book   = "long" if pos["side"] == "long" else "short"
                    action = f"close_{book}"
                    queue_approval(
                        ticker          = pos["ticker"],
                        book            = book,
                        action          = action,
                        shares          = abs(pos["qty"]),
                        estimated_price = pos["current_price"],
                        cost_bps        = 15.0,
                        notes           = "manual_close",
                        conn            = conn,
                    )
                    st.success(f"Close order queued for {pos['ticker']} — scroll up to approve and execute.")
                    st.rerun()
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
