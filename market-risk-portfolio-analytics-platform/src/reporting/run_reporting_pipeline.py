"""
Run the final reporting/export pipeline in order.

Before running:
1. Put this folder beside your SQLite database, or update DB_PATH in config.py.
2. Make sure Steps 1-12 have created the expected tables.
3. Run: python run_steps_13_to_17.py
"""
from benchmark_comparison import build_benchmark_comparison
from create_reporting_views import create_reporting_views
from excel_validation_export import create_excel_validation_workbook
from powerbi_export import export_powerbi_files
from project_summary_generator import generate_project_summary


def main() -> None:
    build_benchmark_comparison()
    create_reporting_views()
    create_excel_validation_workbook()
    export_powerbi_files()
    generate_project_summary()
    print("All Reporting pipeline completed successfully.")


if __name__ == "__main__":
    main()
