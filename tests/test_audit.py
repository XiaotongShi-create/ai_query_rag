"""Tests for the audit trail. S3 is mocked, so these need no AWS access."""
import json
import unittest
from unittest import mock

import audit

OK = {"sql": "SELECT 1", "status": "ok", "error": None, "rows": 1}
BAD = {"sql": "SELECT nope", "status": "error", "error": "column nope does not exist", "rows": None}
BLOCKED = {"sql": "DELETE FROM orders", "status": "rejected", "error": "write", "rows": None}


class ReliabilitySignal(unittest.TestCase):
    def test_signals(self):
        self.assertEqual(audit.reliability_signal([OK]), "first_attempt")
        self.assertEqual(audit.reliability_signal([BAD, OK]), "after_retry")
        self.assertEqual(audit.reliability_signal([BAD, BLOCKED]), "all_attempts_failed")
        self.assertEqual(audit.reliability_signal([]), "no_query")


class Writes(unittest.TestCase):
    def logged(self, fn, *args, **kwargs):
        with mock.patch("audit.boto3.client") as client:
            error = fn(*args, **kwargs)
        put = client.return_value.put_object.call_args.kwargs
        return error, put["Key"], json.loads(put["Body"])

    def test_answer_record_is_complete_and_partitioned(self):
        error, key, record = self.logged(
            audit.log_answer, "abc123", session_id="s1", schema_type="Schema_Type_A",
            model="m", question="q", answer="a", query_log=[BAD, OK], latency_s=1.234,
        )
        self.assertIsNone(error)
        self.assertRegex(key, r"^audit/dt=\d{4}-\d{2}-\d{2}/abc123\.json$")
        self.assertEqual(record["n_queries"], 2)
        self.assertEqual(record["n_failed_queries"], 1)
        self.assertEqual(record["reliability_signal"], "after_retry")
        self.assertEqual(record["queries"][0]["error"], "column nope does not exist")

    def test_feedback_is_a_new_event_pointing_at_the_answer(self):
        error, key, record = self.logged(audit.log_feedback, "abc123", "s1", "down", "wrong year")
        self.assertIsNone(error)
        self.assertTrue(key.startswith("feedback/dt="))
        self.assertEqual(record["answer_event_id"], "abc123")
        self.assertNotEqual(record["event_id"], "abc123")  # the answer's record is never edited
        self.assertEqual((record["rating"], record["comment"]), ("down", "wrong year"))

    def test_logging_failure_returns_an_error_instead_of_raising(self):
        with mock.patch("audit.boto3.client") as client:
            client.return_value.put_object.side_effect = RuntimeError("AccessDenied")
            error = audit.log_answer("x", session_id="s", schema_type="t", model="m",
                                     question="q", answer="a", query_log=[], latency_s=0)
        self.assertIn("AccessDenied", error)


if __name__ == "__main__":
    unittest.main()
