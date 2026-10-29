"""Offline logic regression tests for duplicate detection and correction identity."""

import unittest

from dedupe.content_identity import identity, source_question_key, source_location_key
from dedupe.dedupe_exact import plan_deletions
from dedupe.near_candidates import find_candidate_pairs


class IdentityTests(unittest.TestCase):
    def test_identity_ignores_punctuation_case_spacing_and_printed_number(self):
        self.assertEqual(
            identity("12. What is the capital?", ["A", "B"]),
            identity("what IS the capital ?", ["B", "A"]),
        )

    def test_identity_preserves_word_order(self):
        self.assertNotEqual(identity("A is greater than B", []),
                            identity("B is greater than A", []))

    def test_numbered_source_key_survives_changed_source_content(self):
        self.assertEqual(source_question_key(14, "old stem", ["a", "b"]),
                         source_question_key(14, "corrected stem", ["x", "y"]))

    def test_unnumbered_source_key_is_content_derived(self):
        self.assertNotEqual(source_question_key(None, "old stem", ["a", "b"]),
                            source_question_key(None, "new stem", ["a", "b"]))

    def test_source_location_is_stable_under_case_and_spacing(self):
        self.assertEqual(source_location_key("Book  A", "Topic X"),
                         source_location_key("book a", " topic   x "))


class ExactMergePlanTests(unittest.TestCase):
    def row(self, qid, *, answer=None, index=None):
        return {"id": qid, "text": "Same stem", "options": ["A", "B"],
                "ct": answer, "ci": index, "quality_score": 1, "corrected_at": 0}

    def test_exact_plan_merges_only_answer_agreement(self):
        groups = {(1, "k"): [self.row(1, answer="A"), self.row(2, answer="A"),
                              self.row(3, answer="B"), self.row(4)]}
        plans, conflicts = plan_deletions(groups)
        self.assertEqual(conflicts, 1)
        self.assertEqual([(p[1], p[2]) for p in plans], [(1, 2)])

    def test_index_only_answer_is_supported_and_empty_answers_retained(self):
        groups = {(1, "k"): [self.row(1, index=0), self.row(2, answer="A"),
                              self.row(3, answer=None)]}
        plans, _ = plan_deletions(groups)
        self.assertEqual([(p[1], p[2]) for p in plans], [(1, 2)])


class FuzzyReviewTests(unittest.TestCase):
    def test_finds_similar_question_only_with_compatible_options(self):
        rows = [
            {"chapter_id": 1, "id": 1, "identity_key": "a", "text": "What is the capital of Nepal?", "options": ["Kathmandu", "Pokhara"]},
            {"chapter_id": 1, "id": 2, "identity_key": "b", "text": "What is Nepal's capital?", "options": ["Kathmandu", "Pokhara"]},
            {"chapter_id": 2, "id": 3, "identity_key": "c", "text": "What is the capital of Nepal?", "options": ["Kathmandu", "Pokhara"]},
        ]
        pairs = find_candidate_pairs(rows, min_stem=50, min_options=50)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][:3], (1, 1, 2))
        self.assertGreaterEqual(pairs[0][3], 80.0)
        self.assertEqual(pairs[0][4], 100.0)
        self.assertFalse(any(3 in pair[1:3] for pair in pairs))

    def test_empty_or_mismatched_options_do_not_create_fuzzy_candidate(self):
        rows = [
            {"chapter_id": 1, "id": 1, "identity_key": "a", "text": "A sufficiently long matching question stem", "options": []},
            {"chapter_id": 1, "id": 2, "identity_key": "b", "text": "A sufficiently long matching question stem", "options": []},
        ]
        self.assertEqual(find_candidate_pairs(rows, min_stem=50, min_options=0), [])


if __name__ == "__main__":
    unittest.main()
