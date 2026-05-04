"""
Configuration for Steps 13-17 of the Market Risk & Portfolio Exposure Analytics Platform.

This version assumes the project folder looks like this:

market-risk-portfolio-analytics-platform/
    stocks.db
    src/reporting/
        config.py
        run_steps_13_to_17.py

The database path is built from this file's location, so it works whether you run
from the main folder or from inside market_risk_steps_13_17.
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent

# Your completed SQLite database from Steps 1-12 should be here.
DB_PATH = PROJECT_ROOT / "stocks.db"

# Output folder used by Steps 13-17.
OUTPUT_DIR = PROJECT_ROOT / "outputs"

# Benchmark ticker. The earlier ingestion script stores SPY in benchmark_price.
BENCHMARK_TICKER = "SPY"

# Starting value used for cumulative performance comparison.
INITIAL_PORTFOLIO_VALUE = 100.0

# Lookback used in reporting views for "recent" alerts.
RECENT_ALERT_LOOKBACK_DAYS = 30

# Expected core table names from the build blueprint.
TABLES = {
    "sector": "dim_sector",
    "stock": "dim_stock",
    "price": "fact_price",
    "return": "fact_return",
    "risk_metric": "fact_risk_metric",
    "portfolio_holding": "portfolio_holding",
    "portfolio_daily_value": "portfolio_daily_value",
    "risk_alert": "risk_alert",
    "portfolio_benchmark_comparison": "portfolio_benchmark_comparison",
}
