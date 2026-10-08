"""Tests for the data-layer guardrail and the semantic layer it enforces.

Run from the repo root:  python -m unittest discover -s tests -t . -v
Tests marked (db) need DATABASE_URL and are skipped without it; they only read.
"""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import psycopg2
import sqlglot
from dotenv import load_dotenv
from sqlglot import exp

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")
os.chdir(REPO_ROOT)  # library.load_schema_file uses a relative path

import db  # noqa: E402
import library as lib  # noqa: E402

SCOPE = lib.get_allowed_scope("Schema_Type_A")


def rejected(sql, scope=SCOPE):
    with unittest.TestCase().assertRaises(db.UnsafeQueryError) as ctx:
        db.validate_query(sql, scope)
    return str(ctx.exception)


class AcceptsLegitimateQueries(unittest.TestCase):
    def test_accepted(self):
        queries = [
            "SELECT COUNT(*) FROM orders",
            "SELECT company_name, country FROM customers WHERE country = 'UK' ORDER BY company_name",
            "SELECT c.country, COUNT(*) AS n FROM customers c GROUP BY c.country ORDER BY n DESC",
            "WITH t AS (SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id) SELECT * FROM t",
            "SELECT customer_id, RANK() OVER (ORDER BY freight DESC) FROM orders",
            "SELECT e.first_name FROM employees e WHERE e.reports_to = (SELECT employee_id FROM employees WHERE last_name = 'Fuller')",
            "SELECT * FROM (SELECT order_id FROM orders) sub",
            "SELECT order_id FROM public.orders;",
            "SELECT product_name FROM products UNION SELECT company_name FROM shippers",
            "SELECT EXTRACT(YEAR FROM order_date) AS y, COUNT(*) FROM orders GROUP BY 1",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                db.validate_query(sql, SCOPE)


class BlocksWritesAndSideEffects(unittest.TestCase):
    def test_blocked(self):
        queries = {
            "delete": "DELETE FROM orders",
            "update": "UPDATE products SET unit_price = 0",
            "insert": "INSERT INTO shippers (shipper_id, company_name) VALUES (99, 'x')",
            "drop": "DROP TABLE customers",
            "stacked statement": "SELECT 1; DROP TABLE orders",
            "writable CTE": "WITH gone AS (DELETE FROM orders RETURNING *) SELECT COUNT(*) FROM gone",
            "select into": "SELECT order_id INTO scratch FROM orders",
            "sleep": "SELECT pg_sleep(30)",
            "file read": "SELECT pg_read_file('/etc/passwd')",
            "not sql": "this is not sql at all (",
            "empty": "",
        }
        for label, sql in queries.items():
            with self.subTest(label):
                rejected(sql, scope=None)  # must hold even with no scope configured


class EnforcesSemanticLayerScope(unittest.TestCase):
    def test_table_outside_scope(self):
        self.assertIn("suppliers", rejected("SELECT company_name FROM suppliers"))

    def test_system_catalogs(self):
        rejected("SELECT table_name FROM information_schema.tables")
        rejected("SELECT tablename FROM pg_catalog.pg_tables")

    def test_column_outside_scope(self):
        self.assertIn("phone", rejected("SELECT phone FROM customers"))
        rejected("SELECT home_phone FROM employees")
        rejected("SELECT company_name FROM customers WHERE address LIKE '%Main%'")

    def test_select_star_on_a_table(self):
        rejected("SELECT * FROM employees")
        rejected("SELECT e.* FROM employees e")
        rejected("SELECT o.order_id, c.* FROM orders o JOIN customers c ON o.customer_id = c.customer_id")

    def test_table_functions(self):
        rejected("SELECT * FROM generate_series(1, 5)")

    def test_scope_is_optional(self):
        db.validate_query("SELECT phone FROM customers", scope=None)  # no scope -> only read-only checks


class RowLimit(unittest.TestCase):
    def limit_for(self, sql, limit=10):
        return db._with_row_limit(sql, db.validate_query(sql), limit)

    def test_adds_limit_when_missing(self):
        self.assertTrue(self.limit_for("SELECT order_id FROM orders;").endswith("LIMIT 10"))

    def test_keeps_existing_limit(self):
        self.assertEqual(self.limit_for("SELECT order_id FROM orders LIMIT 3"), "SELECT order_id FROM orders LIMIT 3")

    def test_trailing_comment_does_not_swallow_limit(self):
        sql = self.limit_for("SELECT order_id FROM orders -- all of them")
        self.assertTrue(sql.endswith("\nLIMIT 10"))


class SemanticLayerIsConsistent(unittest.TestCase):
    """The semantic layer is enforced as the allowed scope, so it must not contradict itself."""

    @classmethod
    def setUpClass(cls):
        cls.tables = json.loads((REPO_ROOT / "Table_Schema_A.json").read_text())["tables"]

    def test_ddl_columns_match_documented_columns(self):
        for table in self.tables:
            with self.subTest(table["name"]):
                ddl = sqlglot.parse_one(table["schema"], read="postgres")
                ddl_columns = {c.name.lower() for c in ddl.find_all(exp.ColumnDef)}
                documented = {c["name"].lower() for c in table["columns"]}
                self.assertEqual(ddl_columns, documented)

    def test_every_sample_query_passes_the_guardrail(self):
        for table in self.tables:
            for sample in table.get("sample_queries", []):
                with self.subTest(sample["user_input"]):
                    db.validate_query(sample["query"], SCOPE)


class AgentQueryTool(unittest.TestCase):
    """The tool the LLM actually calls. Rejections must come back as text the model can
    read and react to (that's what lets it self-correct), and must be recorded."""

    def setUp(self):
        self.captured, self.query_log = [], []
        self.tool = lib._make_run_sql_query_tool(self.captured, self.query_log, SCOPE)

    def test_write_attempt_is_rejected_and_logged(self):
        reply = self.tool("DELETE FROM orders")
        self.assertTrue(reply.startswith("Query rejected"))
        self.assertEqual([q["status"] for q in self.query_log], ["rejected"])
        self.assertEqual(self.captured, [])

    def test_rejection_message_tells_the_model_how_to_fix_it(self):
        self.assertIn("list the columns", self.tool("SELECT * FROM customers"))

    @unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL not set")
    def test_success_is_logged_and_captured(self):  # (db)
        reply = self.tool("SELECT COUNT(*) AS n FROM orders")
        self.assertIn("830", reply)
        self.assertEqual(self.query_log[0]["status"], "ok")
        self.assertEqual(len(self.captured), 1)


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL not set")
class AgainstTheDatabase(unittest.TestCase):
    def test_returns_dataframe_capped_at_limit(self):  # (db)
        df = db.run_select_query("SELECT order_id FROM orders", limit=5, scope=SCOPE)
        self.assertEqual(len(df), 5)

    def test_database_itself_refuses_writes(self):  # (db)
        """Backstop: if validation were bypassed, the READ ONLY transaction still stops a write.
        DELETE ... WHERE false changes nothing even if the backstop failed."""
        with mock.patch.object(db, "_with_row_limit", return_value="DELETE FROM orders WHERE false"):
            with self.assertRaises(psycopg2.errors.ReadOnlySqlTransaction):
                db.run_select_query("SELECT 1", scope=None)

    def test_connections_are_closed(self):  # (db)
        before = db.run_select_query("SELECT COUNT(*) FROM pg_stat_activity", scope=None).iat[0, 0]
        for _ in range(5):
            db.run_select_query("SELECT 1", scope=None)
        after = db.run_select_query("SELECT COUNT(*) FROM pg_stat_activity", scope=None).iat[0, 0]
        self.assertLessEqual(after, before + 1)


if __name__ == "__main__":
    unittest.main()
