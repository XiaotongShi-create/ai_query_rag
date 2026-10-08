"""Data layer: executes agent-generated SQL against Postgres.

Kept separate from library.py (AI layer) and app.py (UI layer) on purpose --
this module owns the one thing that's allowed to run a query against real data,
and every query goes through the same guardrail before it's executed.

Guardrail layers, outermost first:
  1. Parse: the SQL must parse (Postgres dialect) as exactly one statement.
  2. Read-only: the parse tree may contain no write/DDL/side-effect nodes anywhere,
     including inside CTEs (writable CTEs are the classic way around keyword checks).
  3. Scope: only tables/columns documented in the semantic layer may be touched
     (see Table_Schema_A.json); SELECT * on a physical table is refused.
  4. Limits: row cap, statement timeout, and a READ ONLY transaction enforced by the
     database itself as a backstop in case 1-3 ever miss something.
"""
import os
from contextlib import closing

import pandas as pd
import psycopg2
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

DEFAULT_ROW_LIMIT = 500
STATEMENT_TIMEOUT_MS = 5000

# Scope is {table_name: {allowed_column_name, ...}} -- built from the semantic layer.
Scope = dict

# Any of these node types anywhere in the tree means the query is not read-only.
_WRITE_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.TruncateTable, exp.Command, exp.Copy, exp.Grant, exp.Set, exp.Use,
    exp.Transaction, exp.Commit, exp.Rollback, exp.Into, exp.Returning,
)

# Functions with side effects, server-file access, or that can stall the server.
_DENIED_FUNCTIONS = {
    "pg_sleep", "pg_sleep_for", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
    "pg_stat_file", "lo_import", "lo_export", "dblink", "dblink_exec",
    "pg_terminate_backend", "pg_cancel_backend", "set_config", "nextval", "setval",
}


class UnsafeQueryError(Exception):
    """Raised when generated SQL fails the guardrail. The message is shown to the
    model, so it says what to do instead, which lets the agent self-correct."""


def _function_name(node: exp.Expression) -> str:
    name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
    return name.lower()


def _parse_single_statement(sql: str) -> exp.Expression:
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except SqlglotError as e:
        raise UnsafeQueryError(f"Could not parse the query as SQL: {str(e)[:150]}")
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeQueryError("Send exactly one SQL statement.")
    return statements[0]


def _check_read_only(tree: exp.Expression) -> None:
    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        raise UnsafeQueryError("Only SELECT queries can be executed.")
    write_node = tree.find(*_WRITE_NODES)
    if write_node is not None:
        raise UnsafeQueryError(
            f"Query contains a write/DDL operation ({type(write_node).__name__}); "
            "only read-only SELECT queries are allowed."
        )
    for func in tree.find_all(exp.Func):
        if _function_name(func) in _DENIED_FUNCTIONS:
            raise UnsafeQueryError(f"Function {_function_name(func)}() is not allowed.")


def _derived_names(tree: exp.Expression) -> set:
    """Names that refer to something the query itself defined (CTE names, column
    aliases, derived-table aliases) rather than to a physical table or column."""
    names = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
    names |= {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    for table_alias in tree.find_all(exp.TableAlias):
        names |= {c.name.lower() for c in table_alias.columns}
    return names


def _check_scope(tree: exp.Expression, scope: Scope) -> None:
    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
    derived = _derived_names(tree)

    # Tables: only semantic-layer tables in the default schema.
    referenced = set()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeQueryError("Table-valued functions are not allowed; query the documented tables only.")
        name = table.name.lower()
        if name in cte_names:
            continue
        if table.db and table.db.lower() != "public":
            raise UnsafeQueryError(f"Schema '{table.db}' is out of scope; use the documented tables only.")
        if name not in scope:
            raise UnsafeQueryError(
                f"Table '{name}' is out of scope. Allowed tables: {', '.join(sorted(scope))}."
            )
        referenced.add(name)

    # SELECT * / t.* straight off a physical table would expose undocumented columns.
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            is_star = isinstance(projection, exp.Star) or (
                isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
            )
            if not is_star:
                continue
            source = select.args.get("from_") or select.args.get("from")
            sources = [source.this] if source is not None else []
            sources += [join.this for join in select.args.get("joins") or []]
            if any(isinstance(s, exp.Table) and s.name.lower() not in cte_names for s in sources):
                raise UnsafeQueryError("SELECT * is not allowed on tables; list the columns you need.")

    # Columns: must be documented for at least one table this query touches.
    allowed_columns = set().union(*(scope[t] for t in referenced)) if referenced else set()
    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        name = column.name.lower()
        if name in allowed_columns or (not column.table and name in derived):
            continue
        raise UnsafeQueryError(
            f"Column '{name}' is out of scope or does not exist. "
            f"Documented columns for the tables you used: {', '.join(sorted(allowed_columns))}."
        )


def validate_query(sql: str, scope: Scope = None) -> exp.Expression:
    """Run every static guardrail check; returns the parse tree or raises UnsafeQueryError."""
    tree = _parse_single_statement(sql)
    _check_read_only(tree)
    if scope is not None:
        _check_scope(tree, scope)
    return tree


def _with_row_limit(sql: str, tree: exp.Expression, limit: int) -> str:
    stripped = sql.strip().rstrip(";").rstrip()
    if tree.args.get("limit") is not None:
        return stripped
    return f"{stripped}\nLIMIT {limit}"


def run_select_query(sql: str, limit: int = DEFAULT_ROW_LIMIT, scope: Scope = None) -> pd.DataFrame:
    """Validate and execute a read-only query, returning results as a DataFrame."""
    tree = validate_query(sql, scope)
    safe_sql = _with_row_limit(sql, tree, limit)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set.")

    with closing(psycopg2.connect(database_url)) as conn:
        # Backstop: even if a write slipped past the checks above, Postgres refuses it.
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute(safe_sql)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchmany(limit)

    return pd.DataFrame(rows, columns=columns)
