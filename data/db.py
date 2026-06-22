"""
Shared SQLite database connection and schema initialization.
"""

import sqlite3
import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

DB_PATH = Path(__file__).parent.parent / _CFG["database"]["path"]


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_connection()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS universe (
            ticker          TEXT PRIMARY KEY,
            company_name    TEXT,
            sector          TEXT,
            sub_industry    TEXT,
            type            TEXT DEFAULT 'equity',
            last_updated    TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_prices (
            ticker  TEXT,
            date    TEXT,
            open    REAL,
            high    REAL,
            low     REAL,
            close   REAL,
            volume  REAL,
            PRIMARY KEY (ticker, date)
        );

        CREATE TABLE IF NOT EXISTS fundamentals (
            ticker              TEXT,
            period_end          TEXT,
            period_type         TEXT,
            source              TEXT,
            roe                 REAL,
            roa                 REAL,
            gross_margin        REAL,
            operating_margin    REAL,
            net_margin          REAL,
            revenue_growth_yoy  REAL,
            revenue_growth_qoq  REAL,
            earnings_growth_yoy REAL,
            earnings_growth_qoq REAL,
            debt_to_equity      REAL,
            fcf_yield           REAL,
            current_ratio       REAL,
            ar_to_revenue       REAL,
            cfo_to_ni           REAL,
            accruals_ratio      REAL,
            retained_earnings   REAL,
            working_capital     REAL,
            total_liabilities   REAL,
            ebit                REAL,
            rd_expense          REAL,
            shares_outstanding  REAL,
            dividends_paid      REAL,
            buybacks            REAL,
            asset_turnover      REAL,
            last_updated        TEXT,
            PRIMARY KEY (ticker, period_end, period_type)
        );

        CREATE TABLE IF NOT EXISTS insider_transactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT,
            insider_name    TEXT,
            insider_title   TEXT,
            transaction_code TEXT,
            shares          REAL,
            price           REAL,
            date            TEXT,
            ownership_type  TEXT,
            is_open_market  INTEGER DEFAULT 0,
            is_ceo_cfo      INTEGER DEFAULT 0,
            filing_url      TEXT,
            UNIQUE(ticker, insider_name, date, shares, transaction_code)
        );

        CREATE TABLE IF NOT EXISTS filings_cache (
            ticker      TEXT,
            form_type   TEXT,
            filing_date TEXT,
            accession   TEXT PRIMARY KEY,
            content     TEXT,
            cached_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS institutional_holdings (
            fund_name       TEXT,
            ticker          TEXT,
            shares_held     REAL,
            market_value    REAL,
            report_date     TEXT,
            shares_prev     REAL,
            net_change      REAL,
            last_updated    TEXT,
            PRIMARY KEY (fund_name, ticker, report_date)
        );

        CREATE TABLE IF NOT EXISTS short_interest (
            ticker                  TEXT,
            date                    TEXT,
            shares_short            REAL,
            short_ratio             REAL,
            short_percent_of_float  REAL,
            PRIMARY KEY (ticker, date)
        );

        CREATE TABLE IF NOT EXISTS analyst_estimates (
            ticker          TEXT,
            date            TEXT,
            eps_forward     REAL,
            price_target    REAL,
            num_analysts    INTEGER,
            PRIMARY KEY (ticker, date)
        );

        CREATE TABLE IF NOT EXISTS earnings_calendar (
            ticker          TEXT,
            earnings_date   TEXT,
            eps_estimate    REAL,
            fetched_date    TEXT,
            PRIMARY KEY (ticker, earnings_date)
        );

        CREATE TABLE IF NOT EXISTS earnings_transcripts (
            ticker      TEXT,
            quarter     TEXT,
            year        INTEGER,
            content     TEXT,
            fetched_at  TEXT,
            PRIMARY KEY (ticker, quarter, year)
        );

        CREATE TABLE IF NOT EXISTS market_metrics (
            ticker                  TEXT PRIMARY KEY,
            date                    TEXT,
            market_cap              REAL,
            price_to_book           REAL,
            enterprise_value        REAL,
            enterprise_to_ebitda    REAL,
            total_revenue           REAL,
            total_assets            REAL,
            total_cash              REAL,
            forward_pe              REAL,
            trailing_pe             REAL,
            price_to_sales          REAL
        );

        CREATE TABLE IF NOT EXISTS factor_scores (
            ticker          TEXT,
            date            TEXT,
            sector          TEXT,
            momentum        REAL,
            value           REAL,
            quality         REAL,
            growth          REAL,
            revisions       REAL,
            short_interest  REAL,
            insider         REAL,
            institutional   REAL,
            composite       REAL,
            signal          TEXT,
            PRIMARY KEY (ticker, date)
        );

        CREATE TABLE IF NOT EXISTS analysis_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            analyzer    TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            UNIQUE(analyzer, ticker, artifact_id)
        );

        CREATE TABLE IF NOT EXISTS portfolio_positions (
            ticker                  TEXT PRIMARY KEY,
            book                    TEXT NOT NULL,
            shares                  REAL NOT NULL,
            target_weight           REAL,
            entry_price             REAL,
            entry_date              TEXT,
            current_price           REAL,
            unrealized_pnl          REAL,
            sector                  TEXT,
            factor_scores_json      TEXT,
            last_updated            TEXT
        );

        CREATE TABLE IF NOT EXISTS portfolio_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            book        TEXT,
            action      TEXT NOT NULL,
            shares      REAL NOT NULL,
            price       REAL,
            cost_bps    REAL,
            timestamp   TEXT NOT NULL,
            reason      TEXT,
            run_id      TEXT
        );

        CREATE TABLE IF NOT EXISTS position_approvals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            book            TEXT,
            action          TEXT NOT NULL,
            shares          REAL NOT NULL,
            estimated_price REAL,
            cost_bps        REAL,
            status          TEXT DEFAULT 'pending',
            created_at      TEXT NOT NULL,
            decided_at      TEXT,
            notes           TEXT
        );
    """)

    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {DB_PATH}")
