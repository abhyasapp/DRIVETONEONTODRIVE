"""Offline checks for the hardening changes: corrections pagination, the
chapters-data upload (Shared Drive flags, in-place update, validation) and the
by-qn-id admin correction builder."""

import os
import tempfile
import unittest
from unittest import mock

from pipeline import run_pipeline as rp
from sync import apply_corrections as ac
from sync import admin_apply
from upload import upload_chapters_data as ucd
from review import dashboard as dash
from sync import sync_any_level as sal


def _page(n, start):
    return [{"uid": f"u{start + i}", "correctedAt": 1000 + start + i} for i in range(n)]


class CorrectionPaginationTests(unittest.TestCase):
    def test_reads_every_page_and_watermarks_server_clock(self):
        data = _page(500, 0) + _page(500, 500) + _page(120, 1000)
        calls = []

        def fake(since, limit=500):
            calls.append(since)
            rows = [c for c in data if c["correctedAt"] > since][:limit]
            return {"success": True, "corrections": rows, "now": 99999}

        with mock.patch.object(ac, "fetch_corrections", fake):
            out, mark, complete = ac.fetch_all_corrections(0, limit=500)
        self.assertEqual(len(out), 1120)
        self.assertTrue(complete)
        self.assertEqual(mark, 99999)
        self.assertGreater(len(calls), 2)

    def test_page_cap_never_skips_past_what_was_read(self):
        def fake(since, limit=500):
            rows = [{"uid": f"u{since}-{i}", "correctedAt": since + 1 + i} for i in range(limit)]
            return {"success": True, "corrections": rows, "now": 10 ** 9}

        with mock.patch.object(ac, "fetch_corrections", fake):
            out, mark, complete = ac.fetch_all_corrections(0, limit=5, max_pages=3)
        self.assertFalse(complete)
        self.assertLess(mark, 10 ** 9)          # NOT the server clock
        self.assertEqual(mark, max(c["correctedAt"] for c in out))

    def test_backend_failure_raises(self):
        with mock.patch.object(ac, "fetch_corrections",
                               lambda *a, **k: {"success": False, "error": "nope"}):
            with self.assertRaises(ac.BackendError):
                ac.fetch_all_corrections(0)

    def test_to_ms_accepts_epoch_and_iso(self):
        self.assertEqual(ac._to_ms(1700000000000), 1700000000000)
        self.assertEqual(ac._to_ms("1700000000000"), 1700000000000)
        self.assertEqual(ac._to_ms("2023-11-14T22:13:20Z"), 1700000000000)


class ChaptersUploadTests(unittest.TestCase):
    GOOD = "const CH_NAMES = {};\nconst DRIVE = {};\nconst ChapterData = {};\n" + "// pad\n" * 400

    def _file(self, text):
        fd, path = tempfile.mkstemp(suffix=".js")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_validate_accepts_complete_and_rejects_truncated(self):
        ucd.validate_local(self._file(self.GOOD))
        with self.assertRaises(ValueError):
            ucd.validate_local(self._file("const DRIVE = {"))
        with self.assertRaises(ValueError):
            ucd.validate_local(self._file("x" * 5000))

    def _run(self, listed, local_text):
        path = self._file(local_text)
        drive = mock.MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {"files": listed}
        drive.files.return_value.update.return_value.execute.return_value = {"id": "x"}
        drive.files.return_value.create.return_value.execute.return_value = {"id": "new"}
        with mock.patch.object(ucd, "LOCAL_FILE", path), \
                mock.patch.object(ucd, "client", lambda: drive), \
                mock.patch("sys.argv", ["upload_chapters_data"]):
            ucd.main()
        return drive

    def test_lists_with_shared_drive_flags_and_updates_in_place(self):
        drive = self._run([{"id": "ID1", "md5Checksum": "different"}], self.GOOD)
        kwargs = drive.files.return_value.list.call_args.kwargs
        self.assertTrue(kwargs["supportsAllDrives"])
        self.assertTrue(kwargs["includeItemsFromAllDrives"])
        drive.files.return_value.update.assert_called_once()
        self.assertEqual(drive.files.return_value.update.call_args.kwargs["fileId"], "ID1")
        drive.files.return_value.create.assert_not_called()

    def test_unchanged_content_is_skipped_without_state_files(self):
        path = self._file(self.GOOD)
        md5 = ucd.md5_of(path)
        drive = self._run([{"id": "ID1", "md5Checksum": md5}], self.GOOD)
        drive.files.return_value.update.assert_not_called()
        drive.files.return_value.create.assert_not_called()

    def test_creates_only_when_nothing_exists(self):
        drive = self._run([], self.GOOD)
        drive.files.return_value.create.assert_called_once()
        self.assertTrue(drive.files.return_value.create.call_args.kwargs["supportsAllDrives"])


class AdminApplyTests(unittest.TestCase):
    def test_run_rejects_bad_items_without_touching_the_db(self):
        conn = mock.MagicMock()
        cur = conn.cursor.return_value
        cur.fetchone.return_value = (True,)          # functions "installed"
        res = admin_apply.run([
            {"qn": "", "question": "x", "options": ["a", "b"], "correctIndex": 0},
            {"qn": "1", "question": "x", "options": ["a", "b"], "correctIndex": 5},
            {"qn": "1", "question": " ", "options": ["a", "b"], "correctIndex": 0},
        ], apply=False, conn=conn)
        self.assertEqual([r[1] for r in res], [False, False, False])
        conn.rollback.assert_called_once()
        self.assertFalse([c for c in cur.execute.call_args_list if "SAVEPOINT" in str(c)])


class PipelinePolicyTests(unittest.TestCase):
    def test_chapters_steps_are_fatal(self):
        self.assertIn("chapters", rp.FATAL_STEPS)
        self.assertIn("chapters_up", rp.FATAL_STEPS)

    def test_export_uses_stable_parts_by_default(self):
        export = {s[0]: s for s in rp.STEPS}["export"]
        if os.getenv("EXPORT_STABLE_PARTS", "1") != "0":
            self.assertIn("--stable-parts", export[3])


class SyncDeleteGuardTests(unittest.TestCase):
    def test_small_removals_pass(self):
        self.assertIsNone(sal.delete_guard_problem(2, 100, env={}))

    def test_mass_removal_is_blocked_by_count_or_share(self):
        self.assertIn("safety limit", sal.delete_guard_problem(30, 1000, env={}))
        self.assertIn("safety limit", sal.delete_guard_problem(8, 50, env={}))

    def test_explicit_override_and_nothing_deleted(self):
        self.assertIsNone(sal.delete_guard_problem(500, 600, env={"SYNC_ALLOW_MASS_DELETE": "1"}))
        self.assertIsNone(sal.delete_guard_problem(0, 0, env={}))

    def test_limits_are_configurable(self):
        env = {"SYNC_MAX_DELETE_FILES": "100", "SYNC_MAX_DELETE_PCT": "50"}
        self.assertIsNone(sal.delete_guard_problem(40, 100, env=env))


class DashboardGuardTests(unittest.TestCase):
    def test_loopback_json_post_is_allowed(self):
        h = {"Host": "127.0.0.1:8790", "Content-Type": "application/json",
             "Origin": "http://127.0.0.1:8790"}
        self.assertTrue(dash.request_allowed(h, post=True))
        self.assertTrue(dash.request_allowed({"Host": "localhost:8790"}))

    def test_rebinding_host_is_refused(self):
        self.assertFalse(dash.request_allowed({"Host": "evil.example.com:8790"}))

    def test_cross_site_and_form_posts_are_refused(self):
        base = {"Host": "127.0.0.1:8790"}
        self.assertFalse(dash.request_allowed(
            {**base, "Content-Type": "text/plain", "Origin": "https://evil.example"}, post=True))
        self.assertFalse(dash.request_allowed(
            {**base, "Content-Type": "application/json", "Origin": "https://evil.example"}, post=True))
        self.assertFalse(dash.request_allowed(
            {**base, "Content-Type": "application/x-www-form-urlencoded"}, post=True))


if __name__ == "__main__":
    unittest.main()
