"""
Step 14: Create SQL reporting views for Power BI and analysis.

Input tables expected:
- dim_stock, dim_sector
- fact_return, fact_risk_metric, portfolio_holding, risk_alert
- portfolio_benchmark_comparison from Step 13

Output created in SQLite:
- vw_latest_stock_risk
- vw_top_risk_stocks_recent
- vw_sector_risk_summary_latest
- vw_portfolio_exposure_latest
- vw_sector_portfolio_exposure_latest
- vw_portfolio_vs_benchmark
- vw_active_alerts_recent
"""
from __future__ import annotations

from config import DB_PATH, RECENT_ALERT_LOOKBACK_DAYS, TABLES
from db_utils import connect, require_tables


def create_reporting_views() -> None:
    with connect(DB_PATH) as conn:
        require_tables(
            conn,
            [
                TABLES["sector"],
                TABLES["stock"],
                TABLES["return"],
                TABLES["risk_metric"],
                TABLES["portfolio_holding"],
                TABLES["risk_alert"],
                TABLES["portfolio_benchmark_comparison"],
            ],
        )

        cur = conn.cursor()

        view_names = [
            "vw_latest_stock_risk",
            "vw_top_risk_stocks_recent",
            "vw_sector_risk_summary_latest",
            "vw_portfolio_exposure_latest",
            "vw_sector_portfolio_exposure_latest",
            "vw_portfolio_vs_benchmark",
            "vw_active_alerts_recent",
        ]
        for view_name in view_names:
            cur.execute(f"DROP VIEW IF EXISTS {view_name};")

        # Latest stock-level risk metrics, joined with names/sectors.
        cur.execute(f"""
            CREATE VIEW vw_latest_stock_risk AS
            WITH latest_metric_date AS (
                SELECT MAX(date) AS max_date
                FROM {TABLES['risk_metric']}
            )
            SELECT
                rm.date,
                s.stock_id,
                s.ticker,
                s.company_name,
                sec.sector_name,
                r.daily_return,
                rm.rolling_vol_20d,
                rm.drawdown,
                rm.rolling_avg_volume,
                rm.volume_spike_ratio,
                CASE
                    WHEN rm.drawdown <= -0.15 OR rm.volume_spike_ratio >= 2.0 THEN 'High'
                    WHEN rm.drawdown <= -0.08 OR rm.volume_spike_ratio >= 1.5 THEN 'Medium'
                    ELSE 'Normal'
                END AS risk_level
            FROM {TABLES['risk_metric']} rm
            JOIN latest_metric_date lmd ON rm.date = lmd.max_date
            JOIN {TABLES['stock']} s ON rm.stock_id = s.stock_id
            LEFT JOIN {TABLES['sector']} sec ON s.sector_id = sec.sector_id
            LEFT JOIN {TABLES['return']} r
                ON rm.stock_id = r.stock_id AND rm.date = r.date;
        """)

        # Recent top risk stocks. Uses calendar-day lookback, which is fine for dashboard monitoring.
        cur.execute(f"""
            CREATE VIEW vw_top_risk_stocks_recent AS
            WITH max_date AS (
                SELECT MAX(date) AS latest_date FROM {TABLES['risk_metric']}
            )
            SELECT
                rm.date,
                s.ticker,
                s.company_name,
                sec.sector_name,
                r.daily_return,
                rm.rolling_vol_20d,
                rm.drawdown,
                rm.volume_spike_ratio,
                (ABS(COALESCE(rm.drawdown, 0))
                    + COALESCE(rm.rolling_vol_20d, 0)
                    + CASE WHEN COALESCE(rm.volume_spike_ratio, 0) > 1
                           THEN COALESCE(rm.volume_spike_ratio, 0) / 10.0
                           ELSE 0 END) AS risk_score
            FROM {TABLES['risk_metric']} rm
            JOIN max_date md
            JOIN {TABLES['stock']} s ON rm.stock_id = s.stock_id
            LEFT JOIN {TABLES['sector']} sec ON s.sector_id = sec.sector_id
            LEFT JOIN {TABLES['return']} r
                ON rm.stock_id = r.stock_id AND rm.date = r.date
            WHERE rm.date >= date(md.latest_date, '-{RECENT_ALERT_LOOKBACK_DAYS} days')
            ORDER BY risk_score DESC;
        """)

        # Latest sector risk summary.
        cur.execute(f"""
            CREATE VIEW vw_sector_risk_summary_latest AS
            WITH latest_metric_date AS (
                SELECT MAX(date) AS max_date
                FROM {TABLES['risk_metric']}
            ), latest_alerts AS (
                SELECT stock_id, COUNT(*) AS active_alert_count
                FROM {TABLES['risk_alert']}
                WHERE date = (SELECT max_date FROM latest_metric_date)
                GROUP BY stock_id
            )
            SELECT
                rm.date,
                sec.sector_name,
                COUNT(DISTINCT s.stock_id) AS stock_count,
                AVG(rm.rolling_vol_20d) AS avg_rolling_vol_20d,
                AVG(rm.drawdown) AS avg_drawdown,
                MIN(rm.drawdown) AS worst_drawdown,
                AVG(rm.volume_spike_ratio) AS avg_volume_spike_ratio,
                SUM(COALESCE(la.active_alert_count, 0)) AS active_alert_count
            FROM {TABLES['risk_metric']} rm
            JOIN latest_metric_date lmd ON rm.date = lmd.max_date
            JOIN {TABLES['stock']} s ON rm.stock_id = s.stock_id
            LEFT JOIN {TABLES['sector']} sec ON s.sector_id = sec.sector_id
            LEFT JOIN latest_alerts la ON rm.stock_id = la.stock_id
            GROUP BY rm.date, sec.sector_name
            ORDER BY avg_rolling_vol_20d DESC;
        """)

        # Latest portfolio exposure by stock.
        cur.execute(f"""
            CREATE VIEW vw_portfolio_exposure_latest AS
            WITH latest_return_date AS (
                SELECT MAX(date) AS max_date FROM {TABLES['return']}
            ), latest_alerts AS (
                SELECT stock_id, COUNT(*) AS active_alert_count
                FROM {TABLES['risk_alert']}
                WHERE date = (SELECT max_date FROM latest_return_date)
                GROUP BY stock_id
            ), normalized_holdings AS (
                SELECT
                    holding_id,
                    stock_id,
                    CASE WHEN weight > 1 THEN weight / 100.0 ELSE weight END AS weight
                FROM {TABLES['portfolio_holding']}
            )
            SELECT
                r.date,
                s.stock_id,
                s.ticker,
                s.company_name,
                sec.sector_name,
                nh.weight,
                r.daily_return,
                nh.weight * r.daily_return AS contribution_to_return,
                rm.rolling_vol_20d,
                rm.drawdown,
                nh.weight * ABS(COALESCE(rm.drawdown, 0)) AS weighted_downside_exposure,
                COALESCE(la.active_alert_count, 0) AS active_alert_count
            FROM normalized_holdings nh
            JOIN {TABLES['stock']} s ON nh.stock_id = s.stock_id
            LEFT JOIN {TABLES['sector']} sec ON s.sector_id = sec.sector_id
            JOIN {TABLES['return']} r ON nh.stock_id = r.stock_id
            JOIN latest_return_date lrd ON r.date = lrd.max_date
            LEFT JOIN {TABLES['risk_metric']} rm
                ON nh.stock_id = rm.stock_id AND r.date = rm.date
            LEFT JOIN latest_alerts la ON nh.stock_id = la.stock_id
            ORDER BY weighted_downside_exposure DESC;
        """)

        # Latest portfolio exposure by sector.
        cur.execute("""
            CREATE VIEW vw_sector_portfolio_exposure_latest AS
            SELECT
                date,
                sector_name,
                SUM(weight) AS sector_weight,
                SUM(contribution_to_return) AS sector_contribution_to_return,
                SUM(weighted_downside_exposure) AS sector_weighted_downside_exposure,
                SUM(active_alert_count) AS active_alert_count,
                COUNT(DISTINCT stock_id) AS stock_count
            FROM vw_portfolio_exposure_latest
            GROUP BY date, sector_name
            ORDER BY sector_weighted_downside_exposure DESC;
        """)

        # Simple pass-through view for Power BI.
        cur.execute(f"""
            CREATE VIEW vw_portfolio_vs_benchmark AS
            SELECT *
            FROM {TABLES['portfolio_benchmark_comparison']}
            ORDER BY date;
        """)

        # Recent alert view with stock names and sectors.
        cur.execute(f"""
            CREATE VIEW vw_active_alerts_recent AS
            WITH max_alert_date AS (
                SELECT MAX(date) AS latest_date FROM {TABLES['risk_alert']}
            )
            SELECT
                ra.date,
                s.ticker,
                s.company_name,
                sec.sector_name,
                ra.alert_type,
                ra.alert_value,
                ra.threshold,
                ra.severity
            FROM {TABLES['risk_alert']} ra
            JOIN max_alert_date mad
            JOIN {TABLES['stock']} s ON ra.stock_id = s.stock_id
            LEFT JOIN {TABLES['sector']} sec ON s.sector_id = sec.sector_id
            WHERE ra.date >= date(mad.latest_date, '-{RECENT_ALERT_LOOKBACK_DAYS} days')
            ORDER BY ra.date DESC, ra.severity DESC;
        """)

        conn.commit()
        print("Step 14 complete: reporting views created successfully.")
        print("Views created:")
        for view_name in view_names:
            print(f"- {view_name}")


if __name__ == "__main__":
    create_reporting_views()
