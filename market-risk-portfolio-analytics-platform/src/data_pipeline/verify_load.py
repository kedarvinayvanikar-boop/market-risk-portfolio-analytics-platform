"""
verify_load.py
==============
Step 5 — Quality-check the SQLite database after ingestion.

What this script does
---------------------
Runs 12 SQL quality checks against stocks.db and prints a structured
pass/fail report. It checks:

  QC-01  Schema integrity       — all 8 expected tables exist
  QC-02  Dimension completeness — 5 sectors and 25 stocks seeded
  QC-03  Total row count        — fact_price has a plausible number of rows
  QC-04  Per-ticker row count   — every stock has at least 200 rows
  QC-05  Date range             — first and last date for every ticker
  QC-06  Duplicate detection    — no (stock_id, date) pairs appear twice
  QC-07  NULL adj_close         — no missing adjusted close values
  QC-08  Benchmark presence     — SPY rows exist in benchmark_price
  QC-09  Trading day alignment  — every stock appears on the same set of dates
  QC-10  Price sanity           — no rows where high < low or close ≤ 0
  QC-11  Volume sanity          — no rows with negative volume
  QC-12  Foreign key integrity  — every fact_price.stock_id exists in dim_stock

Usage
-----
    python verify_load.py               # default: stocks.db
    python verify_load.py --db my.db    # custom path
    python verify_load.py --verbose     # print first 10 rows of each check
"""

import argparse
import sqlite3
import sys
import textwrap
from datetime import datetime
from typing import Optional

# ── Palette for console output (ANSI colours) ─────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

PASS_MARK = f"{GREEN}✓ PASS{RESET}"
FAIL_MARK = f"{RED}✗ FAIL{RESET}"
WARN_MARK = f"{YELLOW}⚠ WARN{RESET}"

EXPECTED_TABLES = [
    "dim_sector", "dim_stock", "fact_price", "fact_return",
    "fact_risk_metric", "portfolio_holding", "portfolio_daily_value",
    "benchmark_price", "risk_alert",
]
EXPECTED_SECTORS = 5
EXPECTED_STOCKS  = 25
MIN_ROWS_PER_TICKER = 200   # ~1 year of trading days
MIN_TOTAL_ROWS      = 10_000


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def section(title: str) -> None:
    width = 64
    print()
    print(f"{BOLD}{CYAN}{'─' * width}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * width}{RESET}")


def result_line(check_id: str, description: str, status: str, detail: str = "") -> None:
    detail_str = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {BOLD}{check_id}{RESET}  {description:<42} {status}{detail_str}")


def fetch_all(con: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    return con.execute(textwrap.dedent(sql), params).fetchall()


def fmt_table(rows: list[sqlite3.Row], max_rows: int = 10) -> str:
    """Format a list of sqlite3.Row objects as a simple text table."""
    if not rows:
        return "    (no rows)"
    keys = list(rows[0].keys())
    col_widths = [max(len(k), max(len(str(r[k])) for r in rows)) for k in keys]
    header = "    " + "  ".join(k.ljust(w) for k, w in zip(keys, col_widths))
    divider = "    " + "  ".join("─" * w for w in col_widths)
    body = "\n".join(
        "    " + "  ".join(str(r[k]).ljust(w) for k, w in zip(keys, col_widths))
        for r in rows[:max_rows]
    )
    suffix = f"\n    … and {len(rows) - max_rows} more rows" if len(rows) > max_rows else ""
    return f"\n{header}\n{divider}\n{body}{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# INDIVIDUAL CHECKS
# ─────────────────────────────────────────────────────────────────────────────

def qc_01_schema(con, verbose) -> bool:
    """All expected tables exist in the database."""
    existing = {r[0] for r in fetch_all(con,
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in EXPECTED_TABLES if t not in existing]
    ok = len(missing) == 0
    detail = f"missing: {', '.join(missing)}" if missing else f"{len(existing)} tables found"
    result_line("QC-01", "Schema integrity", PASS_MARK if ok else FAIL_MARK, detail)
    if verbose:
        rows = fetch_all(con,
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")
        print(fmt_table(rows))
    return ok


def qc_02_dimensions(con, verbose) -> bool:
    """Correct number of sectors and stocks seeded."""
    n_sec = fetch_all(con, "SELECT COUNT(*) AS n FROM dim_sector")[0]["n"]
    n_stk = fetch_all(con, "SELECT COUNT(*) AS n FROM dim_stock")[0]["n"]
    ok_sec = n_sec >= EXPECTED_SECTORS
    ok_stk = n_stk >= EXPECTED_STOCKS
    ok = ok_sec and ok_stk
    detail = f"{n_sec} sectors, {n_stk} stocks"
    result_line("QC-02", "Dimension completeness", PASS_MARK if ok else FAIL_MARK, detail)
    if verbose:
        rows = fetch_all(con, """
            SELECT sec.sector_name, COUNT(*) AS stocks
            FROM   dim_stock  s
            JOIN   dim_sector sec ON sec.sector_id = s.sector_id
            GROUP  BY sec.sector_name
            ORDER  BY stocks DESC
        """)
        print(fmt_table(rows))
    return ok


def qc_03_total_rows(con, verbose) -> bool:
    """fact_price has a plausible total row count."""
    n = fetch_all(con, "SELECT COUNT(*) AS n FROM fact_price")[0]["n"]
    ok = n >= MIN_TOTAL_ROWS
    detail = f"{n:,} rows  (min expected: {MIN_TOTAL_ROWS:,})"
    result_line("QC-03", "Total row count", PASS_MARK if ok else FAIL_MARK, detail)
    return ok


def qc_04_per_ticker_rows(con, verbose) -> bool:
    """Every stock has at least MIN_ROWS_PER_TICKER rows."""
    rows = fetch_all(con, """
        SELECT  s.ticker,
                COUNT(fp.price_id) AS row_count
        FROM    dim_stock  s
        LEFT JOIN fact_price fp ON fp.stock_id = s.stock_id
        GROUP   BY s.ticker
        ORDER   BY row_count ASC
    """)
    low = [r for r in rows if r["row_count"] < MIN_ROWS_PER_TICKER]
    ok  = len(low) == 0
    min_count = rows[0]["row_count"] if rows else 0
    max_count = rows[-1]["row_count"] if rows else 0
    detail = f"min={min_count}, max={max_count}" + (f"  {len(low)} tickers LOW" if low else "")
    result_line("QC-04", "Per-ticker row count", PASS_MARK if ok else WARN_MARK, detail)
    if verbose or low:
        print(fmt_table(rows, max_rows=30))
    return ok


def qc_05_date_range(con, verbose) -> bool:
    """Each ticker has a sensible date range spanning at least 1 year."""
    rows = fetch_all(con, """
        SELECT  s.ticker,
                MIN(fp.date) AS first_date,
                MAX(fp.date) AS last_date,
                CAST(julianday(MAX(fp.date)) - julianday(MIN(fp.date)) AS INTEGER) AS span_days
        FROM    dim_stock  s
        LEFT JOIN fact_price fp ON fp.stock_id = s.stock_id
        GROUP   BY s.ticker
        ORDER   BY s.ticker
    """)
    short = [r for r in rows if r["span_days"] is None or r["span_days"] < 365]
    ok = len(short) == 0
    if rows and rows[0]["first_date"]:
        detail = f"range: {rows[0]['first_date']} → {rows[-1]['last_date']}"
    else:
        detail = "no dates found"
    result_line("QC-05", "Date range validity", PASS_MARK if ok else WARN_MARK, detail)
    if verbose:
        print(fmt_table(rows, max_rows=30))
    return ok


def qc_06_duplicates(con, verbose) -> bool:
    """No (stock_id, date) pair appears more than once in fact_price."""
    rows = fetch_all(con, """
        SELECT  s.ticker, fp.date, COUNT(*) AS occurrences
        FROM    fact_price fp
        JOIN    dim_stock  s ON s.stock_id = fp.stock_id
        GROUP   BY fp.stock_id, fp.date
        HAVING  COUNT(*) > 1
        ORDER   BY occurrences DESC
        LIMIT   20
    """)
    ok = len(rows) == 0
    detail = "no duplicates found" if ok else f"{len(rows)} duplicate pairs detected"
    result_line("QC-06", "Duplicate (ticker, date) pairs", PASS_MARK if ok else FAIL_MARK, detail)
    if verbose or not ok:
        if rows:
            print(fmt_table(rows))
    return ok


def qc_07_null_adj_close(con, verbose) -> bool:
    """No rows in fact_price have a NULL adj_close."""
    rows = fetch_all(con, """
        SELECT  s.ticker, fp.date
        FROM    fact_price fp
        JOIN    dim_stock  s ON s.stock_id = fp.stock_id
        WHERE   fp.adj_close IS NULL
        ORDER   BY s.ticker, fp.date
        LIMIT   20
    """)
    ok = len(rows) == 0
    detail = "no NULL adj_close" if ok else f"{len(rows)} NULL rows found"
    result_line("QC-07", "No NULL adj_close values", PASS_MARK if ok else FAIL_MARK, detail)
    if not ok:
        print(fmt_table(rows))
    return ok


def qc_08_benchmark(con, verbose) -> bool:
    """SPY data exists in benchmark_price with a plausible row count."""
    row = fetch_all(con, """
        SELECT COUNT(*) AS n, MIN(date) AS first, MAX(date) AS last
        FROM   benchmark_price
    """)[0]
    ok = row["n"] >= MIN_ROWS_PER_TICKER
    detail = f"{row['n']} rows  {row['first']} → {row['last']}"
    result_line("QC-08", "Benchmark (SPY) data present", PASS_MARK if ok else FAIL_MARK, detail)
    if verbose:
        sample = fetch_all(con,
            "SELECT * FROM benchmark_price ORDER BY date DESC LIMIT 5")
        print(fmt_table(sample))
    return ok


def qc_09_alignment(con, verbose) -> bool:
    """
    Trading day alignment: the number of distinct dates in fact_price should
    equal the date count for the ticker with the most dates.
    Misaligned tickers (fewer dates than the maximum) are listed.
    """
    rows = fetch_all(con, """
        SELECT  s.ticker,
                COUNT(DISTINCT fp.date) AS trading_days
        FROM    dim_stock  s
        JOIN    fact_price fp ON fp.stock_id = s.stock_id
        GROUP   BY s.ticker
        ORDER   BY trading_days ASC
    """)
    if not rows:
        result_line("QC-09", "Trading day alignment", WARN_MARK, "no data")
        return False

    max_days = max(r["trading_days"] for r in rows)
    misaligned = [r for r in rows if r["trading_days"] < max_days - 5]  # allow 5-day tolerance
    ok = len(misaligned) == 0
    detail = f"max={max_days} days  {len(misaligned)} tickers misaligned"
    result_line("QC-09", "Trading day alignment", PASS_MARK if ok else WARN_MARK, detail)
    if verbose or misaligned:
        print(fmt_table(rows, max_rows=30))
    return ok


def qc_10_price_sanity(con, verbose) -> bool:
    """No rows where high < low, or close ≤ 0."""
    rows = fetch_all(con, """
        SELECT  s.ticker, fp.date,
                fp.high, fp.low, fp.close
        FROM    fact_price fp
        JOIN    dim_stock  s ON s.stock_id = fp.stock_id
        WHERE   fp.high < fp.low
           OR   fp.close <= 0
        ORDER   BY s.ticker, fp.date
        LIMIT   20
    """)
    ok = len(rows) == 0
    detail = "all prices sane" if ok else f"{len(rows)} suspect rows"
    result_line("QC-10", "Price sanity (high≥low, close>0)", PASS_MARK if ok else FAIL_MARK, detail)
    if not ok:
        print(fmt_table(rows))
    return ok


def qc_11_volume_sanity(con, verbose) -> bool:
    """No rows with negative volume."""
    rows = fetch_all(con, """
        SELECT  s.ticker, fp.date, fp.volume
        FROM    fact_price fp
        JOIN    dim_stock  s ON s.stock_id = fp.stock_id
        WHERE   fp.volume < 0
        LIMIT   20
    """)
    ok = len(rows) == 0
    detail = "no negative volume" if ok else f"{len(rows)} rows with negative volume"
    result_line("QC-11", "Volume sanity (≥ 0)", PASS_MARK if ok else FAIL_MARK, detail)
    if not ok:
        print(fmt_table(rows))
    return ok


def qc_12_foreign_keys(con, verbose) -> bool:
    """Every fact_price.stock_id has a matching row in dim_stock."""
    rows = fetch_all(con, """
        SELECT  DISTINCT fp.stock_id
        FROM    fact_price fp
        WHERE   fp.stock_id NOT IN (SELECT stock_id FROM dim_stock)
    """)
    ok = len(rows) == 0
    detail = "all foreign keys valid" if ok else f"{len(rows)} orphan stock_id values"
    result_line("QC-12", "Foreign key integrity", PASS_MARK if ok else FAIL_MARK, detail)
    if not ok:
        print(fmt_table(rows))
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# BONUS: USEFUL ANALYST QUERIES
# ─────────────────────────────────────────────────────────────────────────────

def print_sample_queries(con: sqlite3.Connection) -> None:
    section("SAMPLE SQL QUERIES — Confirm Data Looks Right")

    print(f"\n  {BOLD}A) Most recent 5 rows for AAPL:{RESET}")
    rows = fetch_all(con, """
        SELECT  fp.date, fp.open, fp.high, fp.low,
                fp.close, fp.adj_close, fp.volume
        FROM    fact_price fp
        JOIN    dim_stock  s  ON s.stock_id = fp.stock_id
        WHERE   s.ticker = 'AAPL'
        ORDER   BY fp.date DESC
        LIMIT   5
    """)
    print(fmt_table(rows))

    print(f"\n  {BOLD}B) Row count by sector:{RESET}")
    rows = fetch_all(con, """
        SELECT  sec.sector_name,
                COUNT(DISTINCT s.stock_id) AS stocks,
                COUNT(fp.price_id)         AS price_rows
        FROM    dim_sector sec
        JOIN    dim_stock  s  ON s.sector_id  = sec.sector_id
        JOIN    fact_price fp ON fp.stock_id  = s.stock_id
        GROUP   BY sec.sector_name
        ORDER   BY price_rows DESC
    """)
    print(fmt_table(rows))

    print(f"\n  {BOLD}C) Price range check — highest and lowest adj_close per ticker:{RESET}")
    rows = fetch_all(con, """
        SELECT  s.ticker,
                ROUND(MIN(fp.adj_close), 2) AS min_adj_close,
                ROUND(MAX(fp.adj_close), 2) AS max_adj_close,
                ROUND(MAX(fp.adj_close) / MIN(fp.adj_close) - 1, 4) AS total_return
        FROM    fact_price fp
        JOIN    dim_stock  s ON s.stock_id = fp.stock_id
        GROUP   BY s.ticker
        ORDER   BY total_return DESC
    """)
    print(fmt_table(rows, max_rows=30))

    print(f"\n  {BOLD}D) Latest SPY benchmark row:{RESET}")
    rows = fetch_all(con, """
        SELECT * FROM benchmark_price
        ORDER BY date DESC LIMIT 3
    """)
    print(fmt_table(rows))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_checks(db_path: str, verbose: bool) -> None:
    print()
    print(f"{BOLD}{'═' * 64}{RESET}")
    print(f"{BOLD}  STEP 5 — DATABASE LOAD VERIFICATION{RESET}")
    print(f"{BOLD}  Database : {db_path}{RESET}")
    print(f"{BOLD}  Run at   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{'═' * 64}{RESET}")

    try:
        con = connect(db_path)
    except Exception as exc:
        print(f"\n{RED}Cannot open database: {exc}{RESET}")
        sys.exit(1)

    section("QUALITY CHECKS")

    checks = [
        qc_01_schema,
        qc_02_dimensions,
        qc_03_total_rows,
        qc_04_per_ticker_rows,
        qc_05_date_range,
        qc_06_duplicates,
        qc_07_null_adj_close,
        qc_08_benchmark,
        qc_09_alignment,
        qc_10_price_sanity,
        qc_11_volume_sanity,
        qc_12_foreign_keys,
    ]

    results = [fn(con, verbose) for fn in checks]

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(results)
    total  = len(results)
    failed = total - passed

    section("SUMMARY")
    print(f"  Checks run    : {total}")
    print(f"  {GREEN}Passed        : {passed}{RESET}")
    if failed:
        print(f"  {RED}Failed        : {failed}{RESET}")
        print(f"\n  {RED}Action required: review FAIL items above before running Step 6.{RESET}")
    else:
        print(f"\n  {GREEN}{BOLD}All checks passed. Database is ready for Step 6 (metric computation).{RESET}")

    # ── Sample queries ────────────────────────────────────────────────────────
    if passed >= 3:   # only show if enough data was loaded
        print_sample_queries(con)

    con.close()
    print()
    sys.exit(0 if failed == 0 else 1)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Step 5 — Verify the SQLite database load."
    )
    p.add_argument("--db",      default="stocks.db", help="Path to SQLite database")
    p.add_argument("--verbose", action="store_true",  help="Print row samples for each check")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_checks(args.db, args.verbose)
