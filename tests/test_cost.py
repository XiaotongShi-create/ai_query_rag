"""Tests for query-cost safeguards: the per-query cost limit and the per-question budget."""
import os
import unittest
from unittest import mock

from dotenv import load_dotenv

import db
import library as lib

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

SCOPE = None  # cost behaviour is independent of scope; scope has its own tests


def run_with_planner_cost(cost, budget=None, **kwargs):
    """Run db.run_select_query against a fake connection whose planner reports `cost`.
    Returns (result_or_exception, list_of_sql_sent_to_the_database)."""
    fake_conn = mock.MagicMock()
    cur = fake_conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = [[{"Plan": {"Total Cost": cost}}]]
    cur.description = [("n",)]
    cur.fetchmany.return_value = [(1,)]
    with mock.patch.dict(os.environ, {"DATABASE_URL": "postgres://fake"}), \
            mock.patch.object(db.psycopg2, "connect", return_value=fake_conn):
        try:
            outcome = db.run_select_query("SELECT 1", scope=SCOPE, budget=budget, **kwargs)
        except db.UnsafeQueryError as e:
            outcome = e
    sent = [call.args[0] for call in cur.execute.call_args_list]
    return outcome, sent


class PerQueryCostLimit(unittest.TestCase):
    def test_too_expensive_query_is_refused_and_never_executed(self):
        outcome, sent = run_with_planner_cost(2_442_516, max_query_cost=10_000)
        self.assertIsInstance(outcome, db.QueryTooExpensiveError)
        self.assertEqual(outcome.estimated_cost, 2_442_516)
        self.assertIn("filters", str(outcome))  # tells the model how to fix it
        self.assertTrue(any(s.startswith("EXPLAIN") for s in sent))
        self.assertFalse(any(s.startswith("SELECT 1") for s in sent), "the expensive query must not run")

    def test_cheap_query_runs(self):
        outcome, sent = run_with_planner_cost(25, max_query_cost=10_000)
        self.assertEqual(len(outcome), 1)
        self.assertTrue(any(s.startswith("SELECT 1") for s in sent))

    def test_the_limit_is_inclusive_of_the_exact_value(self):
        outcome, _ = run_with_planner_cost(10_000, max_query_cost=10_000)
        self.assertNotIsInstance(outcome, Exception)

    def test_limit_can_be_set_from_the_environment_default(self):
        self.assertGreater(db.MAX_QUERY_COST, 114.5 * 10)  # well above the costliest legitimate eval question


class PerQuestionBudget(unittest.TestCase):
    def test_query_count_is_capped(self):
        budget = db.QueryBudget(max_queries=2)
        budget.charge_attempt()
        budget.charge_attempt()
        with self.assertRaises(db.QueryBudgetExceededError):
            budget.charge_attempt()

    def test_total_cost_is_capped_across_queries(self):
        budget = db.QueryBudget(max_total_cost=100)
        first, _ = run_with_planner_cost(60, budget=budget)
        self.assertNotIsInstance(first, Exception)
        second, sent = run_with_planner_cost(60, budget=budget)  # 60 + 60 > 100
        self.assertIsInstance(second, db.QueryBudgetExceededError)
        self.assertFalse(any(s.startswith("SELECT 1") for s in sent), "over-budget query must not run")
        self.assertEqual(budget.cost_used, 60)  # a refused query spends nothing

    def test_rejected_queries_still_count_toward_the_attempt_cap(self):
        budget = db.QueryBudget(max_queries=2)
        for _ in range(2):
            outcome, _ = run_with_planner_cost(5_000_000, budget=budget, max_query_cost=10_000)
            self.assertIsInstance(outcome, db.QueryTooExpensiveError)
        third, _ = run_with_planner_cost(5, budget=budget)
        self.assertIsInstance(third, db.QueryBudgetExceededError)  # a retry loop is cut off

    def test_last_cost_is_recorded_for_logging(self):
        budget = db.QueryBudget()
        run_with_planner_cost(42, budget=budget)
        self.assertEqual(budget.last_cost, 42)

    def test_each_agent_gets_its_own_budget(self):
        # build_agent creates a fresh budget per agent, and the app builds one agent per question
        a, b = db.QueryBudget(), db.QueryBudget()
        a.charge_attempt()
        self.assertEqual((a.queries_used, b.queries_used), (1, 0))


class AgentToolReportsCostBlocks(unittest.TestCase):
    def setUp(self):
        self.query_log, self.budget = [], db.QueryBudget()
        self.tool = lib._make_run_sql_query_tool([], self.query_log, SCOPE, self.budget)

    def test_expensive_query_is_logged_with_its_own_status_and_message_goes_to_the_model(self):
        error = db.QueryTooExpensiveError("too much", 2_442_516)
        with mock.patch.object(db, "run_select_query", side_effect=error):
            reply = self.tool("SELECT COUNT(*) FROM customers, orders, order_details")
        self.assertTrue(reply.startswith("Query rejected"))
        self.assertEqual(self.query_log[0]["status"], "too_expensive")

    def test_budget_exhaustion_is_logged_with_its_own_status(self):
        with mock.patch.object(db, "run_select_query", side_effect=db.QueryBudgetExceededError("used up")):
            self.tool("SELECT 1")
        self.assertEqual(self.query_log[0]["status"], "over_budget")


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL not set")
class AgainstTheRealPlanner(unittest.TestCase):
    """Guards the calibration itself: if the data or the limit changes so that normal questions
    start being refused (or cross joins stop being refused), this fails."""

    def test_cross_join_is_refused_and_a_normal_query_is_not(self):  # (db)
        budget = db.QueryBudget()
        with self.assertRaises(db.QueryTooExpensiveError) as ctx:
            db.run_select_query("SELECT COUNT(*) FROM customers c, orders o, order_details od", budget=budget)
        self.assertGreater(ctx.exception.estimated_cost, db.MAX_QUERY_COST)
        df = db.run_select_query("SELECT COUNT(*) FROM orders WHERE shipped_date > required_date", budget=budget)
        self.assertEqual(int(df.iat[0, 0]), 37)
        self.assertLess(budget.last_cost, db.MAX_QUERY_COST / 10)


if __name__ == "__main__":
    unittest.main()
