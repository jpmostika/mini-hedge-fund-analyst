"""
Tail Risk Monitor — regime detection via VIX and credit spreads.
Actions are mandatory (no override):
  VIX >= 25 → REDUCE_GROSS_20%
  VIX >= 35 → REDUCE_GROSS_50%
  HY credit spread z-score >= 1 sigma widening → REDUCE_GROSS_20%

Pulls BAMLH0A0HYM2 from FRED if FRED_API_KEY is configured.
Falls back to TLT/HYG ratio as credit spread proxy.
"""

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_R = _CFG["risk"]
VIX_25 = _R["vix_reduce_20_threshold"]
VIX_35 = _R["vix_reduce_50_threshold"]
CS_SIG = _R["credit_spread_sigma_threshold"]


class TailAction:
    NONE           = "NONE"
    REDUCE_GROSS_20 = "REDUCE_GROSS_20%"
    REDUCE_GROSS_50 = "REDUCE_GROSS_50%"


def _get_current_vix(conn: sqlite3.Connection) -> Optional[float]:
    row = conn.execute(
        "SELECT close FROM daily_prices WHERE ticker='^VIX' ORDER BY date DESC LIMIT 1"
    ).fetchone()
    return float(row[0]) if row and row[0] else None


def _get_credit_spread_zscore(conn: sqlite3.Connection) -> Optional[float]:
    """
    Try FRED BAMLH0A0HYM2 first, fall back to HYG/TLT price ratio inversion.
    Returns z-score of current spread vs 252-day history (positive = widening).
    """
    fred_key = os.getenv("FRED_API_KEY", "").strip()
    if fred_key:
        try:
            from data.providers import get_providers
            providers = get_providers()
            df = providers.get_macro_series(_R["hy_spread_series"], "2024-01-01",
                                            pd.Timestamp.today().strftime("%Y-%m-%d"))
            if df is not None and not df.empty:
                df = df.dropna().tail(252)
                if len(df) > 30:
                    current = df["value"].iloc[-1]
                    mu  = df["value"].mean()
                    std = df["value"].std()
                    return float((current - mu) / std) if std > 0 else 0.0
        except Exception as e:
            logger.debug(f"FRED credit spread failed: {e}")

    # Fallback: HYG (HY bond ETF) / TLT (long treasury) ratio — declining = spread widening
    try:
        hyg = pd.read_sql(
            "SELECT date, close FROM daily_prices WHERE ticker='HYG' ORDER BY date DESC LIMIT 252",
            conn,
        ).set_index("date")["close"]
        tlt = pd.read_sql(
            "SELECT date, close FROM daily_prices WHERE ticker='TLT' ORDER BY date DESC LIMIT 252",
            conn,
        ).set_index("date")["close"]

        ratio = (hyg / tlt).dropna()
        if len(ratio) < 30:
            return None

        # Invert ratio change: ratio falling → spread widening → positive z-score
        current = ratio.iloc[-1]
        mu  = ratio.mean()
        std = ratio.std()
        z   = (mu - current) / std if std > 0 else 0.0  # inverted: lower ratio = higher spread
        return float(z)
    except Exception as e:
        logger.debug(f"HYG/TLT spread fallback failed: {e}")
        return None


def check_tail_risk(
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """
    Returns dict with action, vix, credit_z, and reasoning.
    Action is the most severe triggered threshold.
    """
    from data.db import get_connection
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    try:
        vix       = _get_current_vix(conn)
        credit_z  = _get_credit_spread_zscore(conn)
        action    = TailAction.NONE
        triggers  = []

        # VIX checks (most severe wins)
        if vix is not None:
            if vix >= VIX_35:
                action = TailAction.REDUCE_GROSS_50
                triggers.append(f"VIX={vix:.1f} >= {VIX_35} → {action}")
                logger.critical(f"[tail_risk] {triggers[-1]}")
            elif vix >= VIX_25:
                action = TailAction.REDUCE_GROSS_20
                triggers.append(f"VIX={vix:.1f} >= {VIX_25} → {action}")
                logger.warning(f"[tail_risk] {triggers[-1]}")

        # Credit spread
        if credit_z is not None and credit_z >= CS_SIG:
            cs_action = TailAction.REDUCE_GROSS_20
            triggers.append(f"Credit spread z={credit_z:.2f} >= {CS_SIG} sigma → {cs_action}")
            logger.warning(f"[tail_risk] {triggers[-1]}")
            # Escalate if credit AND VIX both firing
            if action == TailAction.NONE:
                action = cs_action

        if not triggers:
            logger.info(
                f"[tail_risk] OK | VIX={vix if vix else 'N/A'} "
                f"credit_z={f'{credit_z:.2f}' if credit_z is not None else 'N/A'}"
            )

        return {
            "action":   action,
            "vix":      round(vix, 1) if vix else None,
            "credit_z": round(credit_z, 2) if credit_z else None,
            "triggers": triggers,
        }

    finally:
        if close_conn:
            conn.close()
