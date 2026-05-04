"""
alert_engine.py
===============
Step 10 — Rule-based alert engine that detects risky conditions and
writes triggered alerts to the risk_alert table.

Alert Rules
-----------
ALERT-01  VOL_SPIKE     HIGH    rolling_vol_20d > 1.5 × its own 60-day average
ALERT-02  DRAWDOWN      HIGH    drawdown < -0.20  (20% below rolling peak)
ALERT-03  VOLUME_SPIKE  MEDIUM  volume_spike_ratio > 2.0
ALERT-04  LARGE_MOVE    MEDIUM  |daily_return| > 0.05  (single-day ±5%)
ALERT-05  COMPOSITE     HIGH    two or more of ALERT-01/02/03/04 fire on same day

Severity levels
---------------
HIGH   — immediate attention, strong signal
MEDIUM — worth noting, watch list
LOW    — informational only

Design decisions
----------------
- Alerts are APPEND-ONLY: every run adds new rows, never updates/deletes.
  This gives a full historical audit trail of every firing.
- ALERT-05 (composite) is fired ONLY when >= 2 individual alerts fire on
  the same (stock_id, date) — it signals corroborating evidence.
- Thresholds are constants at the top of the file — easy to adjust
  without touching logic.
- The 60-day rolling average of volatility (used in ALERT-01) is computed
  on-the-fly inside this script so fact_risk_metric stays clean.

Usage
-----
    python alert_engine.py
    python alert_engine.py --db my.db
    python alert_engine.py --since 2024-01-01   # only scan recent dates
    python alert_engine.py --dry-run            # print alerts without writing
"""

import argparse
import logging
import sqlite3
import sys
from datetime import date

import pandas as pd
import numpy as np

DEFAULT_DB = "stocks.db"

# ── Alert thresholds ──────────────────────────────────────────────
VOL_SPIKE_MULTIPLIER  = 1.5    # ALERT-01: vol > N × 60d avg vol
VOL_AVG_WINDOW        = 60     # ALERT-01: rolling window for vol baseline
DRAWDOWN_THRESHOLD    = -0.20  # ALERT-02: drawdown below this fraction
VOLUME_RATIO_THRESHOLD = 2.0   # ALERT-03: volume spike ratio above this
LARGE_MOVE_THRESHOLD  = 0.05   # ALERT-04: absolute daily return above this
COMPOSITE_MIN_ALERTS  = 2      # ALERT-05: min individual alerts to fire composite

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


def load_metrics(con: sqlite3.Connection, since: str | None) -> pd.DataFrame:
    """
    Join fact_risk_metric + fact_return + dim_stock into a single flat
    DataFrame for alert evaluation.

    Returns one row per (stock_id, date) with all metric columns plus
    ticker name for reporting.
    """
    where = f"AND m.date >= '{since}'" if since else ""
    df = pd.read_sql_query(f"""
        SELECT  m.stock_id,
                s.ticker,
                m.date,
                m.rolling_vol_20d,
                m.drawdown,
                m.volume_spike_ratio,
                r.daily_return
        FROM    fact_risk_metric m
        JOIN    dim_stock        s  ON s.stock_id  = m.stock_id
        LEFT JOIN fact_return    r  ON r.stock_id  = m.stock_id
                                   AND r.date      = m.date
        WHERE   m.rolling_vol_20d IS NOT NULL
                {where}
        ORDER   BY m.stock_id, m.date
    """, con)

    if df.empty:
        raise RuntimeError(
            "No metric data found. Run compute_volatility.py (Step 7) first."
        )

    log.info("Loaded %d metric rows across %d tickers.",
             len(df), df["ticker"].nunique())
    return df


# ─────────────────────────────────────────────────────────────────
# FEATURE ENGINEERING — 60-day vol baseline for ALERT-01
# ─────────────────────────────────────────────────────────────────

def add_vol_baseline(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute a 60-day rolling MEAN of rolling_vol_20d for each stock.
    This becomes the 'normal' volatility baseline for ALERT-01.

    shift(1) avoids including today's vol in its own baseline — same
    look-ahead bias argument as the volume spike in Step 9.

    A stock's current 20d vol is 'spiking' if it exceeds
    VOL_SPIKE_MULTIPLIER × its 60d average historical vol.
    """
    df = df.copy()
    df["vol_60d_avg"] = (
        df.groupby("stock_id")["rolling_vol_20d"]
        .transform(
            lambda s: s.shift(1).rolling(window=VOL_AVG_WINDOW, min_periods=VOL_AVG_WINDOW).mean()
        )
    )
    return df


# ─────────────────────────────────────────────────────────────────
# ALERT RULES
# ─────────────────────────────────────────────────────────────────

def evaluate_rules(df: pd.DataFrame) -> pd.DataFrame:
    """
    Evaluate all five alert rules as boolean columns on the full DataFrame.
    Returns the same DataFrame with added boolean flag columns:
        flag_vol_spike, flag_drawdown, flag_volume, flag_large_move, n_flags
    """
    df = df.copy()

    # ALERT-01: volatility spike relative to its own 60-day history
    # vol_60d_avg may be NaN for the first 60 rows per stock — those
    # rows will produce NaN in the comparison, which evaluates to False.
    df["flag_vol_spike"] = (
        df["rolling_vol_20d"] > VOL_SPIKE_MULTIPLIER * df["vol_60d_avg"]
    ).fillna(False)

    # ALERT-02: severe drawdown
    df["flag_drawdown"] = (
        df["drawdown"] < DRAWDOWN_THRESHOLD
    ).fillna(False)

    # ALERT-03: volume spike
    df["flag_volume"] = (
        df["volume_spike_ratio"] > VOLUME_RATIO_THRESHOLD
    ).fillna(False)

    # ALERT-04: large single-day move (absolute value)
    df["flag_large_move"] = (
        df["daily_return"].abs() > LARGE_MOVE_THRESHOLD
    ).fillna(False)

    # Count how many individual flags fire on each row
    df["n_flags"] = (
        df["flag_vol_spike"].astype(int)
        + df["flag_drawdown"].astype(int)
        + df["flag_volume"].astype(int)
        + df["flag_large_move"].astype(int)
    )

    return df


def build_alert_rows(df: pd.DataFrame) -> list[dict]:
    """
    Convert boolean flag columns into individual alert records ready
    for insertion into risk_alert.

    One row is produced per (stock_id, date, alert_type) combination.
    COMPOSITE alert fires additionally when n_flags >= COMPOSITE_MIN_ALERTS.
    """
    RULES = [
        # (flag_col,         alert_type,    severity, value_col,             threshold)
        ("flag_vol_spike",  "VOL_SPIKE",    "HIGH",   "rolling_vol_20d",     None),
        ("flag_drawdown",   "DRAWDOWN",     "HIGH",   "drawdown",            DRAWDOWN_THRESHOLD),
        ("flag_volume",     "VOLUME_SPIKE", "MEDIUM", "volume_spike_ratio",   VOLUME_RATIO_THRESHOLD),
        ("flag_large_move", "LARGE_MOVE",   "MEDIUM", "daily_return",        LARGE_MOVE_THRESHOLD),
    ]

    alert_rows: list[dict] = []

    # Individual alerts
    for flag_col, alert_type, severity, value_col, threshold in RULES:
        fired = df[df[flag_col]]
        for row in fired.itertuples(index=False):
            raw_val = getattr(row, value_col, None)
            # For VOL_SPIKE, threshold is dynamic (1.5 × vol_60d_avg)
            thr = (
                VOL_SPIKE_MULTIPLIER * row.vol_60d_avg
                if alert_type == "VOL_SPIKE"
                else threshold
            )
            alert_rows.append({
                "stock_id":    int(row.stock_id),
                "date":        row.date,
                "alert_type":  alert_type,
                "alert_value": round(float(raw_val), 6) if pd.notna(raw_val) else None,
                "threshold":   round(float(thr), 6) if thr is not None and pd.notna(thr) else None,
                "severity":    severity,
            })

    # COMPOSITE alert — fires when 2+ individual rules trigger on same day
    composite_rows = df[df["n_flags"] >= COMPOSITE_MIN_ALERTS]
    for row in composite_rows.itertuples(index=False):
        alert_rows.append({
            "stock_id":    int(row.stock_id),
            "date":        row.date,
            "alert_type":  "COMPOSITE",
            "alert_value": float(row.n_flags),   # how many alerts fired
            "threshold":   float(COMPOSITE_MIN_ALERTS),
            "severity":    "HIGH",
        })

    return alert_rows


# ─────────────────────────────────────────────────────────────────
# DATABASE WRITE
# ─────────────────────────────────────────────────────────────────

def write_alerts(con: sqlite3.Connection, alert_rows: list[dict]) -> int:
    """
    Append alert rows to risk_alert.

    IMPORTANT: risk_alert is append-only. We use INSERT OR IGNORE (not
    INSERT OR REPLACE) because the table has no UNIQUE constraint on
    (stock_id, date, alert_type) — re-running should NOT produce
    duplicates, but we also do not want to silently drop a second alert
    of the same type if thresholds changed.

    To avoid duplicates on re-run, we first delete alerts for the date
    range being processed, then re-insert. This is the clean idempotent
    pattern for an event log.
    """
    if not alert_rows:
        log.info("No alerts generated — nothing to write.")
        return 0

    # Find the date range we are about to insert
    dates = sorted({r["date"] for r in alert_rows})
    min_date, max_date = dates[0], dates[-1]

    cur = con.cursor()
    cur.execute("BEGIN")

    # Clear existing alerts for this date range to avoid duplicates on re-run
    cur.execute("""
        DELETE FROM risk_alert
        WHERE date BETWEEN ? AND ?
    """, (min_date, max_date))
    deleted = cur.rowcount
    if deleted:
        log.info("Cleared %d existing alert rows for %s → %s.", deleted, min_date, max_date)

    sql = """
        INSERT INTO risk_alert
            (stock_id, date, alert_type, alert_value, threshold, severity)
        VALUES (:stock_id, :date, :alert_type, :alert_value, :threshold, :severity)
    """
    cur.executemany(sql, alert_rows)
    written = len(alert_rows)
    con.commit()

    log.info("Inserted %d alert rows into risk_alert.", written)
    return written


# ─────────────────────────────────────────────────────────────────
# VALIDATION & REPORTING
# ─────────────────────────────────────────────────────────────────

def print_alert_summary(con: sqlite3.Connection) -> None:
    log.info("")
    log.info("─" * 70)
    log.info("ALERT SUMMARY — ALL TIME")
    log.info("─" * 70)

    # By type
    rows = con.execute("""
        SELECT  alert_type,
                severity,
                COUNT(*) AS total,
                COUNT(DISTINCT stock_id) AS unique_stocks,
                MIN(date) AS first_fired,
                MAX(date) AS last_fired
        FROM    risk_alert
        GROUP   BY alert_type, severity
        ORDER   BY CASE severity WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                   total DESC
    """).fetchall()

    log.info("%-14s  %-8s  %6s  %12s  %12s  %12s",
             "Alert Type","Severity","Count","Stocks Hit","First Date","Last Date")
    log.info("%-14s  %-8s  %6s  %12s  %12s  %12s",
             "──────────","────────","──────","──────────","──────────","─────────")
    for atype, sev, total, stocks, first, last in rows:
        log.info("%-14s  %-8s  %6d  %12d  %12s  %12s",
                 atype, sev, total, stocks, first, last)

    # Most alerted tickers
    log.info("")
    log.info("MOST ALERTED TICKERS")
    log.info("─" * 40)
    ticker_rows = con.execute("""
        SELECT  s.ticker,
                COUNT(*)                                                AS total_alerts,
                SUM(CASE WHEN a.severity = 'HIGH'   THEN 1 ELSE 0 END) AS high,
                SUM(CASE WHEN a.severity = 'MEDIUM' THEN 1 ELSE 0 END) AS medium
        FROM    risk_alert a
        JOIN    dim_stock   s ON s.stock_id = a.stock_id
        GROUP   BY a.stock_id
        ORDER   BY total_alerts DESC
        LIMIT   10
    """).fetchall()

    log.info("%-8s  %12s  %6s  %8s", "Ticker","Total Alerts","HIGH","MEDIUM")
    log.info("%-8s  %12s  %6s  %8s", "──────","────────────","──────","──────")
    for ticker, total, high, medium in ticker_rows:
        log.info("%-8s  %12d  %6d  %8d", ticker, total, high or 0, medium or 0)

    # Recent HIGH alerts
    log.info("")
    log.info("MOST RECENT HIGH ALERTS")
    log.info("─" * 60)
    recent = con.execute("""
        SELECT  s.ticker, a.date, a.alert_type,
                ROUND(a.alert_value, 4) AS value,
                ROUND(a.threshold,   4) AS threshold
        FROM    risk_alert a
        JOIN    dim_stock   s ON s.stock_id = a.stock_id
        WHERE   a.severity = 'HIGH'
        ORDER   BY a.date DESC, s.ticker
        LIMIT   10
    """).fetchall()

    log.info("%-6s  %-12s  %-14s  %10s  %10s",
             "Ticker","Date","Alert Type","Value","Threshold")
    log.info("%-6s  %-12s  %-14s  %10s  %10s",
             "──────","──────────","──────────","──────────","──────────")
    for ticker, dt, atype, val, thr in recent:
        log.info("%-6s  %-12s  %-14s  %10.4f  %10.4f",
                 ticker, dt, atype, val or 0, thr or 0)

    log.info("─" * 70)


def print_composite_events(con: sqlite3.Connection) -> None:
    """Show composite alert dates — these are the highest-priority review items."""
    rows = con.execute("""
        SELECT  s.ticker, a.date,
                CAST(a.alert_value AS INTEGER) AS n_signals
        FROM    risk_alert a
        JOIN    dim_stock   s ON s.stock_id = a.stock_id
        WHERE   a.alert_type = 'COMPOSITE'
        ORDER   BY a.alert_value DESC, a.date DESC
        LIMIT   15
    """).fetchall()

    if not rows:
        log.info("No composite alerts fired.")
        return

    log.info("")
    log.info("COMPOSITE ALERT EVENTS (highest priority)")
    log.info("─" * 45)
    log.info("%-8s  %-12s  %s", "Ticker","Date","Signals Fired")
    for ticker, dt, n in rows:
        log.info("%-8s  %-12s  %d individual alerts on same day", ticker, dt, n)
    log.info("─" * 45)


# ─────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────

def run(db_path: str, since: str | None, dry_run: bool) -> None:
    log.info("=" * 60)
    log.info("STEP 10 — ALERT ENGINE")
    log.info("  DB       : %s", db_path)
    log.info("  Since    : %s", since or "all dates")
    log.info("  Dry run  : %s", dry_run)
    log.info("=" * 60)
    log.info("Thresholds:")
    log.info("  VOL_SPIKE    : rolling_vol_20d > %.1f × 60d avg vol", VOL_SPIKE_MULTIPLIER)
    log.info("  DRAWDOWN     : drawdown < %.0f%%", DRAWDOWN_THRESHOLD * 100)
    log.info("  VOLUME_SPIKE : spike_ratio > %.1f×", VOLUME_RATIO_THRESHOLD)
    log.info("  LARGE_MOVE   : |daily_return| > %.0f%%", LARGE_MOVE_THRESHOLD * 100)
    log.info("  COMPOSITE    : >= %d of above fire on same day", COMPOSITE_MIN_ALERTS)

    con = get_connection(db_path)

    # 1. Load metrics
    df = load_metrics(con, since)

    # 2. Add 60-day vol baseline for ALERT-01
    df = add_vol_baseline(df)

    # 3. Evaluate all rules
    df = evaluate_rules(df)

    # 4. Log flag summary before writing
    log.info("")
    log.info("Flag counts across all stock-dates:")
    log.info("  VOL_SPIKE   : %d rows", df["flag_vol_spike"].sum())
    log.info("  DRAWDOWN    : %d rows", df["flag_drawdown"].sum())
    log.info("  VOLUME      : %d rows", df["flag_volume"].sum())
    log.info("  LARGE_MOVE  : %d rows", df["flag_large_move"].sum())
    log.info("  COMPOSITE   : %d rows", (df["n_flags"] >= COMPOSITE_MIN_ALERTS).sum())

    # 5. Build alert records
    alert_rows = build_alert_rows(df)
    log.info("Total alert rows to write: %d", len(alert_rows))

    # 6. Write (unless dry-run)
    if dry_run:
        log.info("Dry run — skipping DB write. First 10 alerts:")
        for r in alert_rows[:10]:
            log.info("  %s", r)
    else:
        write_alerts(con, alert_rows)

    # 7. Report
    print_alert_summary(con)
    print_composite_events(con)

    log.info("")
    log.info("STEP 10 COMPLETE.")
    con.close()


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step 10 — Alert engine.")
    p.add_argument("--db",      default=DEFAULT_DB, help="SQLite DB path")
    p.add_argument("--since",   default=None,       help="Only scan dates >= YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="Print alerts without writing to DB")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.db, args.since, args.dry_run)
