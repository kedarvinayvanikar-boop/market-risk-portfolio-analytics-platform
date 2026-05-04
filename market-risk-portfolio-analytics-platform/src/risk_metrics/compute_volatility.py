"""
compute_volatility.py
=====================
Step 7 — Compute rolling 20-day annualised volatility and store in
fact_risk_metric alongside drawdown and volume metrics.

What this script does
---------------------
1. Loads log returns from fact_return (joined to dim_stock).
2. For each stock computes a 20-day rolling standard deviation of
   log returns, then annualises it by multiplying by sqrt(252).
3. Loads adj_close prices from fact_price and computes:
      - maximum drawdown from a 252-day rolling peak
      - 20-day rolling average volume
      - volume spike ratio = today_volume / rolling_avg_volume
4. Merges all four metrics into a single DataFrame keyed on
   (stock_id, date) and upserts into fact_risk_metric.
5. Prints a validation report and per-ticker summary.

Why log returns for volatility?
--------------------------------
Log returns are used (not simple returns) because:
  - they are approximately normally distributed
  - standard deviation of log returns is the input assumed by
    the Black-Scholes model and most risk frameworks
  - annualisation via sqrt(252) is only theoretically correct
    when returns are i.i.d. — log returns satisfy this better
    than simple returns

Why 20 days?
------------
20 trading days ≈ 1 calendar month.  Short enough to be responsive
to recent regime changes; long enough to average out single-day noise.
Common industry windows are 20d (short-term), 60d (medium), 252d (1yr).

Why sqrt(252)?
--------------
Under the assumption that daily log returns are i.i.d., variance
scales linearly with time:  Var(annual) = 252 × Var(daily)
Taking the square root:     Std(annual) = sqrt(252) × Std(daily)
252 is the approximate number of US equity trading days per year.

Usage
-----
    python compute_volatility.py
    python compute_volatility.py --db my.db
    python compute_volatility.py --window 20 --ann-factor 252
"""

import argparse
import logging
import sqlite3
import sys
import math

import numpy as np
import pandas as pd

DEFAULT_DB         = "stocks.db"
DEFAULT_WINDOW     = 20     # rolling window in trading days
DEFAULT_ANN_FACTOR = 252    # trading days per year for annualisation
PEAK_WINDOW        = 252    # lookback for rolling max (drawdown calc)
VOL_WINDOW         = 20     # volume rolling average window

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


def load_returns(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Pull log_return from fact_return, joined to dim_stock.
    Returns columns: stock_id, ticker, date, log_return
    Sorted by stock_id then date — required for rolling to work correctly.
    """
    df = pd.read_sql_query("""
        SELECT  r.stock_id,
                s.ticker,
                r.date,
                r.log_return
        FROM    fact_return r
        JOIN    dim_stock   s ON s.stock_id = r.stock_id
        WHERE   r.log_return IS NOT NULL
        ORDER   BY r.stock_id, r.date
    """, con)

    if df.empty:
        raise RuntimeError("fact_return is empty. Run compute_returns.py first.")

    log.info("Loaded %d return rows across %d tickers.",
             len(df), df["ticker"].nunique())
    return df


def load_prices(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Pull adj_close and volume from fact_price.
    Used for drawdown and volume spike calculations.
    Returns columns: stock_id, date, adj_close, volume
    """
    df = pd.read_sql_query("""
        SELECT  stock_id, date, adj_close, volume
        FROM    fact_price
        WHERE   adj_close IS NOT NULL
        ORDER   BY stock_id, date
    """, con)
    log.info("Loaded %d price rows for drawdown/volume calculation.", len(df))
    return df


# ─────────────────────────────────────────────────────────────────
# VOLATILITY
# ─────────────────────────────────────────────────────────────────

def compute_rolling_volatility(
    returns: pd.DataFrame,
    window: int,
    ann_factor: int,
) -> pd.DataFrame:
    """
    20-day rolling standard deviation of log returns, annualised.

    rolling(window, min_periods=window) means the first (window-1) rows
    of each stock will have NaN volatility — there are not enough prior
    returns to fill the window.  This is correct behaviour: we do not
    want to report a 'volatility' based on 3 days of data.

    ddof=1 uses the sample standard deviation (divides by N-1 instead
    of N) — the statistically correct choice when estimating population
    variance from a sample.
    """
    ann_scale = math.sqrt(ann_factor)

    vol = (
        returns
        .groupby("stock_id")["log_return"]
        .transform(lambda s: s.rolling(window=window, min_periods=window).std(ddof=1))
        * ann_scale
    )

    result = returns[["stock_id", "date"]].copy()
    result["rolling_vol_20d"] = vol.round(6)

    log.info("Computed %d volatility values (%d NaN from warm-up window).",
             result["rolling_vol_20d"].notna().sum(),
             result["rolling_vol_20d"].isna().sum())
    return result


# ─────────────────────────────────────────────────────────────────
# DRAWDOWN
# ─────────────────────────────────────────────────────────────────

def compute_drawdown(prices: pd.DataFrame, peak_window: int) -> pd.DataFrame:
    """
    Rolling drawdown from a 252-day (1-year) rolling peak.

    drawdown_t = (price_t - rolling_max_t) / rolling_max_t

    Always ≤ 0:
      - 0.0 means price is at a new 252-day high
      - -0.20 means price is 20% below its 252-day peak

    min_periods=1 is intentional for the rolling max so that even the
    first trading day has a drawdown value (which will be 0.0 by
    definition, since it equals its own peak).
    """
    grp = prices.groupby("stock_id")["adj_close"]

    rolling_max = grp.transform(
        lambda s: s.rolling(window=peak_window, min_periods=1).max()
    )

    drawdown = (prices["adj_close"] - rolling_max) / rolling_max

    result = prices[["stock_id", "date"]].copy()
    result["drawdown"] = drawdown.round(6)
    return result


# ─────────────────────────────────────────────────────────────────
# VOLUME METRICS
# ─────────────────────────────────────────────────────────────────

def compute_volume_metrics(prices: pd.DataFrame, vol_window: int) -> pd.DataFrame:
    """
    20-day rolling average volume and today's spike ratio.

    rolling_avg_volume: 20-day simple moving average of daily volume.
    volume_spike_ratio: today_volume / 20d_avg_volume.
      - ratio = 1.0 → normal day
      - ratio = 2.5 → 2.5× normal volume → potential alert trigger

    shift(1) in the rolling average uses only past volume (not today's)
    to avoid look-ahead bias in the ratio.  We want to answer: "is today
    unusual compared to recent history?" — that requires yesterday's avg.
    """
    grp = prices.groupby("stock_id")["volume"]

    # rolling avg uses past vol (shift 1 to avoid including today)
    rolling_avg = grp.transform(
        lambda s: s.shift(1).rolling(window=vol_window, min_periods=vol_window).mean()
    )

    spike_ratio = prices["volume"] / rolling_avg

    result = prices[["stock_id", "date"]].copy()
    result["rolling_avg_volume"]  = rolling_avg.round(0)
    result["volume_spike_ratio"]  = spike_ratio.round(4)
    return result


# ─────────────────────────────────────────────────────────────────
# MERGE & WRITE
# ─────────────────────────────────────────────────────────────────

def merge_metrics(
    vol_df:    pd.DataFrame,
    dd_df:     pd.DataFrame,
    volume_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Left-join all three metric DataFrames on (stock_id, date).
    The spine is the volatility frame — same (stock_id, date) pairs
    that exist in fact_return.
    """
    df = vol_df.merge(dd_df,     on=["stock_id", "date"], how="left")
    df = df.merge(volume_df,     on=["stock_id", "date"], how="left")
    return df


def write_metrics(con: sqlite3.Connection, df: pd.DataFrame) -> int:
    """
    Upsert rows into fact_risk_metric.
    Rows where ALL metric columns are NaN (warm-up rows) are included —
    they serve as placeholders so the (stock_id, date) pair exists in
    the table, making future joins predictable.
    """
    sql = """
        INSERT OR REPLACE INTO fact_risk_metric
            (stock_id, date,
             rolling_vol_20d, drawdown,
             rolling_avg_volume, volume_spike_ratio)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    rows = [
        (
            int(r.stock_id),
            r.date,
            float(r.rolling_vol_20d)    if pd.notna(r.rolling_vol_20d)    else None,
            float(r.drawdown)           if pd.notna(r.drawdown)            else None,
            float(r.rolling_avg_volume) if pd.notna(r.rolling_avg_volume)  else None,
            float(r.volume_spike_ratio) if pd.notna(r.volume_spike_ratio)  else None,
        )
        for r in df.itertuples(index=False)
    ]
    con.execute("BEGIN")
    con.executemany(sql, rows)
    con.commit()
    log.info("Written %d rows to fact_risk_metric.", len(rows))
    return len(rows)


# ─────────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────────

def validate(con: sqlite3.Connection, window: int) -> None:
    log.info("")
    log.info("─" * 76)
    log.info("VOLATILITY VALIDATION REPORT")
    log.info("─" * 76)
    log.info("%-6s  %6s  %6s  %8s  %8s  %8s  %8s",
             "Ticker", "Rows", "NaN Vol", "Min Vol", "Max Vol",
             "Min DD", "Max Spike")
    log.info("%-6s  %6s  %6s  %8s  %8s  %8s  %8s",
             "──────","──────","───────","───────","───────","──────","─────────")

    rows = con.execute("""
        SELECT  s.ticker,
                COUNT(*)                                   AS rows,
                SUM(CASE WHEN m.rolling_vol_20d IS NULL
                         THEN 1 ELSE 0 END)                AS nan_vol,
                ROUND(MIN(m.rolling_vol_20d), 4)           AS min_vol,
                ROUND(MAX(m.rolling_vol_20d), 4)           AS max_vol,
                ROUND(MIN(m.drawdown),        4)           AS min_dd,
                ROUND(MAX(m.volume_spike_ratio), 2)        AS max_spike
        FROM    fact_risk_metric m
        JOIN    dim_stock        s ON s.stock_id = m.stock_id
        GROUP   BY m.stock_id
        ORDER   BY s.ticker
    """).fetchall()

    for ticker, n, nan_vol, mn_v, mx_v, mn_dd, mx_spike in rows:
        nan_pct = f"{nan_vol/n:.0%}" if n else "—"
        log.info("%-6s  %6d  %5s%%  %8.4f  %8.4f  %8.4f  %8.2f",
                 ticker, n, nan_pct.strip("%"),
                 mn_v or 0, mx_v or 0, mn_dd or 0, mx_spike or 0)

    total = con.execute("SELECT COUNT(*) FROM fact_risk_metric").fetchone()[0]
    non_null_vol = con.execute(
        "SELECT COUNT(*) FROM fact_risk_metric WHERE rolling_vol_20d IS NOT NULL"
    ).fetchone()[0]
    log.info("─" * 76)
    log.info("Total rows        : %d", total)
    log.info("Non-null vol rows : %d  (first %d days of each ticker are NaN by design)",
             non_null_vol, window - 1)

    # Sanity: expected NaN count = (window-1) * n_tickers
    n_tickers = con.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
    expected_nan = (window - 1) * n_tickers
    actual_nan   = total - non_null_vol
    if abs(actual_nan - expected_nan) <= n_tickers:
        log.info("NaN count check   : PASS  (actual=%d, expected≈%d)", actual_nan, expected_nan)
    else:
        log.warning("NaN count check   : MISMATCH  (actual=%d, expected≈%d)",
                    actual_nan, expected_nan)
    log.info("─" * 76)


def print_sample(con: sqlite3.Connection) -> None:
    """Show the most recent 5 metric rows for AAPL."""
    rows = con.execute("""
        SELECT  m.date,
                ROUND(m.rolling_vol_20d    * 100, 2) AS vol_pct,
                ROUND(m.drawdown           * 100, 2) AS drawdown_pct,
                ROUND(m.rolling_avg_volume / 1e6, 1) AS avg_vol_M,
                ROUND(m.volume_spike_ratio,         2) AS spike_ratio
        FROM    fact_risk_metric m
        JOIN    dim_stock        s ON s.stock_id = m.stock_id
        WHERE   s.ticker = 'AAPL'
          AND   m.rolling_vol_20d IS NOT NULL
        ORDER   BY m.date DESC
        LIMIT   5
    """).fetchall()

    log.info("")
    log.info("Sample — AAPL last 5 metric rows:")
    log.info("  %-12s  %8s  %12s  %10s  %10s",
             "date", "vol(%)", "drawdown(%)", "avgVol(M)", "spikeRatio")
    log.info("  %-12s  %8s  %12s  %10s  %10s",
             "──────────","──────","──────────","─────────","──────────")
    for date, vol, dd, avgv, spike in rows:
        log.info("  %-12s  %8.2f  %12.2f  %10.1f  %10.2f",
                 date, vol or 0, dd or 0, avgv or 0, spike or 0)


# ─────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────

def run(db_path: str, window: int, ann_factor: int) -> None:
    log.info("=" * 60)
    log.info("STEP 7 — COMPUTE ROLLING VOLATILITY & RISK METRICS")
    log.info("  DB         : %s", db_path)
    log.info("  Vol window : %d trading days", window)
    log.info("  Ann factor : %d  (sqrt = %.4f)", ann_factor, math.sqrt(ann_factor))
    log.info("=" * 60)

    con = get_connection(db_path)

    # 1. Load inputs
    returns = load_returns(con)
    prices  = load_prices(con)

    # 2. Compute each metric independently
    vol_df    = compute_rolling_volatility(returns, window, ann_factor)
    dd_df     = compute_drawdown(prices, PEAK_WINDOW)
    volume_df = compute_volume_metrics(prices, VOL_WINDOW)

    # 3. Merge on (stock_id, date)
    merged = merge_metrics(vol_df, dd_df, volume_df)
    log.info("Merged metric frame: %d rows, %d columns.", *merged.shape)

    # 4. Write
    write_metrics(con, merged)

    # 5. Validate
    validate(con, window)
    print_sample(con)

    log.info("")
    log.info("STEP 7 COMPLETE — fact_risk_metric is populated.")
    con.close()


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Step 7 — Compute rolling volatility and risk metrics."
    )
    p.add_argument("--db",         default=DEFAULT_DB,         help="SQLite DB path")
    p.add_argument("--window",     default=DEFAULT_WINDOW,     type=int,
                   help=f"Rolling window in trading days (default {DEFAULT_WINDOW})")
    p.add_argument("--ann-factor", default=DEFAULT_ANN_FACTOR, type=int,
                   dest="ann_factor",
                   help=f"Trading days per year for annualisation (default {DEFAULT_ANN_FACTOR})")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.db, args.window, args.ann_factor)
