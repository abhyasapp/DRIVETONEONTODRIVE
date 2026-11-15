"""Offline checks for the identity fields an exported question carries.

The contract matters because the app sends the exported `id` back when a user
reports a question, and `reported_questions.qid` is a BIGINT: it must stay the
internal numeric id unless the operator explicitly runs --public-qn.
"""

import unittest

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


if __name__ == "__main__":
    unittest.main()
