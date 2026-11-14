"""Offline checks for qid resolution and correction-backlog retry.

    python -m unittest tests.test_corrections

No database or network is used: the cursor is a tiny in-memory fake and the
HTTP client is mocked.
"""

import unittest
from unittest import mock

from sync import apply_corrections as ac


class FakeCursor:
    def __init__(self, questions=None, backlog=None):
        self.questions = questions or {}
        self.backlog = list(backlog or [])
        self.deleted = []
        self._result = None

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        if "FROM questions q" in text and "WHERE q.id=%s" in text:
            self._result = self.questions.get(params[0])
        elif "SELECT correction, attempts FROM correction_backlog" in text:
            self._result = list(self.backlog[:params[0]])
        elif text.startswith("DELETE FROM correction_backlog"):
            self.deleted.append(params)
            self.backlog = [r for r in self.backlog if (r[0].get("uid"), r[0].get("correctedAt")) != params]
            self._result = None
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []


def question_row(qid, canonical=None):
    return (qid, f"q{qid}", [], 1, f"n:{qid}", None, "fileA", 7, canonical)


class QidResolutionTests(unittest.TestCase):
    def test_qid_direct(self):
        cur = FakeCursor({42: question_row(42)})
        row, why = ac.find_by_qid(cur, 42)
        self.assertEqual(why, "qid")
        self.assertEqual(row[0], 42)

    def test_qid_follows_canonical_hop(self):
        cur = FakeCursor({1: question_row(1, 2), 2: question_row(2)})
        row, why = ac.find_by_qid(cur, 1)
        self.assertEqual(why, "qid")
        self.assertEqual(row[0], 2)

    def test_qid_missing_and_loop(self):
        self.assertEqual(ac.find_by_qid(FakeCursor(), ""), (None, "qid_missing"))
        self.assertEqual(ac.find_by_qid(FakeCursor(), 999), (None, "qid_not_found"))

        cur = FakeCursor({1: question_row(1, 2), 2: question_row(2, 1)})
        self.assertEqual(ac.find_by_qid(cur, 1), (None, "qid_canonical_loop"))


class BacklogRetryTests(unittest.TestCase):
    def test_retry_applies_and_reports(self):
        cur = FakeCursor(backlog=[({"uid": "u1", "correctedAt": "t1"}, 2)])
        with mock.patch.object(ac, "process_correction", return_value=(42, "applied_qid", None)):
            stats = ac.retry_backlog(cur, 100)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["failed"], 0)

    def test_retry_removes_stale_already_applied_row(self):
        cur = FakeCursor(backlog=[({"uid": "u1", "correctedAt": "t1"}, 1)])
        with mock.patch.object(ac, "process_correction", return_value=(None, "already_applied", None)):
            stats = ac.retry_backlog(cur, 100)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(cur.deleted, [("u1", "t1")])

    def test_retry_keeps_missing_source_queued(self):
        cur = FakeCursor(backlog=[({"uid": "u1", "correctedAt": "t1"}, 1)])
        with mock.patch.object(ac, "process_correction", return_value=(None, "not_found_in_source_file", None)):
            stats = ac.retry_backlog(cur, 100)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["applied"], 0)

    def test_retry_reports_failures(self):
        cur = FakeCursor(backlog=[({"uid": "u1", "correctedAt": "t1"}, 1)])
        with mock.patch.object(ac, "process_correction", return_value=(None, None, "boom")):
            stats = ac.retry_backlog(cur, 100)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["errors"], [("u1", "boom")])

    def test_retry_skips_the_current_batch(self):
        cur = FakeCursor(backlog=[({"uid": "u1", "correctedAt": "t1"}, 1)])
        with mock.patch.object(ac, "process_correction") as process:
            stats = ac.retry_backlog(cur, 100, skip_keys={("u1", "t1")})
        process.assert_not_called()
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(stats["skipped"], 0)


class AnswerValidationTests(unittest.TestCase):
    def test_valid_indexes_are_accepted(self):
        self.assertIsNone(ac.validate_after_answer(["A", "B"], 0))
        self.assertIsNone(ac.validate_after_answer(["A", "B"], 1))

    def test_missing_or_out_of_range_answer_is_rejected(self):
        self.assertEqual(ac.validate_after_answer([], 0), "invalid_after_options")
        self.assertEqual(ac.validate_after_answer(["A"], None), "invalid_after_correct_index")
        self.assertEqual(ac.validate_after_answer(["A"], True), "invalid_after_correct_index")
        self.assertEqual(ac.validate_after_answer(["A"], 1), "invalid_after_correct_index")


class FetchRetryTests(unittest.TestCase):
    @staticmethod
    def response(status, payload=None):
        r = mock.Mock()
        r.status_code = status
        r.headers = {"Content-Type": "application/json"}
        r.text = ""
        r.json.return_value = payload or {"success": True}
        return r

    def fetch(self, responses):
        with mock.patch.object(ac, "APPS_URL", "https://example.test/api"), \
             mock.patch.object(ac, "SYNC_KEY", "test-key"), \
             mock.patch.object(ac.time, "sleep"), \
             mock.patch.object(ac.requests, "post", side_effect=responses) as post:
            result = ac.fetch_corrections(0)
        return result, post

    def test_retries_a_transient_500(self):
        result, post = self.fetch([
            self.response(503),
            self.response(200, {"success": True, "corrections": []}),
        ])
        self.assertTrue(result["success"])
        self.assertEqual(post.call_count, 2)

    def test_retries_a_timeout(self):
        result, post = self.fetch([
            ac.requests.exceptions.Timeout(),
            self.response(200, {"success": True, "corrections": []}),
        ])
        self.assertTrue(result["success"])
        self.assertEqual(post.call_count, 2)

    def test_does_not_retry_bad_key(self):
        with self.assertRaises(ac.BackendError):
            self.fetch([self.response(401)])


if __name__ == "__main__":
    unittest.main()
