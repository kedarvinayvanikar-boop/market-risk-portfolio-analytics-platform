"""
Run the full Market Risk & Portfolio Exposure Analytics Platform pipeline.

This script assumes it is executed from the repository root:
    python3 run_pipeline.py

It creates stocks.db locally and writes outputs to outputs/.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
DB = ROOT / "stocks.db"
UNIVERSE = ROOT / "data" / "universe.csv"

COMMANDS = [
    [PYTHON, "src/data_pipeline/portfolio_universe.py", "--db", str(DB), "--csv", str(UNIVERSE)],
    [PYTHON, "src/data_pipeline/ingest_prices.py", "--db", str(DB)],
    [PYTHON, "src/data_pipeline/verify_load.py", "--db", str(DB)],
    [PYTHON, "src/risk_metrics/compute_returns.py", "--db", str(DB)],
    [PYTHON, "src/risk_metrics/compute_volatility.py", "--db", str(DB)],
    [PYTHON, "src/risk_metrics/compute_drawdown.py", "--db", str(DB)],
    [PYTHON, "src/risk_metrics/compute_volume_spike.py", "--db", str(DB)],
    [PYTHON, "src/alerting/alert_engine.py", "--db", str(DB)],
    [PYTHON, "src/portfolio_analytics/portfolio_engine.py", "--db", str(DB)],
    [PYTHON, "src/portfolio_analytics/portfolio_exposure.py", "--db", str(DB)],
    [PYTHON, "src/reporting/run_reporting_pipeline.py"],
]


def run_command(command: list[str]) -> None:
    print("\n" + "=" * 90)
    print("Running:", " ".join(command))
    print("=" * 90)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    for command in COMMANDS:
        run_command(command)
    print("\nPipeline complete. Check the outputs/ folder.")


if __name__ == "__main__":
    main()
