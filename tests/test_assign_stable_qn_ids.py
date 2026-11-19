"""Public qn ids are immutable: an owner keeps its id, newcomers move on.

The step used to abort the whole level as soon as a computed id was already
owned by another question. That happens whenever the export layout shifts under
older ids (the 50-per-file split was restored in 80af060) — question 46283 kept
`70024.110026` from an older layout while a brand-new question landed on the
sequence that id encodes. A whole level then lost its ids for the day, so the
newcomer now takes the next free sequence instead.
"""

import unittest

from export.assign_stable_qn_ids import plan_assignments, stable_qn_id_from_parts

CHAPTER, CODE = "002", "011"


def _row(qid, identity, stable="", chapter=CHAPTER, code=CODE, level="level5"):
    """One classified-question row, in the shape the assignment query returns."""
    return (level, chapter, code, "Subtopic", qid, 1, "q", ["A"], 0, "",
            None, identity, None, 0.0, stable, 0.9)


def _slot(qid, seq, part=1, chapter=CHAPTER, code=CODE):
    return (qid, chapter, code), (part, seq)


class SlotCollisionTests(unittest.TestCase):
    def test_newcomer_takes_the_next_free_sequence_instead_of_aborting(self):
        # 46283 owns the id that encodes file 1, sequence 2, although the layout
        # has since moved it to sequence 5.
        owned = stable_qn_id_from_parts("level5", CHAPTER, CODE, 1, 2)
        expected = stable_qn_id_from_parts("level5", CHAPTER, CODE, 1, 3)
        rows = [
            _row(46283, "a" * 64, stable=owned),
            _row(54537, "b" * 64),
        ]
        scope = dict([_slot(46283, 5), _slot(54537, 2)])

        pending, assigned, reused, skipped = plan_assignments(
            "level5", rows, scope, {owned: 46283})

        got = dict(pending)
        self.assertEqual(got[46283], owned, "the owner keeps its id")
        self.assertEqual(got[54537], expected, "the newcomer moves to sequence 3")
        self.assertEqual((assigned, reused, skipped), (1, 1, 0))

    def test_a_free_slot_keeps_the_id_that_encodes_it(self):
        base = stable_qn_id_from_parts("level5", CHAPTER, CODE, 1, 2)
        rows = [_row(11, "c" * 64)]
        scope = dict([_slot(11, 2)])

        pending, assigned, _, _ = plan_assignments("level5", rows, scope, {})

        self.assertEqual(pending, [(11, base)])
        self.assertEqual(assigned, 1)

    def test_two_newcomers_on_one_contended_slot_are_allocated_in_id_order(self):
        owned = stable_qn_id_from_parts("level5", CHAPTER, CODE, 1, 2)
        rows = [_row(54537, "b" * 64), _row(50001, "c" * 64)]
        scope = dict([_slot(54537, 2), _slot(50001, 2)])

        first, _, _, _ = plan_assignments("level5", rows, scope, {owned: 99})
        second, _, _, _ = plan_assignments("level5", rows, scope, {owned: 99})

        got = dict(first)
        self.assertEqual(got[50001], stable_qn_id_from_parts("level5", CHAPTER, CODE, 1, 3))
        self.assertEqual(got[54537], stable_qn_id_from_parts("level5", CHAPTER, CODE, 1, 4))
        self.assertEqual(first, second, "allocation is deterministic across runs")

    def test_an_identity_whose_own_id_is_held_by_another_identity_still_aborts(self):
        taken = stable_qn_id_from_parts("level5", CHAPTER, CODE, 1, 4)
        rows = [_row(7, "d" * 64, stable=taken)]
        scope = dict([_slot(7, 4)])

        with self.assertRaises(RuntimeError) as caught:
            plan_assignments("level5", rows, scope, {taken: 999})

        self.assertIn("Assignment collision", str(caught.exception))
        self.assertIn(taken, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
