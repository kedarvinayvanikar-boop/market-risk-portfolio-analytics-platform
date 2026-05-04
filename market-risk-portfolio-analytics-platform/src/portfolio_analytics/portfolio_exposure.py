"""
portfolio_exposure.py
=====================
Step 12 — Compute portfolio contribution, sector exposure, and alert
exposure, then create SQL views for Power BI consumption.

What this script does
---------------------
1. Creates four reusable SQL views that Power BI will connect to:
      v_stock_contribution   — daily wi × Ri,t per stock
      v_sector_contribution  — daily sector-level contribution rollup
      v_sector_weight        — static sector weight breakdown
      v_alert_exposure       — portfolio weight in currently alerted names

2. Computes a summary exposure snapshot for the most recent date:
      - Top 5 contributors (most positive impact)
      - Top 5 detractors (most negative impact)
      - Sector breakdown: weight and cumulative return contribution
      - Alert exposure: % of portfolio weight currently flagged

3. Writes a contribution_log table to SQLite for historical trend analysis
   in Power BI (sector contribution per day as rows, not a view).

Contribution formula
--------------------
    Contribution_{i,t} = w_i × R_{i,t}

    Where:  w_i   = static portfolio weight of stock i
            R_{i,t} = simple daily return of stock i on date t

Sector contribution = sum of individual contributions in that sector.
Portfolio return  = sum of all individual contributions (= Step 11).

Usage
-----
    python portfolio_exposure.py
    python portfolio_exposure.py --db my.db
    python portfolio_exposure.py --snapshot-date 2024-12-31
"""

import argparse
import logging
import sqlite3
import sys

import pandas as pd

DEFAULT_DB = "stocks.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────

def get_connection(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode  = WAL")
    return con


# ─────────────────────────────────────────────────────────────────
# SQL VIEWS
# Views are stored in SQLite and can be queried directly by Power BI.
# They recompute on the fly — no storage cost.
# ─────────────────────────────────────────────────────────────────

VIEWS = {

"v_stock_contribution": """
-- Daily contribution of each stock to portfolio return
-- contribution = weight × daily_return
CREATE VIEW IF NOT EXISTS v_stock_contribution AS
SELECT
    r.date,
    s.ticker,
    s.company_name,
    sec.sector_name,
    ph.weight,
    r.daily_return,
    ph.weight * r.daily_return              AS contribution,
    ph.weight * r.daily_return * 100        AS contribution_pct
FROM  fact_return       r
JOIN  portfolio_holding ph  ON ph.stock_id  = r.stock_id
JOIN  dim_stock         s   ON s.stock_id   = r.stock_id
JOIN  dim_sector        sec ON sec.sector_id = s.sector_id
""",

"v_sector_contribution": """
-- Daily sector-level rollup of contribution and weight
CREATE VIEW IF NOT EXISTS v_sector_contribution AS
SELECT
    date,
    sector_name,
    ROUND(SUM(weight), 4)                   AS sector_weight,
    ROUND(SUM(contribution), 6)             AS sector_contribution,
    ROUND(SUM(contribution) * 100, 4)       AS sector_contribution_pct,
    COUNT(DISTINCT ticker)                  AS n_stocks
FROM  v_stock_contribution
GROUP BY date, sector_name
""",

"v_sector_weight": """
-- Static sector weight breakdown (no date dimension)
CREATE VIEW IF NOT EXISTS v_sector_weight AS
SELECT
    sec.sector_name,
    COUNT(s.stock_id)                       AS n_stocks,
    ROUND(SUM(ph.weight), 4)                AS total_weight,
    ROUND(SUM(ph.weight) * 100, 2)          AS weight_pct,
    GROUP_CONCAT(s.ticker, ', ')            AS tickers
FROM  portfolio_holding ph
JOIN  dim_stock         s   ON s.stock_id   = ph.stock_id
JOIN  dim_sector        sec ON sec.sector_id = s.sector_id
GROUP BY sec.sector_name
ORDER BY total_weight DESC
""",

"v_alert_exposure": """
-- Portfolio weight currently exposed to alerted stocks
-- Joins the most recent alert per stock to the portfolio holding
CREATE VIEW IF NOT EXISTS v_alert_exposure AS
SELECT
    a.date                                  AS alert_date,
    s.ticker,
    sec.sector_name,
    ph.weight,
    ph.weight * 100                         AS weight_pct,
    a.alert_type,
    a.severity,
    ROUND(a.alert_value, 4)                 AS alert_value
FROM  risk_alert        a
JOIN  dim_stock         s   ON s.stock_id   = a.stock_id
JOIN  dim_sector        sec ON sec.sector_id = s.sector_id
JOIN  portfolio_holding ph  ON ph.stock_id  = a.stock_id
WHERE a.date = (
    SELECT MAX(date) FROM risk_alert WHERE stock_id = a.stock_id
)
AND   a.alert_type != 'COMPOSITE'
ORDER BY ph.weight DESC, a.severity
""",

"v_rolling_sector_contribution": """
-- 20-day rolling cumulative sector contribution for trend analysis
CREATE VIEW IF NOT EXISTS v_rolling_sector_contribution AS
SELECT
    sc.date,
    sc.sector_name,
    sc.sector_contribution,
    SUM(sc.sector_contribution) OVER (
        PARTITION BY sc.sector_name
        ORDER BY sc.date
        ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
    ) AS rolling_20d_contribution
FROM v_sector_contribution sc
""",

}


def create_views(con: sqlite3.Connection) -> None:
    """Drop and re-create all views so they stay in sync with schema changes."""
    for name, ddl in VIEWS.items():
        con.execute(f"DROP VIEW IF EXISTS {name}")
        con.execute(ddl)
    con.commit()
    log.info("Created %d SQL views: %s", len(VIEWS), ", ".join(VIEWS))


# ─────────────────────────────────────────────────────────────────
# CONTRIBUTION LOG TABLE
# Stores daily sector contributions as rows (not a view) so Power BI
# can build time-series charts without recomputing on every render.
# ─────────────────────────────────────────────────────────────────

def ensure_contribution_log(con: sqlite3.Connection) -> None:
    """
    Ensure contribution_log has the Step 12 sector-level schema.

    Earlier setup code may have created contribution_log as a stock-level table
    with columns like contribution_id and stock_id. That table shape is not
    compatible with Step 12, so we safely drop and recreate it when the expected
    sector_name column is missing. This only affects the materialized reporting
    log; the source tables/views remain untouched.
    """
    existing_cols = [
        row[1]
        for row in con.execute("PRAGMA table_info(contribution_log)").fetchall()
    ]

    expected_cols = {
        "date",
        "sector_name",
        "sector_weight",
        "sector_contribution",
        "sector_contribution_pct",
        "n_stocks",
    }

    if existing_cols and not expected_cols.issubset(set(existing_cols)):
        log.warning(
            "Existing contribution_log schema is incompatible with Step 12; "
            "dropping and recreating it."
        )
        con.execute("DROP TABLE IF EXISTS contribution_log")

    con.execute("""
        CREATE TABLE IF NOT EXISTS contribution_log (
            date                      TEXT NOT NULL,
            sector_name               TEXT NOT NULL,
            sector_weight             REAL,
            sector_contribution       REAL,
            sector_contribution_pct   REAL,
            n_stocks                  INTEGER,
            PRIMARY KEY (date, sector_name)
        )
    """)
    con.commit()


def populate_contribution_log(con: sqlite3.Connection) -> int:
    """
    Materialise v_sector_contribution into contribution_log.
    DELETE-then-INSERT pattern makes it idempotent.
    """
    # Find the date range available
    date_range = con.execute("""
        SELECT MIN(date), MAX(date) FROM v_stock_contribution
    """).fetchone()

    if not date_range[0]:
        log.warning("v_stock_contribution returned no rows — contribution_log not populated.")
        return 0

    min_date, max_date = date_range

    # Clear and repopulate
    con.execute("""
        DELETE FROM contribution_log
        WHERE date BETWEEN ? AND ?
    """, (min_date, max_date))

    con.execute("""
        INSERT INTO contribution_log
            (date, sector_name, sector_weight,
             sector_contribution, sector_contribution_pct, n_stocks)
        SELECT
            date, sector_name, sector_weight,
            sector_contribution, sector_contribution_pct, n_stocks
        FROM v_sector_contribution
    """)
    con.commit()

    n = con.execute("SELECT COUNT(*) FROM contribution_log").fetchone()[0]
    log.info("Populated contribution_log: %d rows.", n)
    return n


# ─────────────────────────────────────────────────────────────────
# SNAPSHOT ANALYTICS
# ─────────────────────────────────────────────────────────────────

def get_snapshot_date(con: sqlite3.Connection, requested: str | None) -> str:
    """Return the snapshot date (most recent available if not specified)."""
    if requested:
        return requested
    date = con.execute(
        "SELECT MAX(date) FROM v_stock_contribution"
    ).fetchone()[0]
    if not date:
        raise RuntimeError("v_stock_contribution is empty.")
    return date


def print_stock_snapshot(con: sqlite3.Connection, snapshot_date: str) -> None:
    """Top contributors and detractors on a given date."""
    log.info("")
    log.info("─" * 72)
    log.info("STOCK CONTRIBUTION SNAPSHOT — %s", snapshot_date)
    log.info("─" * 72)

    rows = con.execute("""
        SELECT  ticker, sector_name,
                ROUND(weight * 100, 1)          AS weight_pct,
                ROUND(daily_return * 100, 3)    AS ret_pct,
                ROUND(contribution * 100, 4)    AS contrib_pct
        FROM    v_stock_contribution
        WHERE   date = ?
        ORDER   BY contribution DESC
    """, (snapshot_date,)).fetchall()

    if not rows:
        log.warning("No data for %s.", snapshot_date)
        return

    log.info("%-6s  %-16s  %8s  %10s  %12s",
             "Ticker","Sector","Wt%","Return%","Contrib%")
    log.info("%-6s  %-16s  %8s  %10s  %12s",
             "──────","──────────────","──────","────────","──────────")

    portfolio_return = sum(r[4] for r in rows)

    for ticker, sector, wt, ret, contrib in rows:
        bar = "▓" * max(0, round(abs(contrib) * 200)) if contrib else ""
        direction = "+" if contrib >= 0 else ""
        log.info("%-6s  %-16s  %7.1f%%  %9.3f%%  %+11.4f%%  %s",
                 ticker, sector, wt, ret or 0, contrib or 0, bar)

    log.info("%-6s  %-16s  %8s  %10s  %+12.4f%%  ← portfolio total",
             "TOTAL","","100.0%","", portfolio_return)
    log.info("─" * 72)


def print_sector_snapshot(con: sqlite3.Connection, snapshot_date: str) -> None:
    """Sector breakdown on a given date."""
    rows = con.execute("""
        SELECT  sector_name,
                ROUND(sector_weight * 100, 1)       AS weight_pct,
                ROUND(sector_contribution_pct, 4)   AS contrib_pct,
                n_stocks
        FROM    v_sector_contribution
        WHERE   date = ?
        ORDER   BY sector_contribution DESC
    """, (snapshot_date,)).fetchall()

    log.info("")
    log.info("SECTOR CONTRIBUTION SNAPSHOT — %s", snapshot_date)
    log.info("─" * 55)
    log.info("%-16s  %8s  %12s  %8s",
             "Sector","Wt%","Contrib%","Stocks")
    log.info("%-16s  %8s  %12s  %8s",
             "──────────────","──────","──────────","──────")
    for sector, wt, contrib, n in rows:
        log.info("%-16s  %7.1f%%  %+11.4f%%  %8d",
                 sector, wt, contrib or 0, n)
    log.info("─" * 55)


def print_sector_weights(con: sqlite3.Connection) -> None:
    """Static sector weight allocation."""
    rows = con.execute("SELECT * FROM v_sector_weight").fetchall()

    log.info("")
    log.info("STATIC SECTOR WEIGHT ALLOCATION")
    log.info("─" * 72)
    log.info("%-16s  %8s  %8s  %8s  %s",
             "Sector","Stocks","Wt%","Wt","Tickers (first 4)")
    log.info("%-16s  %8s  %8s  %8s  %s",
             "──────────────","──────","──────","──────","──────────────────")
    for row in rows:
        sector, n, wt, wt_pct, tickers = row
        short_tickers = ", ".join(tickers.split(", ")[:4])
        if len(tickers.split(", ")) > 4:
            short_tickers += "…"
        log.info("%-16s  %8d  %7.1f%%  %8.3f  %s",
                 sector, n, wt_pct, wt, short_tickers)
    log.info("─" * 72)


def print_alert_exposure(con: sqlite3.Connection) -> None:
    """Portfolio weight currently exposed to alerted stocks."""
    rows = con.execute("""
        SELECT  ticker, sector_name, weight_pct,
                alert_type, severity, alert_value, alert_date
        FROM    v_alert_exposure
        ORDER   BY CASE severity WHEN 'HIGH' THEN 1 ELSE 2 END,
                   weight_pct DESC
    """).fetchall()

    if not rows:
        log.info("")
        log.info("ALERT EXPOSURE: No current alerts found.")
        return

    total_exposed_weight = sum(r[2] for r in rows) / 100  # deduplicate by ticker
    # Deduplicate: a stock can appear multiple times (multiple alert types)
    unique_tickers = {r[0]: r[2] for r in rows}
    exposed_pct = sum(unique_tickers.values())

    log.info("")
    log.info("CURRENT ALERT EXPOSURE")
    log.info("─" * 72)
    log.info("  Portfolio weight in alerted names: %.1f%%  (%d unique stocks)",
             exposed_pct, len(unique_tickers))
    log.info("")
    log.info("%-6s  %-14s  %8s  %-14s  %-8s  %10s  %12s",
             "Ticker","Sector","Wt%","Alert Type","Severity","Value","Alert Date")
    log.info("%-6s  %-14s  %8s  %-14s  %-8s  %10s  %12s",
             "──────","──────────────","──────","──────────────","────────","──────","──────────")
    for ticker, sector, wt, atype, sev, val, adate in rows:
        sev_flag = "⚠" if sev == "HIGH" else "·"
        log.info("%s %-5s  %-14s  %7.1f%%  %-14s  %-8s  %10.4f  %12s",
                 sev_flag, ticker, sector, wt, atype, sev, val or 0, adate)
    log.info("─" * 72)


def print_cumulative_sector_contribution(con: sqlite3.Connection) -> None:
    """Cumulative sector contributions over the full period — who drove returns?"""
    rows = con.execute("""
        SELECT  sector_name,
                ROUND(SUM(sector_contribution) * 100, 2) AS total_contrib_pct,
                ROUND(AVG(sector_contribution) * 100, 4) AS avg_daily_contrib_pct,
                COUNT(*) AS days
        FROM    contribution_log
        GROUP   BY sector_name
        ORDER   BY total_contrib_pct DESC
    """).fetchall()

    log.info("")
    log.info("CUMULATIVE SECTOR CONTRIBUTION — FULL PERIOD")
    log.info("─" * 60)
    log.info("%-16s  %14s  %16s  %6s",
             "Sector","Total Contrib%","Avg Daily Contrib","Days")
    log.info("%-16s  %14s  %16s  %6s",
             "──────────────","──────────────","────────────────","──────")
    for sector, total, avg_daily, days in rows:
        bar = "█" * max(0, round(abs(total) / 2))
        log.info("%-16s  %+13.2f%%  %+15.4f%%  %6d  %s",
                 sector, total or 0, avg_daily or 0, days, bar)
    log.info("─" * 60)


# ─────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────

def run(db_path: str, snapshot_date: str | None) -> None:
    log.info("=" * 60)
    log.info("STEP 12 — PORTFOLIO EXPOSURE & CONTRIBUTION")
    log.info("  DB : %s", db_path)
    log.info("=" * 60)

    con = get_connection(db_path)

    # 1. Create SQL views
    create_views(con)

    # 2. Create and populate contribution_log table
    ensure_contribution_log(con)
    populate_contribution_log(con)

    # 3. Get snapshot date
    snap = get_snapshot_date(con, snapshot_date)
    log.info("Snapshot date: %s", snap)

    # 4. Print all exposure reports
    print_sector_weights(con)
    print_stock_snapshot(con, snap)
    print_sector_snapshot(con, snap)
    print_alert_exposure(con)
    print_cumulative_sector_contribution(con)

    log.info("")
    log.info("STEP 12 COMPLETE.")
    log.info("SQL views available for Power BI:")
    for name in VIEWS:
        log.info("  • %s", name)
    con.close()


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Step 12 — Portfolio exposure and contribution analysis."
    )
    p.add_argument("--db",            default=DEFAULT_DB, help="SQLite DB path")
    p.add_argument("--snapshot-date", default=None,
                   help="Date for snapshot report YYYY-MM-DD (default: most recent)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.db, args.snapshot_date)
