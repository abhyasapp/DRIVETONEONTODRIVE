"""Offline checks for the pipeline's fail-closed policy and CI summary."""

import os
import tempfile
import unittest
from unittest import mock

from pipeline import run_pipeline as rp


class PipelineSafetyTests(unittest.TestCase):
    def test_stale_content_steps_are_fatal(self):
        self.assertIn("sync", rp.FATAL_STEPS)
        self.assertIn("corrections", rp.FATAL_STEPS)
        self.assertIn("export", rp.FATAL_STEPS)
        self.assertIn("upload", rp.FATAL_STEPS)

    def test_sync_is_retried_once_but_not_corrections(self):
        self.assertIn("sync", rp.RETRY_ONCE_STEPS)
        self.assertNotIn("corrections", rp.RETRY_ONCE_STEPS)

    def test_ci_summary_marks_failures(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "summary.md")
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": path}):
                rp.write_ci_summary(
                    [("sync", True, 1.2, "exit 0"),
                     ("export", False, 0.4, "exit 1")],
                    aborted=True,
                )
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        self.assertIn("**Result:** ABORTED", text)
        self.assertIn("| `sync` | OK |", text)
        self.assertIn("| `export` | FAILED |", text)


if __name__ == "__main__":
    unittest.main()
