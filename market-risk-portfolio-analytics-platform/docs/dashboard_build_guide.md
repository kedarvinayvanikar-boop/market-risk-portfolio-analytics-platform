# Power BI Dashboard Build Guide

## Data Source

After running the pipeline, use the files in:

```text
outputs/powerbi_exports/
```

Recommended Power BI pages:

## Page 1: Portfolio vs Benchmark

| Visual | Table | Fields |
|---|---|---|
| Line chart | `portfolio_vs_benchmark` | X-axis: `date`; Y-axis: `portfolio_value`, `benchmark_value` |
| Line chart | `portfolio_vs_benchmark` | X-axis: `date`; Y-axis: `portfolio_drawdown`, `benchmark_drawdown` |
| Card | `portfolio_vs_benchmark` | Max of `portfolio_value` |
| Card | `portfolio_vs_benchmark` | Max of `benchmark_value` |
| Card | `portfolio_vs_benchmark` | Min of `portfolio_drawdown` |
| Card | `portfolio_vs_benchmark` | Min of `benchmark_drawdown` |

## Page 2: Market Risk Overview

| Visual | Table | Fields |
|---|---|---|
| Bar chart | `latest_stock_risk` | Y-axis: `ticker`; X-axis: `rolling_vol_20d` |
| Bar chart | `latest_stock_risk` | Y-axis: `ticker`; X-axis: `drawdown` |
| Table | `active_alerts_recent` | `date`, `ticker`, `sector_name`, `alert_type`, `severity` |

## Page 3: Portfolio Exposure

| Visual | Table | Fields |
|---|---|---|
| Bar chart | `portfolio_exposure_latest` | Y-axis: `ticker`; X-axis: `contribution` |
| Donut chart | `sector_portfolio_exposure_latest` | Legend: `sector_name`; Values: `weight` |
| Table | `portfolio_exposure_latest` | `ticker`, `sector_name`, `weight`, `daily_return`, `contribution` |

## Page 4: Sector Risk View

| Visual | Table | Fields |
|---|---|---|
| Bar chart | `sector_risk_summary_latest` | X-axis: `sector_name`; Y-axis: `avg_rolling_vol_20d` |
| Bar chart | `sector_risk_summary_latest` | X-axis: `sector_name`; Y-axis: `avg_drawdown` |
| Table | `sector_portfolio_exposure_latest` | sector exposure and contribution fields |
