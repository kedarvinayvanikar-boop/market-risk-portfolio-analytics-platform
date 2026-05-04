"""
ingest_prices.py
================
Step 4 — Pull raw market data and load into fact_price.

What this script does
---------------------
1. Reads the stock universe from dim_stock / dim_sector (already seeded in Step 3).
2. Downloads 2 years of daily OHLCV + adjusted-close data from Yahoo Finance
   via yfinance, for every ticker in the universe plus the SPY benchmark.
3. Cleans and standardises the raw DataFrame:
   - Renames columns to match the DB schema
   - Converts dates to ISO 'YYYY-MM-DD' strings
   - Drops rows where adj_close is NaN (non-trading days / data gaps)
   - Rounds price columns to 4 dp, volume to integer
4. Writes each row into fact_price using INSERT OR REPLACE (idempotent).
5. Writes SPY daily prices into a separate benchmark_price table.
6. Prints a summary report when finished.

Usage
-----
    python ingest_prices.py                  # uses default DB path
    python ingest_prices.py --db my.db       # custom DB path
    python ingest_prices.py --start 2021-01-01 --end 2023-12-31

Dependencies
------------
    pip install yfinance pandas
"""

import argparse
import datetime
import logging
import sqlite3
import sys
import time
from typing import Optional

import pandas as pd
import yfinance as yf

# ── Configuration ─────────────────────────────────────────────────────────────
DEFAULT_DB    = "stocks.db"
DEFAULT_START = (datetime.date.today() - datetime.timedelta(days=730)).isoformat()
DEFAULT_END   = datetime.date.today().isoformat()
BENCHMARK     = "SPY"
RETRY_LIMIT   = 3          # how many times to retry a failed download
RETRY_DELAY   = 5          # seconds between retries
BATCH_SIZE    = 5          # tickers per yfinance batch download

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_connection(db_path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite database and enable foreign keys."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")   # faster concurrent writes
    return con


def ensure_tables(con: sqlite3.Connection) -> None:
    """
    Create all schema tables if they don't already exist.
    Running this is safe even after Step 2/3 — CREATE IF NOT EXISTS is a no-op
    when tables are already present.
    """
    con.executescript("""
        CREATE TABLE IF NOT EXISTS dim_sector (
            sector_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            sector_name TEXT    NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS dim_stock (
            stock_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker       TEXT    NOT NULL UNIQUE,
            company_name TEXT    NOT NULL,
            sector_id    INTEGER NOT NULL,
            FOREIGN KEY (sector_id) REFERENCES dim_sector(sector_id)
        );

        CREATE TABLE IF NOT EXISTS fact_price (
            price_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id  INTEGER NOT NULL,
            date      TEXT    NOT NULL,
            open      REAL,
            high      REAL,
            low       REAL,
            close     REAL,
            adj_close REAL,
            volume    INTEGER,
            FOREIGN KEY (stock_id) REFERENCES dim_stock(stock_id),
            UNIQUE (stock_id, date)
        );

        CREATE TABLE IF NOT EXISTS benchmark_price (
            date             TEXT PRIMARY KEY,
            open             REAL,
            high             REAL,
            low              REAL,
            close            REAL,
            adj_close        REAL,
            volume           INTEGER,
            benchmark_ticker TEXT NOT NULL DEFAULT 'SPY'
        );

        CREATE TABLE IF NOT EXISTS portfolio_holding (
            holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id   INTEGER NOT NULL,
            weight     REAL    NOT NULL,
            start_date TEXT    NOT NULL,
            FOREIGN KEY (stock_id) REFERENCES dim_stock(stock_id)
        );

        CREATE TABLE IF NOT EXISTS fact_return (
            return_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id     INTEGER NOT NULL,
            date         TEXT    NOT NULL,
            daily_return REAL,
            log_return   REAL,
            FOREIGN KEY (stock_id) REFERENCES dim_stock(stock_id),
            UNIQUE (stock_id, date)
        );

        CREATE TABLE IF NOT EXISTS fact_risk_metric (
            metric_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id           INTEGER NOT NULL,
            date               TEXT    NOT NULL,
            rolling_vol_20d    REAL,
            drawdown           REAL,
            rolling_avg_volume REAL,
            volume_spike_ratio REAL,
            FOREIGN KEY (stock_id) REFERENCES dim_stock(stock_id),
            UNIQUE (stock_id, date)
        );

        CREATE TABLE IF NOT EXISTS portfolio_daily_value (
            portfolio_date   TEXT PRIMARY KEY,
            portfolio_return REAL,
            portfolio_value  REAL,
            benchmark_return REAL
        );

        CREATE TABLE IF NOT EXISTS risk_alert (
            alert_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id    INTEGER,
            date        TEXT NOT NULL,
            alert_type  TEXT NOT NULL,
            alert_value REAL,
            threshold   REAL,
            severity    TEXT NOT NULL,
            FOREIGN KEY (stock_id) REFERENCES dim_stock(stock_id)
        );
    """)
    con.commit()
    log.info("Schema verified / created.")


def get_ticker_map(con: sqlite3.Connection) -> dict[str, int]:
    """
    Return {ticker: stock_id} for every row in dim_stock.
    Raises RuntimeError if the table is empty (Step 3 not run yet).
    """
    rows = con.execute("SELECT ticker, stock_id FROM dim_stock").fetchall()
    if not rows:
        raise RuntimeError(
            "dim_stock is empty. Run portfolio_universe.py (Step 3) first."
        )
    return {ticker: stock_id for ticker, stock_id in rows}


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def download_with_retry(
    tickers: list[str],
    start: str,
    end: str,
    attempt: int = 1,
) -> pd.DataFrame:
    """
    Download OHLCV data for a list of tickers via yfinance.
    Retries up to RETRY_LIMIT times on failure.

    yfinance returns a MultiIndex DataFrame when multiple tickers are passed:
        columns = (Price_field, Ticker)
    We stack it into a flat long-form DataFrame before returning.
    """
    try:
        raw = yf.download(
            tickers,
            start=start,
            end=end,
            auto_adjust=False,   # keep both Close and Adj Close
            progress=False,
            threads=True,
        )
    except Exception as exc:
        if attempt >= RETRY_LIMIT:
            log.error("Download failed after %d attempts: %s", RETRY_LIMIT, exc)
            raise
        log.warning("Download error (attempt %d/%d): %s — retrying in %ds",
                    attempt, RETRY_LIMIT, exc, RETRY_DELAY)
        time.sleep(RETRY_DELAY)
        return download_with_retry(tickers, start, end, attempt + 1)

    if raw.empty:
        log.warning("yfinance returned empty DataFrame for: %s", tickers)
        return pd.DataFrame()

    # ── Normalise MultiIndex → long-form ──────────────────────────────────
    # When a single ticker is passed, yfinance returns flat columns.
    # When multiple tickers are passed, it returns (field, ticker) MultiIndex.
    if isinstance(raw.columns, pd.MultiIndex):
        raw = raw.stack(level=1, future_stack=True).reset_index()
        raw.columns.name = None
        raw = raw.rename(columns={"level_1": "Ticker", "Date": "date"})
    else:
        # Single ticker — add Ticker column
        raw = raw.reset_index()
        raw["Ticker"] = tickers[0]

    return raw


def clean_price_df(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Standardise column names and types ready for DB insertion.

    Input columns (from yfinance):
        date, Ticker, Open, High, Low, Close, Adj Close, Volume

    Output columns:
        date (str YYYY-MM-DD), ticker (str), open, high, low,
        close, adj_close (float 4dp), volume (int)
    """
    # Rename to schema column names
    col_map = {
        "Date":      "date",
        "Ticker":    "ticker",
        "Open":      "open",
        "High":      "high",
        "Low":       "low",
        "Close":     "close",
        "Adj Close": "adj_close",
        "Volume":    "volume",
    }
    df = raw.rename(columns=col_map)

    # Keep only the columns we care about
    keep = ["date", "ticker", "open", "high", "low", "close", "adj_close", "volume"]
    df = df[[c for c in keep if c in df.columns]].copy()

    # Convert date to ISO string
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    # Drop rows where adj_close is missing (non-trading days or data gaps)
    before = len(df)
    df = df.dropna(subset=["adj_close"])
    dropped = before - len(df)
    if dropped:
        log.info("Dropped %d rows with missing adj_close.", dropped)

    # Round prices to 4 decimal places; volume to integer
    price_cols = ["open", "high", "low", "close", "adj_close"]
    for col in price_cols:
        if col in df.columns:
            df[col] = df[col].round(4)

    if "volume" in df.columns:
        df["volume"] = df["volume"].fillna(0).astype(int)

    # Remove any duplicate (ticker, date) pairs (shouldn't happen, but be safe)
    df = df.drop_duplicates(subset=["ticker", "date"])

    return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE WRITERS
# ─────────────────────────────────────────────────────────────────────────────

def write_fact_price(
    con: sqlite3.Connection,
    df: pd.DataFrame,
    ticker_map: dict[str, int],
) -> dict[str, int]:
    """
    Upsert rows from df into fact_price.
    INSERT OR REPLACE handles re-runs gracefully — existing rows are replaced.

    Returns {ticker: rows_written} summary dict.
    """
    summary: dict[str, int] = {}
    cur = con.cursor()

    sql = """
        INSERT OR REPLACE INTO fact_price
            (stock_id, date, open, high, low, close, adj_close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    for ticker, group in df.groupby("ticker"):
        stock_id = ticker_map.get(str(ticker))
        if stock_id is None:
            log.warning("Ticker '%s' not found in dim_stock — skipping.", ticker)
            continue

        rows = [
            (
                stock_id,
                row.date,
                row.open   if pd.notna(row.open)      else None,
                row.high   if pd.notna(row.high)      else None,
                row.low    if pd.notna(row.low)       else None,
                row.close  if pd.notna(row.close)     else None,
                row.adj_close,
                int(row.volume) if pd.notna(row.volume) else None,
            )
            for row in group.itertuples(index=False)
        ]

        cur.executemany(sql, rows)
        summary[str(ticker)] = len(rows)
        log.info("  %-6s → %d rows written to fact_price", ticker, len(rows))

    con.commit()
    return summary


def write_benchmark_price(
    con: sqlite3.Connection,
    df: pd.DataFrame,
    ticker: str = BENCHMARK,
) -> int:
    """Write SPY (or chosen benchmark) rows into benchmark_price table."""
    spy_df = df[df["ticker"] == ticker].copy()
    if spy_df.empty:
        log.warning("No benchmark data found for %s.", ticker)
        return 0

    sql = """
        INSERT OR REPLACE INTO benchmark_price
            (date, open, high, low, close, adj_close, volume, benchmark_ticker)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    rows = [
        (
            row.date,
            row.open      if pd.notna(row.open)      else None,
            row.high      if pd.notna(row.high)      else None,
            row.low       if pd.notna(row.low)       else None,
            row.close     if pd.notna(row.close)     else None,
            row.adj_close,
            int(row.volume) if pd.notna(row.volume) else None,
            ticker,
        )
        for row in spy_df.itertuples(index=False)
    ]

    con.executemany(sql, rows)
    con.commit()
    log.info("  %-6s → %d rows written to benchmark_price", ticker, len(rows))
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def validate_load(con: sqlite3.Connection, ticker_map: dict[str, int]) -> None:
    """
    Run lightweight sanity checks after loading:
    - Every ticker should have at least 200 rows (roughly 1 year of trading days)
    - No ticker should have fewer than 1 row (download failure)
    - Print a per-ticker row count table
    """
    log.info("")
    log.info("─" * 60)
    log.info("VALIDATION REPORT")
    log.info("─" * 60)
    log.info("%-8s  %8s  %12s  %12s", "Ticker", "Rows", "First Date", "Last Date")
    log.info("%-8s  %8s  %12s  %12s", "──────", "────", "──────────", "─────────")

    warnings = []
    for ticker, stock_id in sorted(ticker_map.items()):
        row = con.execute(
            """SELECT COUNT(*), MIN(date), MAX(date)
               FROM fact_price WHERE stock_id = ?""",
            (stock_id,)
        ).fetchone()
        count, first, last = row
        flag = ""
        if count == 0:
            flag = "  ← NO DATA"
            warnings.append(ticker)
        elif count < 200:
            flag = "  ← LOW"
            warnings.append(ticker)
        log.info("%-8s  %8d  %12s  %12s%s", ticker, count, first or "—", last or "—", flag)

    bench = con.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM benchmark_price"
    ).fetchone()
    log.info("%-8s  %8d  %12s  %12s  ← benchmark", BENCHMARK, bench[0], bench[1] or "—", bench[2] or "—")
    log.info("─" * 60)

    total_rows = con.execute("SELECT COUNT(*) FROM fact_price").fetchone()[0]
    log.info("Total rows in fact_price: %d", total_rows)

    if warnings:
        log.warning("Tickers with data issues: %s", ", ".join(warnings))
    else:
        log.info("All tickers passed validation.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def run_ingestion(
    db_path: str,
    start: str,
    end: str,
    tickers_override: Optional[list[str]] = None,
) -> None:
    """
    Full ingestion pipeline:
      1. Open DB and ensure schema
      2. Load ticker → stock_id map from dim_stock
      3. Download price data in batches
      4. Clean and standardise
      5. Write to fact_price and benchmark_price
      6. Validate
    """
    log.info("=" * 60)
    log.info("STEP 4 — PRICE INGESTION")
    log.info("  DB      : %s", db_path)
    log.info("  Period  : %s  →  %s", start, end)
    log.info("=" * 60)

    # ── 1. Open DB ────────────────────────────────────────────────
    con = get_connection(db_path)
    ensure_tables(con)

    # ── 2. Load ticker map ────────────────────────────────────────
    ticker_map = get_ticker_map(con)
    tickers = tickers_override or list(ticker_map.keys())
    all_tickers = tickers + [BENCHMARK]   # always include benchmark

    log.info("Universe: %d stocks + %s benchmark = %d total tickers",
             len(tickers), BENCHMARK, len(all_tickers))

    # ── 3. Download in batches ────────────────────────────────────
    # Batching avoids hitting Yahoo rate limits and makes retries cheaper
    all_dfs: list[pd.DataFrame] = []

    for i in range(0, len(all_tickers), BATCH_SIZE):
        batch = all_tickers[i : i + BATCH_SIZE]
        log.info("Downloading batch %d/%d: %s",
                 i // BATCH_SIZE + 1,
                 -(-len(all_tickers) // BATCH_SIZE),   # ceiling division
                 ", ".join(batch))

        raw = download_with_retry(batch, start, end)
        if not raw.empty:
            all_dfs.append(raw)

        time.sleep(1)   # be polite to Yahoo's servers

    if not all_dfs:
        log.error("No data downloaded. Check your internet connection.")
        sys.exit(1)

    # ── 4. Clean ──────────────────────────────────────────────────
    combined_raw = pd.concat(all_dfs, ignore_index=True)
    log.info("Raw rows downloaded : %d", len(combined_raw))

    df = clean_price_df(combined_raw)
    log.info("Clean rows to write : %d", len(df))

    # Show a quick preview of what we have
    if not df.empty:
        sample = df.groupby("ticker").agg(
            rows=("date", "count"),
            first=("date", "min"),
            last=("date",  "max"),
        )
        log.info("Preview (first 5 tickers):\n%s", sample.head().to_string())

    # ── 5. Write to DB ────────────────────────────────────────────
    log.info("")
    log.info("Writing to fact_price …")

    # Exclude benchmark from stock writes
    stock_df = df[df["ticker"] != BENCHMARK].copy()
    summary  = write_fact_price(con, stock_df, ticker_map)

    log.info("Writing to benchmark_price …")
    bench_rows = write_benchmark_price(con, df, BENCHMARK)

    # ── 6. Validate ───────────────────────────────────────────────
    validate_load(con, ticker_map)

    # ── Final summary ─────────────────────────────────────────────
    log.info("")
    log.info("INGESTION COMPLETE")
    log.info("  Stocks written   : %d", len(summary))
    log.info("  Stock rows total : %d", sum(summary.values()))
    log.info("  Benchmark rows   : %d", bench_rows)
    log.info("  Database         : %s", db_path)

    con.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 4 — Download price data and load into SQLite."
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"Path to SQLite database file (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_START,
        help=f"Start date YYYY-MM-DD (default: {DEFAULT_START})",
    )
    parser.add_argument(
        "--end",
        default=DEFAULT_END,
        help=f"End date YYYY-MM-DD (default: today = {DEFAULT_END})",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=None,
        help="Override tickers (space-separated). Default: all in dim_stock.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_ingestion(
        db_path=args.db,
        start=args.start,
        end=args.end,
        tickers_override=args.tickers,
    )
