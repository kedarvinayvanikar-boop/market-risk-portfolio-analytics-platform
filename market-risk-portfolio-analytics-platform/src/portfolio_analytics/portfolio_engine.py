"""
portfolio_engine.py
===================
Step 11 — Compute daily portfolio returns, cumulative portfolio value,
and benchmark comparison, then write to portfolio_daily_value.

What this script does
---------------------
1. Loads portfolio weights from portfolio_holding (one weight per stock).
2. Loads daily simple returns from fact_return for every held stock.
3. For each trading date, computes the weighted sum of stock returns:
       Rp,t = sum(wi * Ri,t)
4. Computes cumulative portfolio value starting at 100:
       V0 = 100
       Vt = V_{t-1} * (1 + Rp,t)
5. Loads SPY daily returns from benchmark_price and computes the same
   cumulative series for comparison.
6. Writes (date, portfolio_return, portfolio_value, benchmark_return)
   to portfolio_daily_value using INSERT OR REPLACE (idempotent).
7. Prints a performance summary: total return, CAGR, Sharpe, max drawdown.

Key finance concepts
---------------------
Weighted sum:
    Each stock contributes weight_i * return_i to the portfolio's return.
    A stock with 6% weight has 3x the return impact of one with 2%.

Compounding:
    Value grows multiplicatively, not additively.
    V10 = 100 * (1+R1) * (1+R2) * ... * (1+R10)
    This is identical to cumprod(1 + Rp) * 100.

Missing returns (NaN handling):
    If a stock has no return for a given date (e.g. it was suspended),
    its contribution is treated as 0 (weight * NaN = 0) to avoid
    propagating NaN through the entire portfolio series.

Usage
-----
    python portfolio_engine.py
    python portfolio_engine.py --db my.db
    python portfolio_engine.py --initial-value 1000000
    python portfolio_engine.py --benchmark-col adj_close
"""

import argparse
import logging
import math
import sqlite3
import sys

import numpy as np
import pandas as pd

DEFAULT_DB            = "stocks.db"
DEFAULT_INITIAL_VALUE = 100.0    # index base; 100 is standard
ANN_FACTOR            = 252      # trading days per year

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


def load_weights(con: sqlite3.Connection) -> pd.Series:
    """
    Load portfolio weights from portfolio_holding.
    Returns a Series indexed by stock_id with weight values.

    Validates that weights sum to 1.0 (within floating-point tolerance).
    """
    df = pd.read_sql_query("""
        SELECT  stock_id, weight
        FROM    portfolio_holding
        ORDER   BY stock_id
    """, con)

    if df.empty:
        raise RuntimeError("portfolio_holding is empty. Run portfolio_universe.py first.")

    weights = df.set_index("stock_id")["weight"]
    total = weights.sum()

    log.info("Loaded %d holdings. Weight sum = %.6f", len(weights), total)
    if abs(total - 1.0) > 1e-4:
        log.warning("Weights sum to %.6f (expected 1.0) — returns will be scaled.", total)

    return weights


def load_returns(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Load simple daily returns from fact_return for all held stocks.
    Returns a wide DataFrame: rows = dates, columns = stock_id,
    values = daily_return.

    pivot_table fills missing (stock_id, date) combinations with NaN.
    """
    df = pd.read_sql_query("""
        SELECT  r.stock_id,
                r.date,
                r.daily_return
        FROM    fact_return     r
        JOIN    portfolio_holding ph ON ph.stock_id = r.stock_id
        ORDER   BY r.date, r.stock_id
    """, con)

    if df.empty:
        raise RuntimeError("fact_return is empty. Run compute_returns.py first.")

    # Pivot: rows = dates, columns = stock_ids, values = daily_return
    wide = df.pivot_table(
        index="date",
        columns="stock_id",
        values="daily_return",
        aggfunc="first",      # no duplicates, but be explicit
    )

    log.info("Return matrix: %d dates × %d stocks.", *wide.shape)
    return wide


def load_benchmark(con: sqlite3.Connection) -> pd.Series:
    """
    Load SPY daily returns from benchmark_price.
    Computes simple return from adj_close.
    Returns a Series indexed by date.
    """
    df = pd.read_sql_query("""
        SELECT date, adj_close
        FROM   benchmark_price
        ORDER  BY date
    """, con)

    if df.empty:
        log.warning("benchmark_price is empty — benchmark column will be NULL.")
        return pd.Series(dtype=float)

    df["benchmark_return"] = df["adj_close"].pct_change()
    df = df.dropna(subset=["benchmark_return"])
    bench = df.set_index("date")["benchmark_return"]
    log.info("Loaded %d benchmark (SPY) return rows.", len(bench))
    return bench


# ─────────────────────────────────────────────────────────────────
# PORTFOLIO COMPUTATION
# ─────────────────────────────────────────────────────────────────

def compute_portfolio_returns(
    returns_wide: pd.DataFrame,
    weights: pd.Series,
    initial_value: float,
) -> pd.DataFrame:
    """
    Compute daily weighted portfolio return and cumulative value.

    Step A — align weights to return columns
        weights may not cover every stock_id in returns_wide if portfolio
        was changed, or may include stocks with no return rows.
        We intersect to be safe.

    Step B — weighted sum per date
        Rp,t = sum(wi * Ri,t)
        NaN returns are filled with 0 before multiplication so one
        missing stock doesn't null out the entire portfolio day.
        fillna(0) means we treat a suspended stock as flat (0% return)
        for that day — conservative and transparent.

    Step C — cumulative value via cumprod
        Vt = V0 * prod(1 + Rp,t)
        pandas cumprod() is the direct implementation of this chain.
    """
    # Step A: align weights
    common_stocks = returns_wide.columns.intersection(weights.index)
    missing_from_returns = weights.index.difference(returns_wide.columns)
    if not missing_from_returns.empty:
        log.warning(
            "Stock IDs in portfolio_holding but not in fact_return: %s",
            list(missing_from_returns)
        )

    w = weights.loc[common_stocks]               # aligned weight vector
    R = returns_wide[common_stocks].fillna(0.0)  # NaN → 0 (flat return)

    # Step B: weighted sum — matrix multiply returns (N_dates × N_stocks)
    # by weights vector (N_stocks,) → daily portfolio return (N_dates,)
    #
    # R.values  shape: (N_dates, N_stocks)
    # w.values  shape: (N_stocks,)
    # dot product: each row of R · w → scalar portfolio return for that date
    portfolio_return = R.values @ w.values       # numpy dot, vectorised
    portfolio_return = pd.Series(portfolio_return, index=R.index, name="portfolio_return")

    # Step C: cumulative value
    # (1 + R1) * (1 + R2) * ... starting at initial_value
    cumulative_value = (1 + portfolio_return).cumprod() * initial_value
    cumulative_value.name = "portfolio_value"

    result = pd.DataFrame({
        "portfolio_return": portfolio_return.round(8),
        "portfolio_value":  cumulative_value.round(4),
    })

    log.info(
        "Portfolio return: %d days computed. "
        "Start=%.2f, End=%.2f, Total return=%.2f%%.",
        len(result),
        initial_value,
        result["portfolio_value"].iloc[-1],
        (result["portfolio_value"].iloc[-1] / initial_value - 1) * 100,
    )
    return result


def merge_benchmark(
    portfolio_df: pd.DataFrame,
    bench_series: pd.Series,
    initial_value: float,
) -> pd.DataFrame:
    """
    Join benchmark returns to portfolio DataFrame on date.
    Dates only in the portfolio (no SPY data) get NaN benchmark_return.
    """
    df = portfolio_df.copy()
    df = df.join(bench_series.rename("benchmark_return"), how="left")
    df["benchmark_return"] = df["benchmark_return"].round(8)

    # Compute benchmark cumulative value for the summary report (not stored)
    bench_cum = (1 + df["benchmark_return"].fillna(0)).cumprod() * initial_value
    spy_total = (bench_cum.iloc[-1] / initial_value - 1) * 100
    log.info("Benchmark (SPY) total return over same period: %.2f%%", spy_total)

    return df


# ─────────────────────────────────────────────────────────────────
# DATABASE WRITE
# ─────────────────────────────────────────────────────────────────

def write_portfolio(con: sqlite3.Connection, df: pd.DataFrame) -> int:
    """
    Upsert rows to portfolio_daily_value.
    portfolio_date is the PRIMARY KEY, so INSERT OR REPLACE updates
    existing rows and inserts new ones — safe to re-run.
    """
    sql = """
        INSERT OR REPLACE INTO portfolio_daily_value
            (portfolio_date, portfolio_return, portfolio_value, benchmark_return)
        VALUES (?, ?, ?, ?)
    """
    rows = [
        (
            idx,
            float(row.portfolio_return),
            float(row.portfolio_value),
            float(row.benchmark_return) if pd.notna(row.benchmark_return) else None,
        )
        for idx, row in df.iterrows()
    ]

    con.execute("BEGIN")
    con.executemany(sql, rows)
    con.commit()
    log.info("Written %d rows to portfolio_daily_value.", len(rows))
    return len(rows)


# ─────────────────────────────────────────────────────────────────
# PERFORMANCE SUMMARY
# ─────────────────────────────────────────────────────────────────

def performance_summary(
    df: pd.DataFrame,
    initial_value: float,
) -> None:
    """
    Print a performance tear-sheet comparing portfolio vs benchmark.
    Metrics:
        Total return, CAGR, Annualised volatility, Sharpe ratio,
        Maximum drawdown, Calmar ratio (CAGR / |max drawdown|)
    """
    n_days = len(df)
    n_years = n_days / ANN_FACTOR

    # ── Portfolio metrics ─────────────────────────────────────────
    port_total   = df["portfolio_value"].iloc[-1] / initial_value - 1
    port_cagr    = (1 + port_total) ** (1 / n_years) - 1
    port_vol     = df["portfolio_return"].std(ddof=1) * math.sqrt(ANN_FACTOR)
    port_sharpe  = port_cagr / port_vol if port_vol > 0 else 0.0

    # Max drawdown on cumulative value
    roll_max     = df["portfolio_value"].cummax()
    port_dd      = ((df["portfolio_value"] - roll_max) / roll_max).min()
    port_calmar  = port_cagr / abs(port_dd) if port_dd != 0 else float("inf")

    # ── Benchmark metrics ─────────────────────────────────────────
    bench_valid  = df["benchmark_return"].dropna()
    bench_total  = (1 + bench_valid).prod() - 1
    bench_cagr   = (1 + bench_total) ** (1 / n_years) - 1
    bench_vol    = bench_valid.std(ddof=1) * math.sqrt(ANN_FACTOR)
    bench_sharpe = bench_cagr / bench_vol if bench_vol > 0 else 0.0

    bench_cum    = (1 + df["benchmark_return"].fillna(0)).cumprod() * initial_value
    bench_roll   = bench_cum.cummax()
    bench_dd     = ((bench_cum - bench_roll) / bench_roll).min()

    # ── Print ─────────────────────────────────────────────────────
    log.info("")
    log.info("═" * 58)
    log.info("PERFORMANCE TEAR-SHEET")
    log.info("═" * 58)
    log.info("%-24s  %12s  %12s", "Metric", "Portfolio", "SPY Benchmark")
    log.info("%-24s  %12s  %12s", "──────────────────────","──────────","─────────────")
    log.info("%-24s  %11.2f%%  %11.2f%%", "Total Return",
             port_total*100, bench_total*100)
    log.info("%-24s  %11.2f%%  %11.2f%%", "CAGR",
             port_cagr*100, bench_cagr*100)
    log.info("%-24s  %11.2f%%  %11.2f%%", "Annualised Volatility",
             port_vol*100, bench_vol*100)
    log.info("%-24s  %12.3f  %12.3f", "Sharpe Ratio",
             port_sharpe, bench_sharpe)
    log.info("%-24s  %11.2f%%  %11.2f%%", "Maximum Drawdown",
             port_dd*100, bench_dd*100)
    log.info("%-24s  %12.3f  %12s", "Calmar Ratio",
             port_calmar, "—")
    log.info("%-24s  %12d  %12d", "Trading Days",
             n_days, len(bench_valid))
    log.info("═" * 58)

    # Active return vs benchmark
    active = port_total - bench_total
    log.info("Active return vs SPY: %+.2f%%", active * 100)
    if active > 0:
        log.info("Portfolio OUTPERFORMED SPY by %.2f%%", active * 100)
    else:
        log.info("Portfolio UNDERPERFORMED SPY by %.2f%%", abs(active) * 100)
    log.info("─" * 58)


def print_sample(con: sqlite3.Connection) -> None:
    rows = con.execute("""
        SELECT  portfolio_date,
                ROUND(portfolio_return * 100, 4)  AS ret_pct,
                ROUND(portfolio_value,        2)  AS value,
                ROUND(benchmark_return * 100, 4)  AS spy_pct
        FROM    portfolio_daily_value
        ORDER   BY portfolio_date DESC
        LIMIT   5
    """).fetchall()

    log.info("")
    log.info("Most recent 5 rows in portfolio_daily_value:")
    log.info("  %-12s  %10s  %10s  %10s",
             "Date", "Port Ret%", "Value", "SPY Ret%")
    log.info("  %-12s  %10s  %10s  %10s",
             "──────────","─────────","──────────","─────────")
    for date, ret, val, spy in rows:
        log.info("  %-12s  %10.4f  %10.2f  %10.4f",
                 date, ret or 0, val or 0, spy or 0)


# ─────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────

def run(db_path: str, initial_value: float) -> None:
    log.info("=" * 60)
    log.info("STEP 11 — PORTFOLIO RETURN ENGINE")
    log.info("  DB            : %s", db_path)
    log.info("  Initial value : %.2f", initial_value)
    log.info("=" * 60)

    con = get_connection(db_path)

    # 1. Load inputs
    weights      = load_weights(con)
    returns_wide = load_returns(con)
    bench_series = load_benchmark(con)

    # 2. Compute portfolio returns and cumulative value
    portfolio_df = compute_portfolio_returns(returns_wide, weights, initial_value)

    # 3. Attach benchmark returns
    portfolio_df = merge_benchmark(portfolio_df, bench_series, initial_value)

    # 4. Write to DB
    write_portfolio(con, portfolio_df)

    # 5. Performance summary
    performance_summary(portfolio_df, initial_value)

    # 6. Sample output
    print_sample(con)

    log.info("")
    log.info("STEP 11 COMPLETE — portfolio_daily_value is populated.")
    con.close()


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step 11 — Portfolio return engine.")
    p.add_argument("--db",            default=DEFAULT_DB,
                   help="SQLite DB path")
    p.add_argument("--initial-value", default=DEFAULT_INITIAL_VALUE,
                   type=float, dest="initial_value",
                   help=f"Starting index value (default {DEFAULT_INITIAL_VALUE})")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.db, args.initial_value)
