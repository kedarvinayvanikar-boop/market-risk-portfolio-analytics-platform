"""
compute_volume_spike.py
=======================
Step 9 — Compute rolling average volume and volume spike ratio,
then update fact_risk_metric.

Relationship to Step 7
-----------------------
compute_volatility.py (Step 7) already writes rolling_avg_volume and
volume_spike_ratio to fact_risk_metric alongside volatility.  This
script is the standalone, focused version that can:
  - re-run ONLY the two volume columns without touching vol/drawdown
  - switch the rolling window (default 20d, configurable)
  - produce a richer volume diagnostic report
  - filter to just the highest-spike events for alert inspection

Formula
-------
    rolling_avg_volume_t  =  mean( volume_{t-20}, ..., volume_{t-1} )
    volume_spike_ratio_t  =  volume_t  /  rolling_avg_volume_t

Shift-by-1 before rolling:
    The rolling average uses the 20 days BEFORE today.  Using today's
    own volume in the denominator would dampen extreme spikes (a 5x
    day would look like a 3x day if it contributed to its own average).
    shift(1) removes this look-ahead bias.

Interpretation
--------------
    ratio < 0.5   -> unusually quiet (below-normal participation)
    ratio ~ 1.0   -> normal trading activity
    ratio > 2.0   -> elevated volume; watch list
    ratio > 3.0   -> significant spike; alert threshold
    ratio > 5.0   -> major event (earnings, M&A, index rebalance)

Usage
-----
    python compute_volume_spike.py
    python compute_volume_spike.py --window 20
    python compute_volume_spike.py --db my.db --top 20
    python compute_volume_spike.py --report   # diagnostic only, no DB write
"""

import argparse
import logging
import sqlite3
import sys

import pandas as pd

DEFAULT_DB     = "stocks.db"
DEFAULT_WINDOW = 20
DEFAULT_TOP    = 10   # top-N spikes to show in report

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


def load_volume_data(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Pull date, volume (and adj_close for context) from fact_price.
    Returns: stock_id, ticker, date, volume, adj_close
    Sorted by stock_id then date — essential for shift + rolling.
    """
    df = pd.read_sql_query("""
        SELECT  fp.stock_id,
                s.ticker,
                fp.date,
                fp.volume,
                fp.adj_close
        FROM    fact_price fp
        JOIN    dim_stock  s  ON s.stock_id = fp.stock_id
        WHERE   fp.volume IS NOT NULL
        ORDER   BY fp.stock_id, fp.date
    """, con)

    if df.empty:
        raise RuntimeError("fact_price is empty. Run ingest_prices.py first.")

    log.info("Loaded %d volume rows across %d tickers.",
             len(df), df["ticker"].nunique())
    return df


# ─────────────────────────────────────────────────────────────────
# COMPUTATION
# ─────────────────────────────────────────────────────────────────

def compute_volume_metrics(df: pd.DataFrame, window: int) -> pd.DataFrame:
    """
    Compute rolling average volume and spike ratio.

    Step-by-step:
    1. shift(1): move yesterday's volume into today's row position.
       This creates a series where position t holds volume_{t-1}.
    2. rolling(window).mean(): compute the mean over the past `window`
       days, which now (after shift) covers t-window to t-1.
       min_periods=window: return NaN until a full window is available.
    3. Divide today's raw volume by the rolling average.

    Why shift BEFORE rolling?
    -------------------------
    Without shift:
        avg at day t = mean(v_{t-19}, ..., v_t)  [includes today]
        spike_t = v_t / avg_t
        If v_t is a 5x spike, avg_t rises, making spike look ~3x.
        Look-ahead bias: today's abnormal value contaminates its own benchmark.

    With shift(1):
        avg at day t = mean(v_{t-20}, ..., v_{t-1})  [excludes today]
        spike_t = v_t / avg_t
        The benchmark is purely historical. A 5x spike shows as 5x.
    """
    grp = df.groupby("stock_id")["volume"]

    # Rolling average of PAST volume (shifted by 1 day)
    rolling_avg = grp.transform(
        lambda s: s.shift(1).rolling(window=window, min_periods=window).mean()
    )

    # Spike ratio
    spike_ratio = df["volume"] / rolling_avg

    result = df[["stock_id", "ticker", "date", "volume", "adj_close"]].copy()
    result["rolling_avg_volume"]  = rolling_avg.round(0)
    result["volume_spike_ratio"]  = spike_ratio.round(4)

    valid = result["volume_spike_ratio"].notna().sum()
    nan   = result["volume_spike_ratio"].isna().sum()
    log.info(
        "Computed %d spike ratios, %d NaN warm-up rows (first %d days per ticker).",
        valid, nan, window
    )
    return result


# ─────────────────────────────────────────────────────────────────
# DATABASE WRITE
# ─────────────────────────────────────────────────────────────────

def update_volume_columns(
    con: sqlite3.Connection,
    df: pd.DataFrame,
) -> int:
    """
    Update rolling_avg_volume and volume_spike_ratio in fact_risk_metric.
    Preserves existing rolling_vol_20d and drawdown columns.
    """
    sql_read = """
        SELECT rolling_vol_20d, drawdown
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
    written = 0

    for row in df.itertuples(index=False):
        existing = cur.execute(sql_read, (int(row.stock_id), row.date)).fetchone()
        vol_20d, drawdown = existing if existing else (None, None)

        avg_vol = float(row.rolling_avg_volume) if pd.notna(row.rolling_avg_volume) else None
        spike   = float(row.volume_spike_ratio) if pd.notna(row.volume_spike_ratio) else None

        cur.execute(sql_upsert, (
            int(row.stock_id), row.date,
            vol_20d, drawdown, avg_vol, spike,
        ))
        written += 1

    con.commit()
    log.info("Updated volume columns for %d rows in fact_risk_metric.", written)
    return written


# ─────────────────────────────────────────────────────────────────
# VALIDATION & REPORTING
# ─────────────────────────────────────────────────────────────────

def validate(con: sqlite3.Connection, window: int) -> None:
    log.info("")
    log.info("─" * 76)
    log.info("VOLUME SPIKE VALIDATION REPORT")
    log.info("─" * 76)
    log.info("%-6s  %6s  %8s  %8s  %8s  %8s  %8s",
             "Ticker","Rows","NaN%","AvgRatio","MaxRatio","Days>2x","Days>3x")
    log.info("%-6s  %6s  %8s  %8s  %8s  %8s  %8s",
             "──────","──────","──────","────────","────────","───────","───────")

    rows = con.execute("""
        SELECT  s.ticker,
                COUNT(*)                                         AS n,
                SUM(CASE WHEN m.volume_spike_ratio IS NULL
                         THEN 1 ELSE 0 END)                      AS nan_n,
                ROUND(AVG(m.volume_spike_ratio), 3)              AS avg_ratio,
                ROUND(MAX(m.volume_spike_ratio), 2)              AS max_ratio,
                SUM(CASE WHEN m.volume_spike_ratio >= 2.0
                         THEN 1 ELSE 0 END)                      AS days_2x,
                SUM(CASE WHEN m.volume_spike_ratio >= 3.0
                         THEN 1 ELSE 0 END)                      AS days_3x
        FROM    fact_risk_metric m
        JOIN    dim_stock        s ON s.stock_id = m.stock_id
        GROUP   BY m.stock_id
        ORDER   BY max_ratio DESC
    """).fetchall()

    for ticker, n, nan_n, avg_r, max_r, d2x, d3x in rows:
        nan_pct = f"{nan_n/n*100:.0f}" if n else "0"
        flag = "  ← high spike" if max_r and max_r > 8.0 else ""
        log.info("%-6s  %6d  %7s%%  %8.3f  %8.2f  %8d  %8d%s",
                 ticker, n, nan_pct, avg_r or 0, max_r or 0, d2x or 0, d3x or 0, flag)

    n_tickers = con.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
    expected_nan = window * n_tickers
    actual_nan = con.execute(
        "SELECT COUNT(*) FROM fact_risk_metric WHERE volume_spike_ratio IS NULL"
    ).fetchone()[0]
    log.info("─" * 76)
    log.info("NaN rows (warm-up): %d  (expected ≈ %d = %d days × %d tickers)",
             actual_nan, expected_nan, window, n_tickers)


def print_top_spikes(con: sqlite3.Connection, top_n: int) -> None:
    """Show the N largest volume spikes with price context."""
    rows = con.execute(f"""
        SELECT  s.ticker,
                m.date,
                ROUND(m.volume_spike_ratio, 2)               AS spike,
                CAST(m.rolling_avg_volume AS INTEGER)        AS avg_vol,
                fp.volume                                    AS today_vol,
                ROUND(fp.close, 2)                           AS close,
                ROUND(
                    (fp.adj_close / LAG(fp.adj_close)
                     OVER (PARTITION BY fp.stock_id ORDER BY fp.date)) - 1,
                    4
                )                                            AS daily_ret
        FROM    fact_risk_metric m
        JOIN    dim_stock        s  ON s.stock_id = m.stock_id
        JOIN    fact_price       fp ON fp.stock_id = m.stock_id
                                   AND fp.date = m.date
        WHERE   m.volume_spike_ratio IS NOT NULL
        ORDER   BY m.volume_spike_ratio DESC
        LIMIT   {top_n}
    """).fetchall()

    log.info("")
    log.info("TOP %d VOLUME SPIKES (with price context)", top_n)
    log.info("─" * 76)
    log.info("%-6s  %-12s  %8s  %12s  %12s  %8s  %9s",
             "Ticker","Date","Spike","AvgVol","TodayVol","Close","DayRet%")
    log.info("%-6s  %-12s  %8s  %12s  %12s  %8s  %9s",
             "──────","──────────","──────","──────────","─────────","──────","────────")
    for ticker, date, spike, avg_vol, today_vol, close, ret in rows:
        ret_str = f"{ret*100:+.2f}%" if ret is not None else "  —"
        log.info(
            f"{ticker:<6}  {date:<12}  {float(spike or 0):8.2f}  "
            f"{int(avg_vol or 0):12,d}  {int(today_vol or 0):12,d}  "
            f"{float(close or 0):8.2f}  {ret_str:>9}"
        )
    log.info("─" * 76)
    log.info("Interpretation: look for large spikes on big return days → institutional moves")
    log.info("                large spike on near-zero return → accumulation/distribution")


def print_quiet_days(con: sqlite3.Connection) -> None:
    """Show days where volume was unusually LOW — potential liquidity risk."""
    rows = con.execute("""
        SELECT  s.ticker,
                COUNT(*) AS days_below_half
        FROM    fact_risk_metric m
        JOIN    dim_stock        s ON s.stock_id = m.stock_id
        WHERE   m.volume_spike_ratio < 0.5
          AND   m.volume_spike_ratio IS NOT NULL
        GROUP   BY m.stock_id
        ORDER   BY days_below_half DESC
        LIMIT   10
    """).fetchall()

    log.info("")
    log.info("LOW VOLUME DAYS (spike ratio < 0.5) — potential liquidity concern")
    log.info("  %-8s  %s", "Ticker", "Days with < 50% of average volume")
    for ticker, n in rows:
        log.info("  %-8s  %d", ticker, n)


# ─────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────

def run(db_path: str, window: int, top_n: int, report_only: bool) -> None:
    log.info("=" * 60)
    log.info("STEP 9 — COMPUTE VOLUME SPIKE INDICATOR")
    log.info("  DB       : %s", db_path)
    log.info("  Window   : %d trading days", window)
    log.info("  Shift    : 1 day (no look-ahead bias)")
    log.info("=" * 60)

    con = get_connection(db_path)

    # 1. Load
    df = load_volume_data(con)

    # 2. Compute
    result = compute_volume_metrics(df, window)

    # 3. Write (unless report-only)
    if not report_only:
        update_volume_columns(con, result)
    else:
        log.info("--report flag: skipping DB write.")

    # 4. Validate and report
    validate(con, window)
    print_top_spikes(con, top_n)
    print_quiet_days(con)

    log.info("")
    log.info("STEP 9 COMPLETE.")
    con.close()


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step 9 — Compute volume spike indicator.")
    p.add_argument("--db",     default=DEFAULT_DB,     help="SQLite DB path")
    p.add_argument("--window", default=DEFAULT_WINDOW, type=int,
                   help=f"Rolling average window in days (default {DEFAULT_WINDOW})")
    p.add_argument("--top",    default=DEFAULT_TOP,    type=int, dest="top_n",
                   help=f"Number of top spikes to show in report (default {DEFAULT_TOP})")
    p.add_argument("--report", action="store_true", dest="report_only",
                   help="Print diagnostic report without writing to DB")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.db, args.window, args.top_n, args.report_only)
