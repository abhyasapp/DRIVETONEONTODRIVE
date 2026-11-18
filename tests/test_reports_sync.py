"""Offline checks for the reports sync (pull pagination, row mapping, push
acknowledgement) and the admin_apply input handling."""
import datetime
import json
import os
import unittest
from unittest import mock

from sync import admin_apply
from sync import sync_reports as sr
from utils import backend


def _r(i, **extra):
    base = {"id": f"r{i}", "qid": str(100 + i), "reportedAt": 1000 + i, "status": "open"}
    base.update(extra)
    return base


class PullTests(unittest.TestCase):
    def test_reads_every_page_and_uses_server_clock(self):
        data = [_r(i) for i in range(1, 2301)]

        def fake(action, **kw):
            self.assertEqual(action, "syncreportssince")
            rows = [r for r in data if r["reportedAt"] > kw["since"]][:kw["limit"]]
            return {"success": True, "reports": rows, "now": 777777}

        with mock.patch.object(backend, "call", fake):
            out, mark, complete = sr.fetch_all(0, limit=1000)
        self.assertEqual(len(out), 2300)
        self.assertTrue(complete)
        self.assertEqual(mark, 777777)

    def test_status_change_of_an_old_report_is_not_lost(self):
        old = _r(1, status="resolved", resolvedAt="2026-11-18T10:00:00Z")
        with mock.patch.object(backend, "call",
                               lambda a, **k: {"success": True, "reports": [old], "now": 5}):
            out, _mark, _c = sr.fetch_all(0)
        self.assertEqual(out[0]["status"], "resolved")
        self.assertGreater(sr._stamp(old), old["reportedAt"])

    def test_page_cap_keeps_watermark_at_newest_read(self):
        def fake(action, **kw):
            since = kw["since"]
            rows = [{"id": f"x{since}-{i}", "reportedAt": since + 1 + i} for i in range(kw["limit"])]
            return {"success": True, "reports": rows, "now": 10 ** 9}

        with mock.patch.object(backend, "call", fake):
            out, mark, complete = sr.fetch_all(0, limit=4, max_pages=3)
        self.assertFalse(complete)
        self.assertLess(mark, 10 ** 9)


class RowTests(unittest.TestCase):
    def test_to_row_normalises_types(self):
        row = sr.to_row({"id": 7, "qid": "42", "fileId": "f", "optionsSnapshot": ["a", "b"],
                         "correctIndex": "1", "chosenIndex": "", "resolvedAt": "",
                         "corrected": 1, "correctedSnapshot": {"q": "x"}})
        self.assertEqual(row[0], "7")
        self.assertEqual(row[2], 42)
        self.assertEqual(json.loads(row[5]), ["a", "b"])
        self.assertEqual((row[6], row[7]), (1, None))
        self.assertIsNone(row[13])
        self.assertIs(row[15], True)
        self.assertEqual(json.loads(row[16]), {"q": "x"})
        self.assertEqual(row[12], "open")          # missing status defaults to open


class FakeCur:
    def __init__(self, rows=None):
        self.rows, self.executed = rows or [], []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


class PushTests(unittest.TestCase):
    def test_pending_serialises_resolution(self):
        when = datetime.datetime(2026, 11, 18, 10, 0, tzinfo=datetime.timezone.utc)
        cur = FakeCur([("r1", "resolved", when, "console", True, '{"q":1}')])
        items = sr.pending(cur)
        self.assertEqual(items[0]["resolvedAt"], when.isoformat())
        self.assertTrue(items[0]["corrected"])

    def test_only_acknowledged_reports_are_cleared(self):
        cur = FakeCur()
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        reply = {"success": True, "updated": ["a"], "missing": ["b"], "failed": ["c"]}
        with mock.patch.object(backend, "call", lambda *a, **k: reply):
            done, status = sr.push(cur, items)
        self.assertEqual((done, status), (["a", "b"], "ok"))
        self.assertEqual(cur.executed[0][1], (["a", "b"],))

    def test_missing_backend_endpoint_keeps_everything_pending(self):
        cur = FakeCur()

        def boom(*a, **k):
            raise backend.UnknownAction("unknown action")

        with mock.patch.object(backend, "call", boom):
            done, status = sr.push(cur, [{"id": "a"}])
        self.assertEqual((done, status), ([], "endpoint-missing"))
        self.assertEqual(cur.executed, [])

    def test_nothing_pending_makes_no_call(self):
        with mock.patch.object(backend, "call", side_effect=AssertionError("called")):
            self.assertEqual(sr.push(FakeCur(), []), ([], "nothing"))

    def test_batches_are_capped(self):
        sizes = []

        def fake(action, **kw):
            sizes.append(len(kw["items"]))
            return {"success": True, "updated": [i["id"] for i in kw["items"]]}

        with mock.patch.object(backend, "call", fake):
            sr.push(FakeCur(), [{"id": str(i)} for i in range(450)])
        self.assertEqual(sizes, [200, 200, 50])


class BackendClientTests(unittest.TestCase):
    def _resp(self, code=200, body=None):
        r = mock.MagicMock(status_code=code)
        r.json.return_value = body
        return r

    def test_unknown_action_is_its_own_error(self):
        env = {"ABHYAS_APPS_SCRIPT_URL": "https://x", "ABHYAS_SYNC_KEY": "k"}
        with mock.patch.dict(os.environ, env), mock.patch(
                "utils.backend.requests.post",
                return_value=self._resp(body={"success": False, "error": "Unknown action: x"})):
            with self.assertRaises(backend.UnknownAction):
                backend.call("resolvereports")

    def test_rejected_key_and_missing_config(self):
        env = {"ABHYAS_APPS_SCRIPT_URL": "https://x", "ABHYAS_SYNC_KEY": "k"}
        with mock.patch.dict(os.environ, env), mock.patch(
                "utils.backend.requests.post", return_value=self._resp(code=403)):
            with self.assertRaises(backend.BackendError):
                backend.call("a")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(backend.BackendError):
                backend.call("a")


class AdminApplyInputTests(unittest.TestCase):
    def _args(self, **kw):
        return mock.MagicMock(env=kw.get("env"), file=kw.get("file"))

    def test_reads_json_from_environment(self):
        with mock.patch.dict(os.environ, {"X": json.dumps([{"qn": "1"}])}):
            self.assertEqual(admin_apply.load_items(self._args(env="X")), [{"qn": "1"}])

    def test_rejects_empty_nonlist_and_oversized_input(self):
        with mock.patch.dict(os.environ, {"X": " "}):
            with self.assertRaises(SystemExit):
                admin_apply.load_items(self._args(env="X"))
        with mock.patch.dict(os.environ, {"X": "{}"}):
            with self.assertRaises(SystemExit):
                admin_apply.load_items(self._args(env="X"))
        with mock.patch.dict(os.environ, {"X": json.dumps([{}] * 201)}):
            with self.assertRaises(SystemExit):
                admin_apply.load_items(self._args(env="X"))


if __name__ == "__main__":
    unittest.main()
