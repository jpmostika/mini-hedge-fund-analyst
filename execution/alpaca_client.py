"""
Alpaca paper-trading client — thin wrapper around alpaca-py.

Handles authentication, order submission, fill polling, and account queries.
All order routing goes through this module so swapping to live trading later
only requires changing ALPACA_BASE_URL in .env (remove "paper-").
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_EXEC = _CFG["execution"]
POLL_INTERVAL = _EXEC["fill_poll_interval_s"]
FILL_TIMEOUT  = _EXEC["fill_timeout_s"]


# ── Auth ──────────────────────────────────────────────────────────────── #

def _get_client():
    from alpaca.trading.client import TradingClient
    api_key    = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    base_url   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").strip()
    if not api_key or not secret_key:
        raise EnvironmentError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env\n"
            "Get them at: https://app.alpaca.markets → Paper Trading → API Keys"
        )
    paper = "paper" in base_url
    return TradingClient(api_key, secret_key, paper=paper)


# ── Account ───────────────────────────────────────────────────────────── #

def get_account() -> dict:
    """Return key account fields as a plain dict."""
    client  = _get_client()
    account = client.get_account()
    return {
        "equity":          float(account.equity),
        "cash":            float(account.cash),
        "buying_power":    float(account.buying_power),
        "portfolio_value": float(account.portfolio_value),
        "shorting_enabled": account.shorting_enabled,
        "paper_trading":   True,
        "status":          account.status,
    }


def get_open_positions() -> list[dict]:
    """Return all currently open Alpaca positions as plain dicts."""
    client    = _get_client()
    positions = client.get_all_positions()
    return [
        {
            "ticker":         p.symbol,
            "qty":            float(p.qty),
            "side":           p.side.value,
            "avg_entry":      float(p.avg_entry_price),
            "current_price":  float(p.current_price),
            "unrealized_pnl": float(p.unrealized_pl),
            "market_value":   float(p.market_value),
        }
        for p in positions
    ]


# ── Order submission ──────────────────────────────────────────────────── #

def _order_side(book: str, action: str):
    """Determine Alpaca OrderSide from our book/action convention."""
    from alpaca.trading.enums import OrderSide
    # Opening a long  → BUY
    # Opening a short → SELL (short sell — Alpaca handles this automatically)
    # Closing a long  → SELL
    # Closing a short → BUY  (cover)
    if action == "open":
        return OrderSide.BUY if book == "long" else OrderSide.SELL
    else:  # close / cover
        return OrderSide.BUY if book == "short" else OrderSide.SELL


def submit_market_order(
    ticker: str,
    shares: int,
    book: str,
    action: str,
    dry_run: bool = False,
) -> dict:
    """
    Submit a market order.  Returns a result dict with order_id, status, etc.
    dry_run=True logs the order without sending it to Alpaca.
    """
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    side = _order_side(book, action)
    shares = abs(int(shares))  # Alpaca qty must be positive integer

    if shares == 0:
        return {"status": "skipped", "reason": "shares=0"}

    log_prefix = f"[{'DRY RUN' if dry_run else 'LIVE'}] {ticker} {side.value} {shares}sh ({book}/{action})"
    logger.info(log_prefix)

    if dry_run:
        return {
            "status":   "dry_run",
            "ticker":   ticker,
            "side":     side.value,
            "shares":   shares,
            "order_id": None,
        }

    try:
        client = _get_client()
        req = MarketOrderRequest(
            symbol        = ticker,
            qty           = shares,
            side          = side,
            time_in_force = TimeInForce.DAY,
        )
        order = client.submit_order(req)
        logger.info(f"Order submitted: {order.id} | {ticker} {side.value} {shares}sh")
        return {
            "status":   "submitted",
            "order_id": str(order.id),
            "ticker":   ticker,
            "side":     side.value,
            "shares":   shares,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"Order submission failed for {ticker}: {e}")
        return {"status": "error", "reason": str(e), "ticker": ticker}


# ── Fill polling ──────────────────────────────────────────────────────── #

def poll_until_filled(order_id: str) -> dict:
    """
    Poll Alpaca until the order is filled or timeout is reached.
    Returns a result dict with fill_price, filled_qty, fill_time.
    """
    from alpaca.trading.enums import OrderStatus

    client  = _get_client()
    start   = time.monotonic()
    attempt = 0

    while time.monotonic() - start < FILL_TIMEOUT:
        attempt += 1
        try:
            order = client.get_order_by_id(order_id)
            status = order.status

            if status == OrderStatus.FILLED:
                fill_price = float(order.filled_avg_price or 0)
                filled_qty = float(order.filled_qty or 0)
                logger.info(
                    f"Order {order_id} FILLED: {filled_qty}sh @ ${fill_price:.4f} "
                    f"(after {attempt} polls, {time.monotonic()-start:.1f}s)"
                )
                return {
                    "status":      "filled",
                    "fill_price":  fill_price,
                    "filled_qty":  filled_qty,
                    "fill_time":   datetime.now(timezone.utc).isoformat(),
                    "order_id":    order_id,
                }

            if status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
                reason = getattr(order, "failed_at", None) or status.value
                logger.warning(f"Order {order_id} terminal status: {status.value}")
                return {"status": status.value, "order_id": order_id, "reason": reason}

            logger.debug(f"Order {order_id}: {status.value} (poll #{attempt})")

        except Exception as e:
            logger.warning(f"Poll error for {order_id}: {e}")

        time.sleep(POLL_INTERVAL)

    logger.error(f"Order {order_id} fill timeout after {FILL_TIMEOUT}s")
    return {"status": "timeout", "order_id": order_id}


# ── Market clock ─────────────────────────────────────────────────────── #

def get_market_clock() -> dict:
    """Return market open/closed status and next session times from Alpaca."""
    try:
        client = _get_client()
        clock = client.get_clock()
        return {
            "ok":         True,
            "is_open":    clock.is_open,
            "next_open":  clock.next_open.isoformat() if clock.next_open else None,
            "next_close": clock.next_close.isoformat() if clock.next_close else None,
            "timestamp":  clock.timestamp.isoformat() if clock.timestamp else None,
        }
    except Exception as e:
        logger.warning(f"Could not fetch market clock: {e}")
        return {"ok": False, "is_open": None, "error": str(e)}


# ── Connectivity test ─────────────────────────────────────────────────── #

def test_connection() -> dict:
    """Verify credentials and return account summary. Safe to call at startup."""
    try:
        acct = get_account()
        logger.info(
            f"Alpaca connected | equity=${acct['equity']:,.0f} "
            f"cash=${acct['cash']:,.0f} "
            f"shorting={'YES' if acct['shorting_enabled'] else 'NO'}"
        )
        return {"ok": True, **acct}
    except Exception as e:
        logger.error(f"Alpaca connection failed: {e}")
        return {"ok": False, "error": str(e)}
