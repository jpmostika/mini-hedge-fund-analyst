"""Page IV — PERFORMANCE: Equity curve, attribution, sector alpha, win/loss."""

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import numpy as np
from pathlib import Path
import yaml

_CFG = yaml.safe_load((Path(__file__).parent.parent.parent / "config.yaml").read_text())
C    = _CFG["dashboard"]["colors"]
NAV  = _CFG["portfolio"]["nav"]


def render(conn, system_state: dict, state_json: str):
    from reporting.pnl_attribution import load_attribution_history, sector_alpha

    attr_df = load_attribution_history()

    # ── Equity curve vs SPY ──────────────────────────────────────────── #
    st.markdown("### Equity Curve vs SPY (rebased to 100)")
    try:
        spy_prices = pd.read_sql(
            "SELECT date, close FROM daily_prices WHERE ticker='SPY' ORDER BY date",
            conn,
        )
        if not spy_prices.empty:
            spy_prices["spy_idx"] = spy_prices["close"] / spy_prices["close"].iloc[0] * 100

            fig = go.Figure()
            fig.add_scatter(x=spy_prices["date"], y=spy_prices["spy_idx"],
                            name="SPY", line=dict(color=C["muted"], width=1.5, dash="dot"))

            if not attr_df.empty:
                attr_df["cum_ret"] = (1 + attr_df["port_return"]).cumprod() * 100
                fig.add_scatter(x=attr_df["date"], y=attr_df["cum_ret"],
                                name="Portfolio", line=dict(color=C["accent"], width=2))

            fig.update_layout(
                paper_bgcolor=C["bg"], plot_bgcolor=C["bg"],
                font_color=C["text"], height=250,
                margin=dict(l=20, r=20, t=10, b=40),
                legend=dict(orientation="h", y=1.1),
                yaxis=dict(gridcolor=C["border"]),
                xaxis=dict(gridcolor=C["border"]),
            )
            st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.info(f"Equity curve: {e}")

    # ── Monthly returns grid ─────────────────────────────────────────── #
    if not attr_df.empty:
        st.markdown("### Monthly Returns")
        try:
            attr_df["date"] = pd.to_datetime(attr_df["date"])
            attr_df["month"] = attr_df["date"].dt.to_period("M")
            monthly = attr_df.groupby("month")["port_return"].sum().reset_index()
            monthly["year"]  = monthly["month"].dt.year
            monthly["mon"]   = monthly["month"].dt.month
            pivot = monthly.pivot(index="year", columns="mon", values="port_return").fillna(0)
            pivot.columns = ["Jan","Feb","Mar","Apr","May","Jun",
                             "Jul","Aug","Sep","Oct","Nov","Dec"][:len(pivot.columns)]

            fig = go.Figure(go.Heatmap(
                z=pivot.values * 100,
                x=pivot.columns.tolist(),
                y=[str(y) for y in pivot.index],
                colorscale=[[0, C["short"]], [0.5, C["bg"]], [1, C["long"]]],
                zmid=0, text=(pivot.values * 100).round(1),
                texttemplate="%{text:.1f}%",
                showscale=False,
            ))
            fig.update_layout(
                paper_bgcolor=C["bg"], plot_bgcolor=C["bg"],
                font_color=C["text"], height=150,
                margin=dict(l=40, r=10, t=10, b=30),
            )
            st.plotly_chart(fig, use_container_width=True)
        except Exception:
            pass

    # ── P&L attribution bars ─────────────────────────────────────────── #
    if not attr_df.empty:
        st.markdown("### P&L Attribution")
        comp = ["beta_return","sector_return","factor_return","alpha_return"]
        comp_labels = ["Beta","Sector","Factor","Alpha"]
        totals = attr_df[comp].sum()

        fig = go.Figure(go.Bar(
            x=comp_labels,
            y=(totals * 100).values,
            marker_color=[
                C["muted"],
                C["accent"] + "aa",
                C["long"]  + "aa",
                C["long"] if totals["alpha_return"] >= 0 else C["short"],
            ],
            text=(totals * 100).round(2).astype(str) + "%",
            textposition="outside",
        ))
        fig.update_layout(
            paper_bgcolor=C["bg"], plot_bgcolor=C["bg"],
            font_color=C["text"], height=220,
            margin=dict(l=20, r=20, t=10, b=30),
            yaxis=dict(gridcolor=C["border"],
                       tickformat=".2%",
                       ticksuffix="%"),
        )
        st.plotly_chart(fig, use_container_width=True)

    # ── Sector-relative alpha ────────────────────────────────────────── #
    st.markdown("### Sector-Relative Alpha (90d)")
    try:
        sect_df = sector_alpha(conn, lookback_days=90)
        if not sect_df.empty:
            total_alpha = sect_df["alpha"].sum()
            st.metric("Total Stock-Selection Alpha (90d)", f"{total_alpha*100:.2f}%")
            fig = px.bar(
                sect_df, x="sector", y="alpha",
                color="alpha",
                color_continuous_scale=[[0, C["short"]], [0.5, C["bg"]], [1, C["long"]]],
                text=sect_df["alpha"].map(lambda x: f"{x*100:.1f}%"),
            )
            fig.update_layout(
                paper_bgcolor=C["bg"], plot_bgcolor=C["bg"],
                font_color=C["text"], height=220,
                margin=dict(l=20, r=20, t=10, b=80),
                coloraxis_showscale=False,
                xaxis_tickangle=-30,
            )
            st.plotly_chart(fig, use_container_width=True)
    except Exception:
        st.info("Sector alpha: run daily attribution first")

    # ── Rolling Sharpe + Win/Loss ────────────────────────────────────── #
    col_sharpe, col_wl = st.columns(2)

    with col_sharpe:
        st.markdown("**Rolling 12-Month Sharpe**")
        if not attr_df.empty and len(attr_df) >= 20:
            rolling_mean = attr_df["port_return"].rolling(min(252, len(attr_df))).mean() * 252
            rolling_std  = attr_df["port_return"].rolling(min(252, len(attr_df))).std() * np.sqrt(252)
            sharpe = (rolling_mean / rolling_std.replace(0, np.nan)).dropna()
            if not sharpe.empty:
                current = sharpe.iloc[-1]
                st.metric("Current Sharpe", f"{current:.2f}")
        else:
            st.info("Accumulating history...")

    with col_wl:
        st.markdown("**Win/Loss**")
        try:
            from reporting.pnl_attribution import compute_win_loss
            wl = compute_win_loss(conn)
            wr = wl.get("win_rate")
            if wr is not None:
                st.metric("Win Rate",    f"{wr*100:.1f}%")
                st.metric("Total Trades", wl.get("total_trades", 0))
            else:
                st.info(wl.get("message", "No data"))
        except Exception:
            st.info("No trade history yet")

    # ── Weekly commentary card ───────────────────────────────────────── #
    st.markdown("### JARVIS Weekly Commentary")
    try:
        from pathlib import Path as _P
        letter_dir = _P(__file__).parent.parent.parent / _CFG["reporting"]["letter_cache"]
        import datetime
        today = datetime.date.today()
        wk = today.isocalendar()[1]
        weekly_path = letter_dir / f"weekly_{wk}_{today.year}.md"
        if weekly_path.exists():
            st.markdown(weekly_path.read_text())
        else:
            st.info("Weekly commentary not generated yet — fires automatically on Friday, or run reporting/commentary.py")
    except Exception:
        pass
