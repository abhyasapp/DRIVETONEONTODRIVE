"""Offline checks for the identity fields an exported question carries.

The contract matters because the app sends the exported `id` back when a user
reports a question, and `reported_questions.qid` is a BIGINT: it must stay the
internal numeric id unless the operator explicitly runs --public-qn.
"""

import contextlib
import io
import unittest
from unittest import mock

import export.export_any_level as ex
from export.export_any_level import build_question


class ExportIdentityTests(unittest.TestCase):
    def test_default_export_keeps_the_internal_numeric_id(self):
        item = build_question(1, "12. What is the capital?", ["A", "B"], 0,
                              "because", None, qid=987654, stable_qn="G0011.110007")
        self.assertEqual(item["id"], 987654)
        self.assertEqual(item["public_qn"], "G0011.110007")

    def test_public_qn_switch_moves_the_readable_id_into_id(self):
        item = build_question(1, "What is the capital?", ["A", "B"], 0, "", None,
                              qid=987654, public_qn="G0011.110007",
                              stable_qn="G0011.110007")
        self.assertEqual(item["id"], "G0011.110007")
        self.assertEqual(item["public_qn"], "G0011.110007")

    def test_unassigned_question_still_exports(self):
        item = build_question(1, "What is the capital?", ["A"], 0, "", None,
                              qid=42)
        self.assertEqual(item["id"], 42)
        self.assertIsNone(item["public_qn"])

    def test_printed_question_number_is_not_repeated(self):
        item = build_question(7, "7. Explain curing.", ["A"], None, "", None,
                              qid=1)
        self.assertEqual(item["q"], "7. Explain curing.")
        self.assertIsNone(item["correct"], "an unanswered question stays null")

    def test_images_are_attached_only_when_present(self):
        self.assertNotIn("images", build_question(1, "q", [], None, "", None, qid=1))
        self.assertEqual(
            build_question(1, "q", [], None, "", ["data:x"], qid=1)["images"],
            ["data:x"])


class _StubCursor:
    """Just enough of a cursor to answer the level lookup."""

    def execute(self, *args, **kwargs):
        return None

    def fetchone(self):
        return (1, "Level 5")

    def fetchall(self):
        return []


def _row(qid, stable_qn_id):
    return ("1", "Chapter", "s1", "Subtopic", qid, "q", ["A"], 0, "", stable_qn_id)


class PublicQnExportGuardTests(unittest.TestCase):
    """--public-qn must not publish an internal DB id as the public identity."""

    def test_unassigned_row_aborts_a_public_qn_export(self):
        rows = [_row(11, "G0011.110007"), _row(12, None)]
        with mock.patch.object(ex, "fetch_rows", return_value=rows):
            with self.assertRaises(SystemExit) as caught:
                ex.export_level(_StubCursor(), "level5", False, False,
                                public_qn=True)
        message = str(caught.exception)
        self.assertIn("no stable public qn id", message)
        self.assertIn("12", message)

    def test_fully_assigned_row_exports_normally(self):
        rows = [_row(11, "G0011.110007")]
        with mock.patch.object(ex, "fetch_rows", return_value=rows), \
                contextlib.redirect_stdout(io.StringIO()):
            files, questions = ex.export_level(_StubCursor(), "level5", False,
                                               True, public_qn=True)
        self.assertEqual((files, questions), (0, 0), "dry run writes nothing")


if __name__ == "__main__":
    unittest.main()
