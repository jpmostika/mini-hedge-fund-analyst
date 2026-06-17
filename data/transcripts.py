"""
Earnings call transcripts via Financial Modeling Prep API.
Only fetches for long/short candidates (not entire universe).
Skips gracefully if FMP_API_KEY is not configured.
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml

from data.db import get_connection, init_db
from data.providers import get_providers

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

TABLE = _CFG["transcripts"]["table"]
FMP_BASE = _CFG["transcripts"]["fmp_base"]


def _fetch_transcript_fmp(ticker: str, fmp_key: str) -> Optional[list[dict]]:
    url = f"{FMP_BASE}/{ticker}"
    try:
        resp = requests.get(url, params={"apikey": fmp_key}, timeout=30)
        if resp.status_code == 401:
            logger.warning("FMP API key invalid or expired")
            return None
        if resp.status_code != 200:
            logger.debug(f"FMP transcript failed for {ticker}: {resp.status_code}")
            return None
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception as e:
        logger.debug(f"FMP transcript exception for {ticker}: {e}")
        return None


def _upsert_transcript(conn: sqlite3.Connection, ticker: str, quarter: str, year: int, content: str):
    now = datetime.utcnow().isoformat()
    conn.execute(
        f"""INSERT OR REPLACE INTO {TABLE} (ticker, quarter, year, content, fetched_at)
            VALUES (?, ?, ?, ?, ?)""",
        (ticker, quarter, year, content, now),
    )
    conn.commit()


def refresh_transcripts(candidates: list[str]) -> dict:
    """
    candidates: list of tickers that are long/short candidates.
                Pass an empty list to skip entirely.
    """
    init_db()
    providers = get_providers()

    if not providers.use_fmp:
        logger.info("FMP_API_KEY not configured — skipping earnings transcripts")
        return {"skipped": True, "reason": "no FMP key"}

    if not candidates:
        logger.info("No transcript candidates provided — skipping")
        return {"transcripts_fetched": 0}

    conn = get_connection()
    fetched = 0
    errors = 0

    for ticker in candidates:
        transcripts = _fetch_transcript_fmp(ticker, providers.fmp_key)
        if not transcripts:
            errors += 1
            continue

        for item in transcripts[:1]:  # store only the latest transcript
            content = item.get("content", "")
            quarter = item.get("quarter", "")
            year = item.get("year", 0)
            if not content:
                continue

            # Skip if already stored
            cur = conn.execute(
                f"SELECT 1 FROM {TABLE} WHERE ticker=? AND quarter=? AND year=?",
                (ticker, str(quarter), int(year) if year else 0),
            )
            if cur.fetchone():
                continue

            _upsert_transcript(conn, ticker, str(quarter), int(year) if year else 0, content)
            fetched += 1
            logger.info(f"Transcript stored: {ticker} Q{quarter} {year}")

    conn.close()
    logger.info(f"Transcript refresh complete — {fetched} stored, {errors} errors")
    return {"transcripts_fetched": fetched, "errors": errors}
