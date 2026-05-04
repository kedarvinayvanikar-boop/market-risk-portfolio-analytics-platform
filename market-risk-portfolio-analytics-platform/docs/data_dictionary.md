# Data Dictionary

## Core SQLite Tables

| Table | Column | Meaning |
|---|---|---|
| `dim_sector` | `sector_id` | Primary key for sector. |
| `dim_sector` | `sector_name` | Sector label such as Technology or Financials. |
| `dim_stock` | `stock_id` | Primary key for stock. |
| `dim_stock` | `ticker` | Market ticker. |
| `dim_stock` | `company_name` | Company name. |
| `dim_stock` | `sector_id` | Foreign key to `dim_sector`. |
| `fact_price` | `date` | Trading date. |
| `fact_price` | `adj_close` | Adjusted close used for returns. |
| `fact_price` | `volume` | Daily trading volume. |
| `fact_return` | `daily_return` | Simple daily return. |
| `fact_return` | `log_return` | Natural log return. |
| `fact_risk_metric` | `rolling_vol_20d` | 20-day annualized volatility. |
| `fact_risk_metric` | `drawdown` | Current price relative to running peak. |
| `fact_risk_metric` | `volume_spike_ratio` | Daily volume divided by trailing 20-day average. |
| `risk_alert` | `alert_type` | Alert category. |
| `risk_alert` | `severity` | HIGH or MEDIUM. |
| `portfolio_holding` | `weight` | Portfolio weight by stock. |
| `portfolio_daily_value` | `portfolio_return` | Daily weighted portfolio return. |
| `portfolio_daily_value` | `portfolio_value` | Compounded portfolio value from base 100. |

## Power BI Export Tables

| Export | Use in Dashboard |
|---|---|
| `portfolio_vs_benchmark.csv` | Performance and drawdown page. |
| `latest_stock_risk.csv` | Current stock-level risk metrics. |
| `top_risk_stocks_recent.csv` | Recent highest-risk tickers. |
| `sector_risk_summary_latest.csv` | Sector volatility and drawdown summary. |
| `portfolio_exposure_latest.csv` | Stock-level contribution and exposure. |
| `sector_portfolio_exposure_latest.csv` | Sector-level portfolio contribution. |
| `active_alerts_recent.csv` | Most recent alert events. |
