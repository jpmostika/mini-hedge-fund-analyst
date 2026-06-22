"""
Rebalance Schedule — advisory event warnings.
Returns warnings; does NOT block trading.

Events checked:
  - Earnings in 2 days (per current positions + candidates)
  - FOMC meeting within 5 days (hardcoded 2026 dates from config)
  - Monthly options expiration within 3 days (third Friday of month)
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import sqlite3
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_FOMC_DATES = [date.fromisoformat(d) for d in _CFG["portfolio"]["fomc_dates_2026"]]


def _third_friday(year: int, month: int) -> date:
    """Return the third Friday of the given month."""
    d = date(year, month, 1)
    fridays = []
    while d.month == month:
        if d.weekday() == 4:  # Friday
            fridays.append(d)
        d += timedelta(days=1)
    return fridays[2] if len(fridays) >= 3 else fridays[-1]


def _options_expiry_dates() -> list[date]:
    """Third Fridays for current + next 2 months."""
    today = date.today()
    months = [(today.year, today.month)]
    for _ in range(2):
        y, m = months[-1]
        m += 1
        if m > 12:
            m, y = 1, y + 1
        months.append((y, m))
    return [_third_friday(y, m) for y, m in months]


def check_schedule(
    tickers: list[str],
    conn: Optional[sqlite3.Connection] = None,
) -> list[str]:
    """
    Returns list of advisory warning strings.
    Empty list = no calendar concerns.
    """
    warnings = []
    today = date.today()

    # 1. Earnings in 2 days
    if conn and tickers:
        cutoff_earn = (today + timedelta(days=2)).isoformat()
        placeholders = ",".join("?" * len(tickers))
        rows = conn.execute(
            f"""SELECT ticker, earnings_date FROM earnings_calendar
                WHERE ticker IN ({placeholders})
                  AND earnings_date BETWEEN date('now') AND ?
                ORDER BY earnings_date""",
            tickers + [cutoff_earn],
        ).fetchall()
        for ticker, earn_date in rows:
            warnings.append(
                f"EARNINGS: {ticker} reports on {earn_date} "
                f"({(date.fromisoformat(earn_date) - today).days} days away)"
            )

    # 2. FOMC meeting within 5 days
    for fomc_date in _FOMC_DATES:
        days_away = (fomc_date - today).days
        if 0 <= days_away <= 5:
            warnings.append(
                f"FOMC: Fed meeting ends {fomc_date} ({days_away} days away) — "
                f"consider reducing gross exposure"
            )

    # 3. Monthly options expiration within 3 days
    for opex in _options_expiry_dates():
        days_away = (opex - today).days
        if 0 <= days_away <= 3:
            warnings.append(
                f"OPEX: Options expiration {opex} ({days_away} days away) — "
                f"expect elevated volatility in high-short-interest names"
            )

    return warnings
