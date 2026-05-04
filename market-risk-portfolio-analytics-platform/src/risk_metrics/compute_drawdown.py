"""
compute_drawdown.py
===================
Step 8 — Compute drawdowns and update fact_risk_metric.

Relationship to Step 7
-----------------------
Step 7 (compute_volatility.py) already writes a drawdown column to
fact_risk_metric using a 252-day ROLLING peak.  This script is the
standalone, focused version that can:
  - be run independently to recompute ONLY the drawdown column
  - switch between CUMULATIVE and ROLLING peak methods
  - produce a richer diagnostic report on drawdown behaviour

Run this script after Step 7 if you want to:
  a) change the peak method (cumulative vs rolling)
  b) inspect or validate drawdown values in isolation
  c) re-run drawdown without recomputing volatility

Peak methods
------------
CUMULATIVE (default, mode="cumulative"):
    rolling_max_t = max(P_1, P_2, ..., P_t)
    Uses pandas cummax() — expanding window from the first date in the DB.
    Always non-decreasing.
    Use case: "how far is this stock from its all-time high in the dataset?"

ROLLING (mode="rolling", window=252):
    rolling_max_t = max(P_{t-251}, ..., P_t)
    Uses pandas rolling(252).max() — fixed 1-year lookback.
    Can recover to 0 even if historical peak was years ago.
    Use case: "how far is this stock from its 1-year high?"

Formula (same in both cases)
-----------------------------
    Drawdown_t = (P_t - peak_t) / peak_t

Always <= 0:
    0.000  -> at or above the reference peak (new high)
   -0.200  -> 20% below peak
   -0.500  -> 50% below peak (severe; may trigger alerts)

Usage
-----
    python compute_drawdown.py                         # cumulative, all data
    python compute_drawdown.py --mode rolling          # rolling 252-day peak
    python compute_drawdown.py --mode rolling --window 60   # 60-day peak
    python compute_drawdown.py --db my.db
    python compute_drawdown.py --report                # extended report only
"""

import argparse
import logging
import sqlite3
import sys

import pandas as pd

DEFAULT_DB     = "stocks.db"
DEFAULT_WINDOW = 252   # used only in rolling mode

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


def load_prices(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Pull adj_close from fact_price.
    Returns: stock_id, ticker, date, adj_close
    Sorted by stock_id then date — essential for cummax() / rolling().
    """
    df = pd.read_sql_query("""
        SELECT  fp.stock_id,
                s.ticker,
                fp.date,
                fp.adj_close
        FROM    fact_price fp
        JOIN    dim_stock  s  ON s.stock_id = fp.stock_id
        WHERE   fp.adj_close IS NOT NULL
        ORDER   BY fp.stock_id, fp.date
    """, con)

    if df.empty:
        raise RuntimeError("fact_price is empty. Run ingest_prices.py first.")

    log.info("Loaded %d price rows across %d tickers.",
             len(df), df["ticker"].nunique())
    return df


# ─────────────────────────────────────────────────────────────────
# DRAWDOWN COMPUTATION
# ─────────────────────────────────────────────────────────────────

def compute_drawdown_cumulative(prices: pd.DataFrame) -> pd.Series:
    """
    Cumulative drawdown: peak = max(P_1 ... P_t) from start of dataset.

    cummax() returns a Series of the running maximum up to each row.
    It never decreases — once a new high is set, it stays there until
    an even higher price appears.

    groupby ensures the cummax resets at each new stock, not between
    the last AAPL row and the first ABBV row.

    Timeline example for a single stock:
        date        price   cummax   drawdown
        2023-01-03  150.00  150.00    0.000
        2023-01-04  153.00  153.00    0.000   <- new high
        2023-01-05  148.00  153.00   -0.033   <- 3.3% below peak
        2023-01-06  151.00  153.00   -0.013
        2023-01-09  155.00  155.00    0.000   <- new high
        2023-01-10  140.00  155.00   -0.097   <- 9.7% below peak
    """
    running_max = (
        prices
        .groupby("stock_id")["adj_close"]
        .transform("cummax")           # pandas built-in expanding max
    )
    drawdown = (prices["adj_close"] - running_max) / running_max
    return drawdown


def compute_drawdown_rolling(prices: pd.DataFrame, window: int) -> pd.Series:
    """
    Rolling drawdown: peak = max over a fixed trailing window.

    min_periods=1 so day-1 drawdown is 0.0 (price equals its own peak),
    matching the cumulative approach's behaviour at the start.

    Unlike cumulative, rolling drawdown CAN recover to 0 even after a
    large historical decline — it 'forgets' peaks older than `window`
    trading days.
    """
    rolling_max = (
        prices
        .groupby("stock_id")["adj_close"]
        .transform(lambda s: s.rolling(window=window, min_periods=1).max())
    )
    drawdown = (prices["adj_close"] - rolling_max) / rolling_max
    return drawdown


# ─────────────────────────────────────────────────────────────────
# MAXIMUM DRAWDOWN SUMMARY
# ─────────────────────────────────────────────────────────────────

def compute_max_drawdown_stats(prices: pd.DataFrame, drawdown: pd.Series) -> pd.DataFrame:
    """
    For each stock, find:
      - maximum drawdown (worst point reached)
      - date of maximum drawdown
      - date of the peak that preceded the worst drawdown
      - recovery: was a new high reached after the trough?

    This is a richer diagnostic than just storing the daily drawdown value.
    It answers: "what was the worst episode and when did it happen?"
    """
    df = prices[["stock_id", "ticker", "date", "adj_close"]].copy()
    df["drawdown"] = drawdown.values

    records = []
    for stock_id, grp in df.groupby("stock_id"):
        ticker = grp["ticker"].iloc[0]
        grp = grp.reset_index(drop=True)

        # Maximum drawdown = most negative value
        min_idx = grp["drawdown"].idxmin()
        max_dd  = grp["drawdown"].iloc[min_idx]
        trough_date  = grp["date"].iloc[min_idx]
        trough_price = grp["adj_close"].iloc[min_idx]

        # Peak before the trough: find the cummax up to trough date
        pre_trough = grp.iloc[: min_idx + 1]
        peak_idx   = pre_trough["adj_close"].idxmax()
        peak_date  = pre_trough["date"].iloc[peak_idx]
        peak_price = pre_trough["adj_close"].iloc[peak_idx]

        # Recovery: did price ever exceed peak_price after trough?
        post_trough = grp.iloc[min_idx:]
        recovered   = (post_trough["adj_close"] > peak_price).any()

        records.append({
            "ticker":       ticker,
            "max_drawdown": round(max_dd, 4),
            "peak_date":    peak_date,
            "peak_price":   round(peak_price, 2),
            "trough_date":  trough_date,
            "trough_price": round(trough_price, 2),
            "recovered":    "Yes" if recovered else "No",
        })

    return pd.DataFrame(records).sort_values("max_drawdown")


# ─────────────────────────────────────────────────────────────────
# DATABASE WRITE
# ─────────────────────────────────────────────────────────────────

def update_drawdown_column(
    con: sqlite3.Connection,
    prices: pd.DataFrame,
    drawdown: pd.Series,
) -> int:
    """
    Update ONLY the drawdown column in existing fact_risk_metric rows.

    Uses INSERT OR REPLACE so the row is created if it doesn't exist yet
    (e.g. if Step 7 hasn't run), or updated if it has.

    All other metric columns (rolling_vol_20d, rolling_avg_volume,
    volume_spike_ratio) are preserved via a subquery.
    """
    df = prices[["stock_id", "date"]].copy()
    df["drawdown"] = drawdown.round(6).values

    # Use INSERT OR REPLACE — other columns preserved by reading existing row
    sql_existing = """
        SELECT rolling_vol_20d, rolling_avg_volume, volume_spike_ratio
        FROM   fact_risk_metric
        WHERE  stock_id = ? AND date = ?
    """
    sql_upsert = """
        INSERT OR REPLACE INTO fact_risk_metric
            (stock_id, date, rolling_vol_20d, drawdown,
             rolling_avg_volume, volume_spike_ratio)
        VALUES (?, ?, ?, ?, ?, ?)
    """

    cur = con.cursor()
    cur.execute("BEGIN")

    rows_written = 0
    for row in df.itertuples(index=False):
        # Fetch existing other-column values (may be NULL if Step 7 not run)
        existing = cur.execute(sql_existing, (row.stock_id, row.date)).fetchone()
        if existing:
            vol, avg_vol, spike = existing
        else:
            vol, avg_vol, spike = None, None, None

        cur.execute(sql_upsert, (
            int(row.stock_id),
            row.date,
            vol,
            float(row.drawdown) if pd.notna(row.drawdown) else None,
            avg_vol,
            spike,
        ))
        rows_written += 1

    con.commit()
    log.info("Updated drawdown for %d rows in fact_risk_metric.", rows_written)
    return rows_written


# ─────────────────────────────────────────────────────────────────
# VALIDATION & REPORTING
# ─────────────────────────────────────────────────────────────────

def validate(con: sqlite3.Connection) -> None:
    log.info("")
    log.info("─" * 72)
    log.info("DRAWDOWN VALIDATION REPORT")
    log.info("─" * 72)
    log.info("%-6s  %10s  %10s  %10s  %12s",
             "Ticker", "Rows", "Min DD", "Avg DD", "Days < -20%")
    log.info("%-6s  %10s  %10s  %10s  %12s",
             "──────","──────","──────","──────","───────────")

    rows = con.execute("""
        SELECT  s.ticker,
                COUNT(*)                                AS n,
                ROUND(MIN(m.drawdown), 4)               AS min_dd,
                ROUND(AVG(m.drawdown), 4)               AS avg_dd,
                SUM(CASE WHEN m.drawdown < -0.20
                         THEN 1 ELSE 0 END)             AS days_severe
        FROM    fact_risk_metric m
        JOIN    dim_stock        s ON s.stock_id = m.stock_id
        WHERE   m.drawdown IS NOT NULL
        GROUP   BY m.stock_id
        ORDER   BY min_dd ASC
    """).fetchall()

    for ticker, n, min_dd, avg_dd, days_severe in rows:
        flag = "  ← severe" if min_dd and min_dd < -0.40 else ""
        log.info("%-6s  %10d  %10.4f  %10.4f  %12d%s",
                 ticker, n, min_dd or 0, avg_dd or 0, days_severe or 0, flag)

    log.info("─" * 72)
    # Invariant: drawdown must always be <= 0
    violations = con.execute("""
        SELECT COUNT(*) FROM fact_risk_metric WHERE drawdown > 0.0001
    """).fetchone()[0]
    if violations == 0:
        log.info("Invariant check: PASS  (all drawdown values <= 0)")
    else:
        log.warning("Invariant check: FAIL  (%d rows with drawdown > 0)", violations)


def print_max_dd_report(stats: pd.DataFrame) -> None:
    log.info("")
    log.info("MAXIMUM DRAWDOWN EPISODES (worst to best)")
    log.info("─" * 82)
    log.info("%-6s  %10s  %12s  %10s  %12s  %10s  %9s",
             "Ticker","Max DD","Peak Date","Peak $","Trough Date","Trough $","Recovered")
    log.info("%-6s  %10s  %12s  %10s  %12s  %10s  %9s",
             "──────","──────","─────────","──────","──────────","────────","─────────")
    for _, r in stats.iterrows():
        log.info("%-6s  %10.2f%%  %12s  %10.2f  %12s  %10.2f  %9s",
                 r["ticker"], r["max_drawdown"] * 100,
                 r["peak_date"], r["peak_price"],
                 r["trough_date"], r["trough_price"],
                 r["recovered"])
    log.info("─" * 82)


# ─────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────

def run(db_path: str, mode: str, window: int, report_only: bool) -> None:
    log.info("=" * 60)
    log.info("STEP 8 — COMPUTE DRAWDOWNS")
    log.info("  DB     : %s", db_path)
    log.info("  Mode   : %s", mode)
    if mode == "rolling":
        log.info("  Window : %d trading days", window)
    log.info("=" * 60)

    con = get_connection(db_path)
    prices = load_prices(con)

    # Compute drawdown series
    if mode == "cumulative":
        drawdown = compute_drawdown_cumulative(prices)
        log.info("Using CUMULATIVE peak (cummax from dataset start).")
    else:
        drawdown = compute_drawdown_rolling(prices, window)
        log.info("Using ROLLING peak (%d-day window).", window)

    # Max drawdown episode stats (diagnostic only, not written to DB)
    stats = compute_max_drawdown_stats(prices, drawdown)

    if not report_only:
        # Write drawdown column to fact_risk_metric
        update_drawdown_column(con, prices, drawdown)
    else:
        log.info("--report flag set: skipping DB write.")

    # Validate and report
    validate(con)
    print_max_dd_report(stats)

    log.info("")
    log.info("STEP 8 COMPLETE.")
    con.close()


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step 8 — Compute drawdowns.")
    p.add_argument("--db",     default=DEFAULT_DB,    help="SQLite DB path")
    p.add_argument("--mode",   default="cumulative",  choices=["cumulative", "rolling"],
                   help="Peak method: cumulative (default) or rolling")
    p.add_argument("--window", default=DEFAULT_WINDOW, type=int,
                   help=f"Rolling window in days (only used if --mode rolling, default {DEFAULT_WINDOW})")
    p.add_argument("--report", action="store_true", dest="report_only",
                   help="Print diagnostic report without writing to DB")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.db, args.mode, args.window, args.report_only)
