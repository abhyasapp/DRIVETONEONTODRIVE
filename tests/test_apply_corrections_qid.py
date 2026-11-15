"""Offline tests for matching corrections by stable question id (no database)."""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://offline/test")

from sync.apply_corrections import find_match, match_by_qid  # noqa: E402

# id, text, options, file_ref_id, source_key, corrected_at, file_id, chapter_id, canonical_id
ROWS = {
    10: (10, "old", ["a"], 1, "n:3", None, "F", 7, None),
    11: (11, "dup", ["a"], 1, None, None, "F", 7, 10),   # merged into 10 by dedupe
    20: (20, "loop", ["a"], 1, None, None, "F", 7, 21),
    21: (21, "loop", ["a"], 1, None, None, "F", 7, 20),
}


class FakeCursor:
    def execute(self, sql, params):
        self.key = params[0]

    def fetchone(self):
        return ROWS.get(self.key)


class PublicIdCursor(FakeCursor):
    """Answers the public-id lookup, then the by-id lookup it resolves to."""

    PUBLIC = {"g0011.110001": 10, "50011.110001": 10, "5002mixedqn10001": 10}

    def execute(self, sql, params):
        self.sql = sql
        self.key = params[0]

    def fetchone(self):
        if "stable_qn_id" in getattr(self, "sql", ""):
            return (PublicIdCursor.PUBLIC.get(str(self.key).lower()),)
        return ROWS.get(self.key)


class QidMatchTests(unittest.TestCase):
    def setUp(self):
        self.cur = FakeCursor()

    def test_direct_hit(self):
        self.assertEqual(match_by_qid(self.cur, 10)[0], 10)

    def test_follows_canonical_to_the_keeper(self):
        self.assertEqual(match_by_qid(self.cur, 11)[0], 10)

    def test_canonical_cycle_terminates(self):
        self.assertIsNone(match_by_qid(self.cur, 20))

    def test_unknown_or_garbage_id_returns_none(self):
        self.assertIsNone(match_by_qid(self.cur, 999))
        self.assertIsNone(match_by_qid(self.cur, "abc"))
        self.assertIsNone(match_by_qid(self.cur, None))

    def test_numeric_string_id_is_accepted(self):
        self.assertEqual(match_by_qid(self.cur, "10")[0], 10)

    def test_public_qn_id_resolves_case_insensitively(self):
        # The export writes "G0011.110001"; the app stores "g0011.110001".
        self.assertEqual(match_by_qid(PublicIdCursor(), "G0011.110001")[0], 10)
        self.assertEqual(match_by_qid(PublicIdCursor(), "g0011.110001")[0], 10)

    def test_other_published_shapes_resolve(self):
        self.assertEqual(match_by_qid(PublicIdCursor(), "50011.110001")[0], 10)
        self.assertEqual(match_by_qid(PublicIdCursor(), "5002mixedqn10001")[0], 10)

    def test_unknown_public_id_is_not_found(self):
        self.assertIsNone(match_by_qid(PublicIdCursor(), "G9999.999999"))

    def test_find_match_prefers_qid_over_content(self):
        row, why = find_match(self.cur, "99. unrelated", ["z"], "id", file_id="F", qid=11)
        self.assertEqual((row[0], why), (10, "qid"))


if __name__ == "__main__":
    unittest.main()
