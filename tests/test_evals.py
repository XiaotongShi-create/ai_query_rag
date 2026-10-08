"""Tests for how the eval harness grades results (no database or LLM needed)."""
import unittest

import pandas as pd

from evals.run_evals import results_match


class ResultComparison(unittest.TestCase):
    def test_order_and_column_names_are_ignored(self):
        gold = pd.DataFrame({"a": ["x", "y"], "b": [1, 2]})
        agent = pd.DataFrame({"total": [2, 1], "name": ["y", "x"]})
        self.assertTrue(results_match(gold, agent))

    def test_extra_columns_are_tolerated(self):
        gold = pd.DataFrame({"name": ["x"]})
        agent = pd.DataFrame({"name": ["x"], "orders": [28]})
        self.assertTrue(results_match(gold, agent))

    def test_ranked_prefix_accepts_a_ranking_that_leads_with_the_gold_row(self):
        gold = pd.DataFrame({"country": ["USA"]})
        ranking = pd.DataFrame({"country": ["USA", "Germany"], "revenue": [245584.61, 230284.63]})
        self.assertTrue(results_match(gold, ranking, ranked_prefix=True))
        self.assertFalse(results_match(gold, ranking))  # strict mode still demands equal row counts

    def test_ranked_prefix_rejects_a_ranking_with_the_wrong_leader(self):
        gold = pd.DataFrame({"country": ["USA"]})
        wrong = pd.DataFrame({"country": ["Germany", "USA"], "revenue": [230284.63, 245584.61]})
        self.assertFalse(results_match(gold, wrong, ranked_prefix=True))

    def test_wrong_value_and_wrong_row_count_fail(self):
        gold = pd.DataFrame({"n": [408]})
        self.assertFalse(results_match(gold, pd.DataFrame({"n": [409]})))
        self.assertFalse(results_match(gold, pd.DataFrame({"n": [408, 408]})))


if __name__ == "__main__":
    unittest.main()
