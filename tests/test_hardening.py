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
    def test_build_correction_carries_qid_and_before_state(self):
        row = (42, "old?", ["a", "b"], 7, "n:3", None, "drv1", 5)
        corr = admin_apply.build_correction(
            row, {"question": "new?", "options": ["a", "b"], "correctIndex": 1}, "2026-01-01T00:00:00+00:00")
        self.assertEqual(corr["qid"], "42")
        self.assertEqual(corr["fileId"], "drv1")
        self.assertEqual(corr["before"]["question"], "old?")
        self.assertEqual(corr["after"]["correctIndex"], 1)

    def test_run_rejects_bad_items_without_touching_the_db(self):
        conn = mock.MagicMock()
        with mock.patch.object(admin_apply, "ensure_schema"):
            res = admin_apply.run([
                {"qn": "", "question": "x", "options": ["a", "b"], "correctIndex": 0},
                {"qn": "1", "question": "x", "options": ["a", "b"], "correctIndex": 5},
                {"qn": "1", "question": " ", "options": ["a", "b"], "correctIndex": 0},
            ], apply=False, conn=conn)
        self.assertEqual([r[1] for r in res], [False, False, False])
        conn.rollback.assert_called_once()


class PipelinePolicyTests(unittest.TestCase):
    def test_chapters_steps_are_fatal(self):
        self.assertIn("chapters", rp.FATAL_STEPS)
        self.assertIn("chapters_up", rp.FATAL_STEPS)

    def test_export_uses_stable_parts_by_default(self):
        export = {s[0]: s for s in rp.STEPS}["export"]
        if os.getenv("EXPORT_STABLE_PARTS", "1") != "0":
            self.assertIn("--stable-parts", export[3])


if __name__ == "__main__":
    unittest.main()
