"""
Step 16: Export clean Power BI-ready CSV files from the reporting views.

Output created:
- outputs/powerbi_exports/*.csv
- outputs/powerbi_exports/powerbi_data_dictionary.csv
- outputs/powerbi_exports/PowerBI_Build_Notes.md

Why CSV exports?
Power BI can connect to SQLite through ODBC, but CSV exports are simpler and more reliable
for a student project demo. The data still comes from your SQLite database and SQL views.
"""
from __future__ import annotations

import pandas as pd

from config import DB_PATH, OUTPUT_DIR
from db_utils import connect, read_sql, require_tables

POWERBI_VIEWS = {
    "portfolio_vs_benchmark": "vw_portfolio_vs_benchmark",
    "latest_stock_risk": "vw_latest_stock_risk",
    "top_risk_stocks_recent": "vw_top_risk_stocks_recent",
    "sector_risk_summary_latest": "vw_sector_risk_summary_latest",
    "portfolio_exposure_latest": "vw_portfolio_exposure_latest",
    "sector_portfolio_exposure_latest": "vw_sector_portfolio_exposure_latest",
    "active_alerts_recent": "vw_active_alerts_recent",
}

DATA_DICTIONARY = [
    ["portfolio_vs_benchmark", "date", "Trading date."],
    ["portfolio_vs_benchmark", "portfolio_return", "Daily return of the simulated portfolio."],
    ["portfolio_vs_benchmark", "benchmark_return", "Daily return of benchmark index/ETF."],
    ["portfolio_vs_benchmark", "active_return", "Portfolio return minus benchmark return."],
    ["portfolio_vs_benchmark", "portfolio_drawdown", "Portfolio decline from prior peak value."],
    ["latest_stock_risk", "rolling_vol_20d", "20-day rolling volatility computed from daily returns."],
    ["latest_stock_risk", "drawdown", "Current percentage decline from prior peak."],
    ["latest_stock_risk", "volume_spike_ratio", "Current volume divided by 20-day average volume."],
    ["portfolio_exposure_latest", "weight", "Portfolio allocation to each stock."],
    ["portfolio_exposure_latest", "contribution_to_return", "Stock weight multiplied by latest daily return."],
    ["portfolio_exposure_latest", "weighted_downside_exposure", "Stock weight multiplied by absolute drawdown."],
    ["active_alerts_recent", "severity", "Low/Medium/High alert severity from rule-based engine."],
]


def export_powerbi_files() -> None:
    export_dir = OUTPUT_DIR / "powerbi_exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    with connect(DB_PATH) as conn:
        require_tables(conn, list(POWERBI_VIEWS.values()))

        for file_stem, view_name in POWERBI_VIEWS.items():
            df = read_sql(conn, f"SELECT * FROM {view_name};")
            output_path = export_dir / f"{file_stem}.csv"
            df.to_csv(output_path, index=False)
            print(f"Exported {len(df):,} rows: {output_path}")

    dictionary_df = pd.DataFrame(DATA_DICTIONARY, columns=["dataset", "field", "definition"])
    dictionary_df.to_csv(export_dir / "powerbi_data_dictionary.csv", index=False)

    build_notes = """# Power BI Build Notes

## Import these CSV files
1. portfolio_vs_benchmark.csv
2. latest_stock_risk.csv
3. top_risk_stocks_recent.csv
4. sector_risk_summary_latest.csv
5. portfolio_exposure_latest.csv
6. sector_portfolio_exposure_latest.csv
7. active_alerts_recent.csv

## Suggested dashboard pages

### Page 1 - Market Risk Overview
- KPI cards: active alerts, worst drawdown, highest 20-day volatility
- Bar chart: top 10 stocks by risk score
- Table: recent high-severity alerts

### Page 2 - Sector Risk
- Matrix/heatmap: sector by average volatility and worst drawdown
- Bar chart: active alerts by sector

### Page 3 - Portfolio Exposure
- Donut/bar: portfolio weight by sector
- Bar chart: stock contribution to return
- Bar chart: weighted downside exposure by stock

### Page 4 - Portfolio vs Benchmark
- Line chart: portfolio value vs benchmark value over time
- Line chart: portfolio drawdown vs benchmark drawdown
- Card: cumulative active return

## Recommended slicers
- Date
- Ticker
- Sector
- Alert severity

## Main business questions answered
- Which stocks are currently riskiest?
- Which sectors are driving risk?
- How much of the portfolio is exposed to alerted names?
- Is the portfolio outperforming or underperforming the benchmark?
"""
    (export_dir / "PowerBI_Build_Notes.md").write_text(build_notes, encoding="utf-8")
    print(f"Step 16 complete: Power BI files written to {export_dir}")


if __name__ == "__main__":
    export_powerbi_files()
