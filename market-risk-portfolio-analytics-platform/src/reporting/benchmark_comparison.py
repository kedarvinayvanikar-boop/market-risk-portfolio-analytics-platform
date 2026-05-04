"""
Step 13: Compare the simulated portfolio against a benchmark.

Input tables expected:
- portfolio_daily_value(portfolio_date/date, portfolio_return, portfolio_value optional)
- benchmark_price(date, adj_close, benchmark_ticker optional) OR benchmark inside dim_stock/fact_return/fact_price

Output created:
- SQLite table: portfolio_benchmark_comparison
- CSV: outputs/portfolio_vs_benchmark.csv

This version is intentionally flexible because the earlier ingestion script stores SPY
in a separate benchmark_price table, while some databases store the benchmark as a
regular ticker in dim_stock/fact_price.
"""
from __future__ import annotations

import pandas as pd

from config import BENCHMARK_TICKER, DB_PATH, INITIAL_PORTFOLIO_VALUE, OUTPUT_DIR, TABLES
from db_utils import (
    connect,
    detect_portfolio_date_column,
    read_sql,
    require_columns,
    require_tables,
    safe_to_sql,
    table_exists,
)


def _calculate_drawdown(value_series: pd.Series) -> pd.Series:
    running_peak = value_series.cummax()
    return (value_series / running_peak) - 1.0


def load_portfolio_returns(conn) -> pd.DataFrame:
    portfolio_table = TABLES["portfolio_daily_value"]
    date_col = detect_portfolio_date_column(conn, portfolio_table)
    require_columns(conn, portfolio_table, [date_col, "portfolio_return"])

    cols = set(pd.read_sql_query(f"PRAGMA table_info({portfolio_table});", conn)["name"])
    value_expr = "portfolio_value" if "portfolio_value" in cols else "NULL AS portfolio_value"

    query = f"""
        SELECT
            {date_col} AS date,
            portfolio_return,
            {value_expr}
        FROM {portfolio_table}
        ORDER BY {date_col};
    """
    df = read_sql(conn, query)
    df["date"] = pd.to_datetime(df["date"])
    df["portfolio_return"] = pd.to_numeric(df["portfolio_return"], errors="coerce")
    df = df.dropna(subset=["date", "portfolio_return"]).sort_values("date")

    if df.empty:
        raise RuntimeError("No usable portfolio return data found in portfolio_daily_value.")

    if df["portfolio_value"].isna().all():
        df["portfolio_value"] = INITIAL_PORTFOLIO_VALUE * (1.0 + df["portfolio_return"]).cumprod()
    else:
        df["portfolio_value"] = pd.to_numeric(df["portfolio_value"], errors="coerce")
        if df["portfolio_value"].isna().any():
            df["portfolio_value"] = INITIAL_PORTFOLIO_VALUE * (1.0 + df["portfolio_return"]).cumprod()

    return df


def _load_benchmark_from_benchmark_price(conn) -> pd.DataFrame | None:
    """Load benchmark returns from the benchmark_price table if it exists."""
    if not table_exists(conn, "benchmark_price"):
        return None

    cols = set(pd.read_sql_query("PRAGMA table_info(benchmark_price);", conn)["name"])
    if not {"date", "adj_close"}.issubset(cols):
        return None

    where_clause = ""
    params: tuple = ()
    if "benchmark_ticker" in cols:
        where_clause = "WHERE UPPER(benchmark_ticker) = UPPER(?)"
        params = (BENCHMARK_TICKER,)

    prices = read_sql(
        conn,
        f"""
        SELECT date, adj_close
        FROM benchmark_price
        {where_clause}
        ORDER BY date;
        """,
        params,
    )

    if prices.empty:
        return None

    prices["date"] = pd.to_datetime(prices["date"])
    prices["adj_close"] = pd.to_numeric(prices["adj_close"], errors="coerce")
    prices = prices.dropna(subset=["date", "adj_close"]).sort_values("date")
    prices["benchmark_return"] = prices["adj_close"].pct_change()
    out = prices[["date", "benchmark_return"]].dropna()
    return out if not out.empty else None


def _load_benchmark_from_stock_tables(conn) -> pd.DataFrame | None:
    """Load benchmark if SPY/XIC/etc. was stored as a normal stock."""
    stock_table = TABLES["stock"]
    return_table = TABLES["return"]
    price_table = TABLES["price"]

    if not table_exists(conn, stock_table):
        return None

    benchmark_row = conn.execute(
        f"SELECT stock_id FROM {stock_table} WHERE UPPER(ticker) = UPPER(?) LIMIT 1;",
        (BENCHMARK_TICKER,),
    ).fetchone()

    if benchmark_row is None:
        return None

    benchmark_stock_id = benchmark_row[0]

    if table_exists(conn, return_table):
        return_count = conn.execute(
            f"SELECT COUNT(*) FROM {return_table} WHERE stock_id = ?;",
            (benchmark_stock_id,),
        ).fetchone()[0]

        if return_count > 0:
            df = read_sql(
                conn,
                f"""
                SELECT date, daily_return AS benchmark_return
                FROM {return_table}
                WHERE stock_id = ?
                ORDER BY date;
                """,
                (benchmark_stock_id,),
            )
            df["date"] = pd.to_datetime(df["date"])
            df["benchmark_return"] = pd.to_numeric(df["benchmark_return"], errors="coerce")
            df = df.dropna(subset=["date", "benchmark_return"]).sort_values("date")
            return df if not df.empty else None

    if table_exists(conn, price_table):
        require_columns(conn, price_table, ["stock_id", "date", "adj_close"])
        prices = read_sql(
            conn,
            f"""
            SELECT date, adj_close
            FROM {price_table}
            WHERE stock_id = ?
            ORDER BY date;
            """,
            (benchmark_stock_id,),
        )
        prices["date"] = pd.to_datetime(prices["date"])
        prices["adj_close"] = pd.to_numeric(prices["adj_close"], errors="coerce")
        prices = prices.dropna(subset=["date", "adj_close"]).sort_values("date")
        prices["benchmark_return"] = prices["adj_close"].pct_change()
        df = prices[["date", "benchmark_return"]].dropna()
        return df if not df.empty else None

    return None


def load_benchmark_returns(conn) -> pd.DataFrame:
    benchmark = _load_benchmark_from_benchmark_price(conn)
    if benchmark is None:
        benchmark = _load_benchmark_from_stock_tables(conn)

    if benchmark is None or benchmark.empty:
        raise RuntimeError(
            f"No usable benchmark returns found for {BENCHMARK_TICKER}. Expected either a "
            "benchmark_price table from ingest_prices.py or benchmark data stored as a ticker "
            "inside dim_stock/fact_price/fact_return."
        )

    benchmark["date"] = pd.to_datetime(benchmark["date"])
    benchmark["benchmark_return"] = pd.to_numeric(benchmark["benchmark_return"], errors="coerce")
    benchmark = benchmark.dropna(subset=["date", "benchmark_return"]).sort_values("date")
    return benchmark


def build_benchmark_comparison() -> pd.DataFrame:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with connect(DB_PATH) as conn:
        require_tables(conn, [TABLES["portfolio_daily_value"]])

        portfolio = load_portfolio_returns(conn)
        benchmark = load_benchmark_returns(conn)

        comparison = portfolio.merge(benchmark, on="date", how="inner")
        if comparison.empty:
            raise RuntimeError(
                "Portfolio and benchmark have no overlapping dates. Check date formats and benchmark coverage."
            )

        comparison = comparison.sort_values("date").reset_index(drop=True)
        comparison["benchmark_value"] = INITIAL_PORTFOLIO_VALUE * (
            1.0 + comparison["benchmark_return"]
        ).cumprod()
        comparison["active_return"] = comparison["portfolio_return"] - comparison["benchmark_return"]
        comparison["cumulative_portfolio_return"] = (
            comparison["portfolio_value"] / INITIAL_PORTFOLIO_VALUE
        ) - 1.0
        comparison["cumulative_benchmark_return"] = (
            comparison["benchmark_value"] / INITIAL_PORTFOLIO_VALUE
        ) - 1.0
        comparison["cumulative_active_return"] = (
            comparison["cumulative_portfolio_return"]
            - comparison["cumulative_benchmark_return"]
        )
        comparison["portfolio_drawdown"] = _calculate_drawdown(comparison["portfolio_value"])
        comparison["benchmark_drawdown"] = _calculate_drawdown(comparison["benchmark_value"])

        comparison["date"] = comparison["date"].dt.strftime("%Y-%m-%d")
        ordered_columns = [
            "date",
            "portfolio_return",
            "benchmark_return",
            "active_return",
            "portfolio_value",
            "benchmark_value",
            "cumulative_portfolio_return",
            "cumulative_benchmark_return",
            "cumulative_active_return",
            "portfolio_drawdown",
            "benchmark_drawdown",
        ]
        comparison = comparison[ordered_columns]

        safe_to_sql(
            comparison,
            TABLES["portfolio_benchmark_comparison"],
            conn,
            if_exists="replace",
        )

    output_csv = OUTPUT_DIR / "portfolio_vs_benchmark.csv"
    comparison.to_csv(output_csv, index=False)
    print(f"Step 13 complete: wrote {len(comparison):,} rows to {output_csv}")
    return comparison


if __name__ == "__main__":
    build_benchmark_comparison()
