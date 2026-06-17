"""
Analysis results cache — SQLite-backed, TTL-based eviction.
Key: (analyzer, ticker, artifact_id)
Re-running the same artifact is a free cache hit; no Claude call made.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from data.db import get_connection, init_db

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

TTL_DAYS: int = _CFG["analysis"]["cache_ttl_days"]


def _now() -> str:
    return datetime.utcnow().isoformat()


def _expiry() -> str:
    return (datetime.utcnow() + timedelta(days=TTL_DAYS)).isoformat()


def cache_get(analyzer: str, ticker: str, artifact_id: str) -> Optional[dict]:
    """
    Return cached result if present and not expired, else None.
    """
    init_db()
    conn = get_connection()
    try:
        now = _now()
        row = conn.execute(
            """SELECT result_json FROM analysis_results
               WHERE analyzer=? AND ticker=? AND artifact_id=? AND expires_at > ?""",
            (analyzer, ticker, artifact_id, now),
        ).fetchone()
        if row:
            logger.debug(f"[cache HIT] {analyzer}/{ticker}/{artifact_id}")
            return json.loads(row[0])
        logger.debug(f"[cache MISS] {analyzer}/{ticker}/{artifact_id}")
        return None
    finally:
        conn.close()


def cache_set(analyzer: str, ticker: str, artifact_id: str, result: dict) -> None:
    """Store or replace a result. Resets TTL on re-run."""
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO analysis_results
               (analyzer, ticker, artifact_id, result_json, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (analyzer, ticker, artifact_id, json.dumps(result), _now(), _expiry()),
        )
        conn.commit()
    finally:
        conn.close()


def cache_evict_expired() -> int:
    """Delete expired entries. Call periodically (e.g., at start of run)."""
    init_db()
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM analysis_results WHERE expires_at <= ?", (_now(),)
        )
        conn.commit()
        deleted = cur.rowcount
        if deleted:
            logger.info(f"Cache: evicted {deleted} expired analysis result(s)")
        return deleted
    finally:
        conn.close()


def cache_stats() -> dict:
    """Return counts of cached results by analyzer."""
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT analyzer, COUNT(*) FROM analysis_results GROUP BY analyzer"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()
