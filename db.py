"""Data layer: executes agent-generated SQL against Postgres (standing in for Redshift).

Kept separate from library.py (AI layer) and app.py (UI layer) on purpose --
this module owns the one thing that's allowed to run a query against real data,
and every query goes through the same guardrail before it's executed.
"""
import os
import re

import pandas as pd
import psycopg2

DEFAULT_ROW_LIMIT = 500
STATEMENT_TIMEOUT_MS = 5000

# Blocks any statement-changing keyword appearing anywhere in the query,
# including inside a CTE -- not just DML on the outermost statement.
_DISALLOWED_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|create|copy|call|merge|vacuum)\b",
    re.IGNORECASE,
)
_LIMIT_AT_END = re.compile(r"\blimit\s+\d+\s*$", re.IGNORECASE)


class UnsafeQueryError(Exception):
    """Raised when generated SQL fails the read-only guardrail."""


def _validate_select_only(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if not re.match(r"^\s*(with\b.*?)?select\b", stripped, re.IGNORECASE | re.DOTALL):
        raise UnsafeQueryError("Only SELECT queries can be executed.")
    if _DISALLOWED_KEYWORDS.search(stripped):
        raise UnsafeQueryError("Query contains a disallowed keyword.")


def _enforce_row_limit(sql: str, limit: int) -> str:
    stripped = sql.strip().rstrip(";")
    if _LIMIT_AT_END.search(stripped):
        return stripped
    return f"{stripped} LIMIT {limit}"


def run_select_query(sql: str, limit: int = DEFAULT_ROW_LIMIT) -> pd.DataFrame:
    """Validate and execute a read-only query, returning results as a DataFrame."""
    _validate_select_only(sql)
    safe_sql = _enforce_row_limit(sql, limit)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set.")

    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute(safe_sql)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

    return pd.DataFrame(rows, columns=columns)
