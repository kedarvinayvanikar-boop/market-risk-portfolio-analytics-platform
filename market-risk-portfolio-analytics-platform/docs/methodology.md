# Methodology

## Objective

The platform is built to simulate a realistic finance/data analyst workflow: collect market data, structure it in a relational database, compute risk indicators, flag abnormal conditions, and explain how those risks affect a portfolio.

## Risk Metrics

| Metric | Calculation | Interpretation |
|---|---|---|
| Daily return | `P_t / P_{t-1} - 1` | One-day percentage change. |
| Log return | `ln(P_t / P_{t-1})` | Continuously compounded return transformation. |
| Rolling volatility | Standard deviation of daily returns over a 20-day window, annualized by `sqrt(252)` | Recent uncertainty/risk. |
| Drawdown | `P_t / max(P_1...P_t) - 1` | Loss from prior high. |
| Volume spike | `Volume_t / rolling_avg_volume_20d` | Abnormal trading activity. |
| Portfolio return | `sum(w_i * R_i)` | Weighted portfolio daily return. |
| Contribution | `w_i * R_i` | Stock or sector contribution to return. |

## Alert Rules

The alert engine uses interpretable business rules rather than a black-box model. This makes the dashboard easier to explain in interviews and closer to analyst workflows where traceability matters.

| Rule | Trigger | Why it matters |
|---|---|---|
| Volatility spike | Stock volatility rises materially above recent baseline | Risk regime may be changing. |
| Drawdown | Stock is down more than 20% from its peak | Downside risk is material. |
| Volume spike | Volume is more than 2× normal volume | Move may be information-driven. |
| Large move | Daily move exceeds 5% in absolute value | Analyst should investigate. |
| Composite | Multiple signals on the same date | Highest-priority risk event. |

## Validation

The Excel validation workbook checks a sample of the core formulas independently:

- return calculation
- drawdown calculation
- portfolio contribution calculation
- benchmark active return calculation

The goal is to show that the dashboard is not just visually correct, but analytically traceable.
