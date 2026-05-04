"""
Shared SQLite helper functions for Steps 13-17.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import pandas as pd


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    query = """
        SELECT 1
        FROM sqlite_master
        WHERE type IN ('table', 'view') AND name = ?
        LIMIT 1;
    """
    return conn.execute(query, (table_name,)).fetchone() is not None


def get_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name});").fetchall()
    return [row[1] for row in rows]


def require_tables(conn: sqlite3.Connection, table_names: Iterable[str]) -> None:
    missing = [name for name in table_names if not table_exists(conn, name)]
    if missing:
        raise RuntimeError(
            "Missing required SQLite table(s): "
            + ", ".join(missing)
            + ". Make sure Steps 1-12 were completed or update config.py/table names."
        )


def require_columns(conn: sqlite3.Connection, table_name: str, columns: Iterable[str]) -> None:
    existing = set(get_columns(conn, table_name))
    missing = [col for col in columns if col not in existing]
    if missing:
        raise RuntimeError(
            f"Table '{table_name}' is missing required column(s): {', '.join(missing)}. "
            "Either add these columns or adjust the code to your schema."
        )


def detect_portfolio_date_column(conn: sqlite3.Connection, portfolio_table: str) -> str:
    cols = set(get_columns(conn, portfolio_table))
    if "portfolio_date" in cols:
        return "portfolio_date"
    if "date" in cols:
        return "date"
    raise RuntimeError(
        f"Could not find a date column in {portfolio_table}. Expected 'portfolio_date' or 'date'."
    )


def read_sql(conn: sqlite3.Connection, query: str, params: tuple | dict | None = None) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=params or ())


def safe_to_sql(df: pd.DataFrame, table_name: str, conn: sqlite3.Connection, if_exists: str = "replace") -> None:
    if df.empty:
        raise RuntimeError(f"Refusing to write empty DataFrame to table '{table_name}'.")
    df.to_sql(table_name, conn, if_exists=if_exists, index=False)
