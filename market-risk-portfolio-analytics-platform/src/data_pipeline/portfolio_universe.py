"""
portfolio_universe.py
=====================
Step 3 — Seed the stock universe, sectors, and portfolio weights.

What this script does
---------------------
1. Reads universe.csv, which contains tickers, company names, sectors, exchanges,
   and target portfolio weights.
2. Creates/verifies the core SQLite schema needed by later scripts.
3. Inserts sectors into dim_sector.
4. Inserts stocks into dim_stock and links each stock to a sector.
5. Inserts portfolio weights into portfolio_holding.
6. Validates that portfolio weights sum to 100%.

Usage
-----
    python3 portfolio_universe.py
    python3 portfolio_universe.py --db stocks.db --csv universe.csv

This script is idempotent: you can run it more than once. It will refresh the
portfolio_holding table and update stock/sector metadata safely.
"""

import argparse
import datetime as dt
import logging
import sqlite3
from pathlib import Path

import pandas as pd

DEFAULT_DB = "stocks.db"
DEFAULT_CSV = "universe.csv"
DEFAULT_START_DATE = (dt.date.today() - dt.timedelta(days=730)).isoformat()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def get_connection(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def ensure_tables(con: sqlite3.Connection) -> None:
    """Create the tables used across Steps 3–17 if they do not already exist."""
    con.executescript(
        """
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

        CREATE TABLE IF NOT EXISTS contribution_log (
            contribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id        INTEGER NOT NULL,
            date            TEXT    NOT NULL,
            weight          REAL    NOT NULL,
            daily_return    REAL,
            contribution    REAL,
            FOREIGN KEY (stock_id) REFERENCES dim_stock(stock_id),
            UNIQUE (stock_id, date)
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

        CREATE INDEX IF NOT EXISTS idx_fact_price_stock_date
            ON fact_price(stock_id, date);
        CREATE INDEX IF NOT EXISTS idx_fact_return_stock_date
            ON fact_return(stock_id, date);
        CREATE INDEX IF NOT EXISTS idx_fact_risk_metric_stock_date
            ON fact_risk_metric(stock_id, date);
        CREATE INDEX IF NOT EXISTS idx_risk_alert_stock_date
            ON risk_alert(stock_id, date);
        """
    )
    con.commit()


def load_universe(csv_path: str) -> pd.DataFrame:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Could not find {csv_path}. Make sure universe.csv is in this folder.")

    df = pd.read_csv(path)
    required = {"ticker", "company_name", "sector", "weight_pct"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"universe.csv is missing required columns: {sorted(missing)}")

    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["company_name"] = df["company_name"].astype(str).str.strip()
    df["sector"] = df["sector"].astype(str).str.strip()
    df["weight_pct"] = pd.to_numeric(df["weight_pct"], errors="raise")

    if df["ticker"].duplicated().any():
        dupes = df.loc[df["ticker"].duplicated(), "ticker"].tolist()
        raise ValueError(f"Duplicate tickers in universe.csv: {dupes}")

    total = float(df["weight_pct"].sum())
    if abs(total - 100.0) > 1e-6:
        raise ValueError(f"Portfolio weights must sum to 100. Current sum: {total:.4f}")

    df["weight"] = df["weight_pct"] / 100.0
    return df


def seed_database(con: sqlite3.Connection, universe: pd.DataFrame, start_date: str) -> None:
    """Insert sectors, stocks, and portfolio holdings."""
    # Sectors
    for sector in sorted(universe["sector"].unique()):
        con.execute(
            "INSERT OR IGNORE INTO dim_sector (sector_name) VALUES (?)",
            (sector,),
        )

    sector_map = dict(con.execute("SELECT sector_name, sector_id FROM dim_sector").fetchall())

    # Stocks
    for row in universe.itertuples(index=False):
        sector_id = sector_map[row.sector]
        con.execute(
            """
            INSERT INTO dim_stock (ticker, company_name, sector_id)
            VALUES (?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                company_name = excluded.company_name,
                sector_id = excluded.sector_id
            """,
            (row.ticker, row.company_name, sector_id),
        )

    stock_map = dict(con.execute("SELECT ticker, stock_id FROM dim_stock").fetchall())

    # Refresh holdings because this is the user's current simulated portfolio.
    con.execute("DELETE FROM portfolio_holding")
    for row in universe.itertuples(index=False):
        con.execute(
            """
            INSERT INTO portfolio_holding (stock_id, weight, start_date)
            VALUES (?, ?, ?)
            """,
            (stock_map[row.ticker], float(row.weight), start_date),
        )

    con.commit()


def print_summary(con: sqlite3.Connection) -> None:
    sectors = con.execute("SELECT COUNT(*) FROM dim_sector").fetchone()[0]
    stocks = con.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
    holdings = con.execute("SELECT COUNT(*) FROM portfolio_holding").fetchone()[0]
    weight_sum = con.execute("SELECT COALESCE(SUM(weight), 0) FROM portfolio_holding").fetchone()[0]

    log.info("Seeded %d sectors, %d stocks, and %d holdings.", sectors, stocks, holdings)
    log.info("Portfolio weight sum = %.6f", weight_sum)

    rows = con.execute(
        """
        SELECT ds.sector_name,
               COUNT(*) AS stock_count,
               ROUND(SUM(ph.weight) * 100, 2) AS weight_pct
        FROM portfolio_holding ph
        JOIN dim_stock st ON st.stock_id = ph.stock_id
        JOIN dim_sector ds ON ds.sector_id = st.sector_id
        GROUP BY ds.sector_name
        ORDER BY weight_pct DESC
        """
    ).fetchall()

    print("\nSector weight summary:")
    for sector, count, weight_pct in rows:
        print(f"  {sector:<15} {count:>2} stocks   {weight_pct:>6.2f}%")


def run(db_path: str, csv_path: str, start_date: str) -> None:
    log.info("=" * 60)
    log.info("STEP 3 — PORTFOLIO UNIVERSE SETUP")
    log.info("  DB   : %s", db_path)
    log.info("  CSV  : %s", csv_path)
    log.info("=" * 60)

    universe = load_universe(csv_path)
    with get_connection(db_path) as con:
        ensure_tables(con)
        seed_database(con, universe, start_date)
        print_summary(con)

    log.info("Step 3 completed successfully. Next run: python3 ingest_prices.py")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed stock universe and portfolio holdings.")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--csv", default=DEFAULT_CSV, help="Path to universe.csv")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Portfolio holding start date")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.db, args.csv, args.start_date)
