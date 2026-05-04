"""
compute_returns.py
==================
Step 6 — Compute daily simple and log returns from adj_close prices.

What this script does
---------------------
1. Reads fact_price from SQLite (all tickers, all dates).
2. For each stock, sorts by date and computes:
      simple return  Rt  = (P_t / P_{t-1}) - 1
      log    return  rt  = ln(P_t / P_{t-1})
   using pandas shift(1) — vectorised, no Python loops over rows.
3. The first row per stock has no prior price, so its return is NaN.
   That row is dropped — it must not enter fact_return.
4. Writes results to fact_return using INSERT OR REPLACE (idempotent).
5. Prints a per-ticker summary and a cross-ticker sanity check.

Why two return types?
---------------------
  Simple return: intuitive, directly usable for portfolio weighting math.
                 Portfolio return = Σ (weight_i × simple_return_i)
  Log return:    time-additive, approximately normal, better for
                 volatility and risk model inputs (used in Step 7).

Usage
-----
    python compute_returns.py
    python compute_returns.py --db my.db
    python compute_returns.py --db stocks.db --start 2024-01-01
"""

import argparse
import logging
import sqlite3
import sys
import numpy as np
import pandas as pd

DEFAULT_DB = "stocks.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────

def get_connection(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode  = WAL")
    return con


def load_prices(con: sqlite3.Connection, start: str | None = None) -> pd.DataFrame:
    """
    Pull adj_close prices from fact_price joined to dim_stock.

    Returns a DataFrame with columns:
        stock_id (int), ticker (str), date (str), adj_close (float)
    Sorted by ticker then date — required for shift() to work correctly.
    """
    where = f"AND fp.date >= '{start}'" if start else ""
    sql = f"""
        SELECT  fp.stock_id,
                s.ticker,
                fp.date,
                fp.adj_close
        FROM    fact_price fp
        JOIN    dim_stock  s  ON s.stock_id = fp.stock_id
        WHERE   fp.adj_close IS NOT NULL
                {where}
        ORDER   BY fp.stock_id, fp.date
    """
    df = pd.read_sql_query(sql, con)
    log.info("Loaded %d price rows across %d tickers.",
             len(df), df["ticker"].nunique())
    return df


# ─────────────────────────────────────────────────────────────────
# RETURN COMPUTATION
# ─────────────────────────────────────────────────────────────────

def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """
    Compute simple and log daily returns for every stock.

    Key mechanics
    -------------
    groupby("stock_id") ensures shift(1) only looks back within the
    same stock — it never lets the last row of AAPL bleed into the
    first row of ABBV even though they are adjacent in the sorted frame.

    shift(1) moves the previous day's price into the current row's
    column, so the division P_t / P_{t-1} is fully vectorised.

    The first row of each stock has NaN in the shifted column → NaN
    return → we drop it, so fact_return never gets a spurious zero or
    NaN row for the first trading day of each ticker.
    """
    log.info("Computing returns …")

    # ── Step A: shift adj_close by 1 within each stock group ──────
    # transform("shift", 1) applies shift(1) group-by-group and
    # returns a Series with the same index as the original frame.
    prices = prices.copy()
    prices["prev_close"] = (
        prices.groupby("stock_id")["adj_close"].transform(lambda s: s.shift(1))
    )

    # ── Step B: simple return  Rt = (P_t / P_{t-1}) - 1 ──────────
    prices["daily_return"] = (prices["adj_close"] / prices["prev_close"]) - 1

    # ── Step C: log return  rt = ln(P_t / P_{t-1}) ───────────────
    # np.log is applied element-wise on the ratio column.
    # Equivalent to: log(adj_close) - log(prev_close)
    prices["log_return"] = np.log(prices["adj_close"] / prices["prev_close"])

    # ── Step D: drop first row per stock (no prior price) ─────────
    before = len(prices)
    returns = prices.dropna(subset=["daily_return"]).copy()
    dropped = before - len(returns)
    log.info("Dropped %d first-row NaN entries (one per ticker).", dropped)

    # ── Step E: round to 8 decimal places ─────────────────────────
    # 8dp is sufficient precision for daily returns while avoiding
    # floating-point noise like 0.00000000000000001
    returns["daily_return"] = returns["daily_return"].round(8)
    returns["log_return"]   = returns["log_return"].round(8)

    return returns[["stock_id", "date", "daily_return", "log_return"]].reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────
# DATABASE WRITE
# ─────────────────────────────────────────────────────────────────

def write_returns(con: sqlite3.Connection, returns: pd.DataFrame) -> int:
    """
    Upsert return rows into fact_return.
    INSERT OR REPLACE handles re-runs safely.
    executemany() sends all rows for each ticker in one round-trip.
    """
    sql = """
        INSERT OR REPLACE INTO fact_return
            (stock_id, date, daily_return, log_return)
        VALUES (?, ?, ?, ?)
    """
    rows = list(returns.itertuples(index=False, name=None))
    con.execute("BEGIN")
    con.executemany(sql, rows)
    con.commit()
    log.info("Written %d rows to fact_return.", len(rows))
    return len(rows)


# ─────────────────────────────────────────────────────────────────
# VALIDATION & SUMMARY
# ─────────────────────────────────────────────────────────────────

def validate_returns(con: sqlite3.Connection) -> None:
    """
    Print a per-ticker summary from fact_return.
    Checks:
      - row count
      - mean daily return (should be small, e.g. ±0.5% for most stocks)
      - max daily return  (a >20% single-day return is suspicious)
      - min daily return  (a <-20% single-day return warrants inspection)
    """
    log.info("")
    log.info("─" * 70)
    log.info("RETURN VALIDATION REPORT")
    log.info("─" * 70)
    log.info("%-6s  %6s  %10s  %10s  %10s  %10s",
             "Ticker", "Rows", "Mean Ret", "Std Ret", "Max Ret", "Min Ret")
    log.info("%-6s  %6s  %10s  %10s  %10s  %10s",
             "──────", "────", "────────", "───────", "───────", "───────")

    rows = con.execute("""
        SELECT  s.ticker,
                COUNT(*)                          AS n,
                ROUND(AVG(r.daily_return), 6)     AS mean_ret,
                ROUND(AVG(r.daily_return * r.daily_return) -
                      AVG(r.daily_return) * AVG(r.daily_return), 8)
                                                  AS var_ret,
                ROUND(MAX(r.daily_return), 4)     AS max_ret,
                ROUND(MIN(r.daily_return), 4)     AS min_ret
        FROM    fact_return r
        JOIN    dim_stock   s ON s.stock_id = r.stock_id
        GROUP   BY r.stock_id
        ORDER   BY s.ticker
    """).fetchall()

    warnings = []
    for ticker, n, mean, var, mx, mn in rows:
        std = round(var ** 0.5, 6) if var and var > 0 else 0.0
        flag = ""
        if mx and mx > 0.25:
            flag = "  ← large spike"
            warnings.append(f"{ticker} max={mx:.2%}")
        if mn and mn < -0.25:
            flag = "  ← large drop"
            warnings.append(f"{ticker} min={mn:.2%}")
        log.info("%-6s  %6d  %10.6f  %10.6f  %10.4f  %10.4f%s",
                 ticker, n, mean or 0, std, mx or 0, mn or 0, flag)

    # ── Total rows ────────────────────────────────────────────────
    total = con.execute("SELECT COUNT(*) FROM fact_return").fetchone()[0]
    log.info("─" * 70)
    log.info("Total rows in fact_return: %d", total)

    # ── Relationship check: fact_return rows ≈ fact_price rows - n_tickers
    price_rows = con.execute("SELECT COUNT(*) FROM fact_price").fetchone()[0]
    n_tickers  = con.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
    expected   = price_rows - n_tickers
    diff = abs(total - expected)
    if diff <= n_tickers:
        log.info("Row count check: PASS  (fact_return=%d, expected≈%d)",
                 total, expected)
    else:
        log.warning("Row count check: MISMATCH  "
                    "(fact_return=%d, expected≈%d, diff=%d)",
                    total, expected, diff)

    if warnings:
        log.warning("Large single-day moves detected — verify these dates:")
        for w in warnings:
            log.warning("  %s", w)
    else:
        log.info("No extreme single-day moves detected.")

    log.info("─" * 70)


def print_sample(con: sqlite3.Connection) -> None:
    """Show the 5 most recent return rows for AAPL as a visual sanity check."""
    rows = con.execute("""
        SELECT  r.date,
                ROUND(r.daily_return * 100, 4) AS daily_ret_pct,
                ROUND(r.log_return   * 100, 4) AS log_ret_pct
        FROM    fact_return r
        JOIN    dim_stock   s ON s.stock_id = r.stock_id
        WHERE   s.ticker = 'AAPL'
        ORDER   BY r.date DESC
        LIMIT   5
    """).fetchall()

    log.info("")
    log.info("Sample — AAPL last 5 return rows:")
    log.info("  %-12s  %14s  %14s", "date", "daily_ret (%)", "log_ret (%)")
    log.info("  %-12s  %14s  %14s", "──────────", "─────────────", "────────────")
    for date, dr, lr in rows:
        log.info("  %-12s  %14.4f  %14.4f", date, dr, lr)


# ─────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────

def run(db_path: str, start: str | None = None) -> None:
    log.info("=" * 60)
    log.info("STEP 6 — COMPUTE DAILY RETURNS")
    log.info("  DB    : %s", db_path)
    if start:
        log.info("  Start : %s", start)
    log.info("=" * 60)

    con = get_connection(db_path)

    # 1. Load prices
    prices = load_prices(con, start)
    if prices.empty:
        log.error("No price data found. Run Steps 3–5 first.")
        sys.exit(1)

    # 2. Compute
    returns = compute_returns(prices)

    # 3. Write
    write_returns(con, returns)

    # 4. Validate
    validate_returns(con)
    print_sample(con)

    log.info("")
    log.info("STEP 6 COMPLETE — fact_return is populated.")
    con.close()


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step 6 — Compute daily returns.")
    p.add_argument("--db",    default=DEFAULT_DB, help="SQLite database path")
    p.add_argument("--start", default=None,       help="Optional start date YYYY-MM-DD")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.db, args.start)
