"""
Order Manager — orchestrates the full execution cycle:

  1. Read approved trades from position_approvals
  2. Run pre-trade risk veto (8 checks, no override)
  3. Submit market order to Alpaca
  4. Poll for fill
  5. Update portfolio_positions with actual fill price
  6. Compute and log slippage
  7. Write execution record to position_approvals (status + notes)
"""

import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_EXEC = _CFG["execution"]
_MAX_SLIPPAGE_BPS = _EXEC["max_slippage_warn_bps"]
NAV = _CFG["portfolio"]["nav"]


# ── DB helpers ────────────────────────────────────────────────────────── #

def _get_pending_approvals(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT id, ticker, book, action, shares, estimated_price, cost_bps, notes
           FROM position_approvals WHERE status = 'approved'
           ORDER BY created_at"""
    ).fetchall()
    cols = ["id", "ticker", "book", "action", "shares", "estimated_price", "cost_bps", "notes"]
    return [dict(zip(cols, r)) for r in rows]


def _set_status(conn: sqlite3.Connection, approval_id: int, status: str, notes: str = ""):
    conn.execute(
        "UPDATE position_approvals SET status=?, decided_at=?, notes=? WHERE id=?",
        (status, datetime.now(timezone.utc).isoformat(), notes, approval_id),
    )
    conn.commit()


def _upsert_position(conn: sqlite3.Connection, approval: dict, fill_price: float, filled_qty: float):
    """Insert or update portfolio_positions with actual fill data."""
    ticker     = approval["ticker"]
    book       = approval["book"]
    action     = approval["action"]
    signed_qty = filled_qty if book == "long" else -filled_qty  # negative = short

    today = date.today().isoformat()

    # Get sector from universe table
    row = conn.execute("SELECT sector FROM universe WHERE ticker=?", (ticker,)).fetchone()
    sector = row[0] if row else ""

    if action == "open":
        conn.execute(
            """INSERT INTO portfolio_positions
               (ticker, book, shares, target_weight, entry_price, entry_date,
                current_price, unrealized_pnl, sector, last_updated)
               VALUES (?,?,?,?,?,?,?,0,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 shares        = shares + excluded.shares,
                 entry_price   = excluded.entry_price,
                 entry_date    = excluded.entry_date,
                 current_price = excluded.current_price,
                 last_updated  = excluded.last_updated""",
            (ticker, book, signed_qty,
             abs(signed_qty) * fill_price / NAV,
             fill_price, today, fill_price, sector, today),
        )
    else:  # close
        # Zero out or remove the position
        existing = conn.execute(
            "SELECT shares FROM portfolio_positions WHERE ticker=?", (ticker,)
        ).fetchone()
        if existing:
            new_shares = existing[0] - signed_qty
            if abs(new_shares) < 0.01:
                conn.execute("DELETE FROM portfolio_positions WHERE ticker=?", (ticker,))
            else:
                conn.execute(
                    "UPDATE portfolio_positions SET shares=?, last_updated=? WHERE ticker=?",
                    (new_shares, today, ticker),
                )

    conn.commit()


def _compute_slippage_bps(estimated: float, actual: float) -> float:
    """Slippage in basis points: positive = paid more than expected."""
    if not estimated or not actual:
        return 0.0
    return ((actual - estimated) / estimated) * 10_000


# ── Main execution loop ───────────────────────────────────────────────── #

def run_execution(
    conn: sqlite3.Connection,
    dry_run: bool = False,
) -> dict:
    """
    Process all 'approved' position_approvals through the full execution cycle.
    Returns a summary dict.
    """
    from execution.alpaca_client import test_connection, submit_market_order, poll_until_filled
    from risk.pre_trade import check_pre_trade

    # ── Verify Alpaca connectivity ────────────────────────────────────── #
    acct = test_connection()
    if not acct["ok"]:
        raise ConnectionError(f"Cannot connect to Alpaca: {acct.get('error')}")

    logger.info(
        f"Alpaca account OK | equity=${acct['equity']:,.0f} "
        f"cash=${acct['cash']:,.0f}"
    )

    pending = _get_pending_approvals(conn)
    if not pending:
        logger.info("No approved trades pending execution.")
        return {"processed": 0, "filled": 0, "vetoed": 0, "errors": 0}

    logger.info(f"Processing {len(pending)} approved trade(s)...")

    results = {"processed": 0, "filled": 0, "vetoed": 0, "errors": 0, "details": []}

    for apv in pending:
        ticker    = apv["ticker"]
        book      = apv["book"]
        action    = apv["action"]
        shares    = apv["shares"]
        est_price = apv["estimated_price"] or 0.0
        apv_id    = apv["id"]
        results["processed"] += 1

        logger.info(f"--- {ticker} | {book} {action} | {shares}sh @ ~${est_price:.2f} ---")

        # ── Pre-trade risk veto ───────────────────────────────────────── #
        row = conn.execute("SELECT sector FROM universe WHERE ticker=?", (ticker,)).fetchone()
        sector = row[0] if row else ""

        try:
            # check_pre_trade returns (approved: bool, reason: str, approved_shares: float)
            approved, reason, approved_shares = check_pre_trade(
                ticker          = ticker,
                action          = action,
                shares          = shares,
                estimated_price = est_price,
                book            = book,
                sector          = sector,
                conn            = conn,
            )
        except Exception as e:
            logger.error(f"Pre-trade check error for {ticker}: {e}")
            _set_status(conn, apv_id, "error", f"pre_trade exception: {e}")
            results["errors"] += 1
            results["details"].append({"ticker": ticker, "status": "error", "reason": str(e)})
            continue

        if not approved:
            logger.warning(f"VETOED {ticker}: {reason}")
            _set_status(conn, apv_id, "vetoed", reason)
            results["vetoed"] += 1
            results["details"].append({"ticker": ticker, "status": "vetoed", "reason": reason})
            continue

        # Use the potentially reduced share count (e.g. earnings blackout halves it)
        shares = approved_shares

        # ── Submit order ──────────────────────────────────────────────── #
        _set_status(conn, apv_id, "submitted")
        order_result = submit_market_order(
            ticker  = ticker,
            shares  = shares,
            book    = book,
            action  = action,
            dry_run = dry_run,
        )

        if order_result["status"] == "dry_run":
            logger.info(f"DRY RUN — would have submitted: {ticker} {book} {action} {shares}sh")
            _set_status(conn, apv_id, "approved", "dry_run — not submitted")
            results["details"].append({"ticker": ticker, "status": "dry_run"})
            continue

        if order_result["status"] == "error":
            reason = order_result.get("reason", "submission error")
            logger.error(f"Submission failed for {ticker}: {reason}")
            _set_status(conn, apv_id, "error", reason)
            results["errors"] += 1
            results["details"].append({"ticker": ticker, "status": "error", "reason": reason})
            continue

        # ── Poll for fill ─────────────────────────────────────────────── #
        order_id    = order_result["order_id"]
        fill_result = poll_until_filled(order_id)
        status      = fill_result["status"]

        if status == "filled":
            fill_price  = fill_result["fill_price"]
            filled_qty  = fill_result["filled_qty"]
            slip_bps    = _compute_slippage_bps(est_price, fill_price)

            slip_note = ""
            if abs(slip_bps) > _MAX_SLIPPAGE_BPS:
                logger.warning(
                    f"HIGH SLIPPAGE {ticker}: est=${est_price:.4f} fill=${fill_price:.4f} "
                    f"slippage={slip_bps:+.1f}bps"
                )
                slip_note = f" | HIGH SLIPPAGE {slip_bps:+.1f}bps"
            else:
                logger.info(f"Fill OK {ticker}: ${fill_price:.4f} slippage={slip_bps:+.1f}bps")

            notes = f"order_id={order_id} fill=${fill_price:.4f} slippage={slip_bps:+.1f}bps{slip_note}"
            _set_status(conn, apv_id, "filled", notes)
            _upsert_position(conn, apv, fill_price, filled_qty)

            results["filled"] += 1
            results["details"].append({
                "ticker":       ticker,
                "status":       "filled",
                "fill_price":   fill_price,
                "filled_qty":   filled_qty,
                "slippage_bps": round(slip_bps, 1),
                "order_id":     order_id,
            })

        else:
            reason = fill_result.get("reason", status)
            logger.error(f"Fill failed {ticker}: {status} — {reason}")
            _set_status(conn, apv_id, status, f"order_id={order_id} | {reason}")
            results["errors"] += 1
            results["details"].append({"ticker": ticker, "status": status, "reason": reason})

    return results
