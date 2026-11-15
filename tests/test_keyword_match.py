"""Offline regression tests for keyword scoring.

Each test pins one of the distortions the shared matcher removes, so a future
change cannot quietly bring it back:

  * keywords spelled with an apostrophe never matching;
  * stopword-only keywords counting as evidence;
  * nested keywords double counting (`stress` / `principal stress`);
  * a specific phrase losing to a generic word, or a tie taking the first one.
"""

import unittest

from classify.keyword_match import matched_keywords, normalize, prepare_keywords
from classify.classify_any_level import classify_one
from classify.classify_with_gemini import keyword_fallback


class MatcherTests(unittest.TestCase):
    def score(self, text, keywords):
        return matched_keywords(normalize(text), prepare_keywords(keywords))

    def test_apostrophe_keyword_matches_the_normalized_question(self):
        # The text normalizes to " young s modulus ...", so an unnormalized
        # keyword "young's modulus" could never match it before.
        score, hits = self.score("What is Young's modulus of steel?",
                                 ["young's modulus"])
        self.assertGreater(score, 0)
        self.assertEqual(hits, ["young's modulus"])

    def test_nested_keywords_count_once(self):
        # One mention of "plain scale" must not also score for "scale" or for
        # the longer "scale of chord" phrase overlapping the same words.
        covering = self.score("Explain the plain scale of chord.", ["plain scale"])[0]
        nested = self.score("Explain the plain scale of chord.",
                            ["scale", "scale of chord", "plain scale"])[0]
        self.assertEqual(nested, covering)
        self.assertEqual(nested, 2, "'plain scale' is one two-word match")

    def test_two_distinct_mentions_both_count(self):
        combined = self.score("Compare plain scale with diagonal scale.",
                              ["plain scale", "diagonal scale"])[0]
        first = self.score("Compare plain scale with diagonal scale.",
                           ["plain scale"])[0]
        second = self.score("Compare plain scale with diagonal scale.",
                            ["diagonal scale"])[0]
        self.assertEqual(combined, first + second)

    def test_specific_phrase_outweighs_a_generic_word(self):
        specific = self.score("Condition for equilibrium in plastic analysis.",
                              ["plastic analysis"])[0]
        generic = self.score("Condition for equilibrium in plastic analysis.",
                             ["analysis"])[0]
        self.assertGreater(specific, generic)

    def test_stopword_only_keywords_are_dropped(self):
        self.assertEqual(self.score("What is it that the engineer does?",
                                    ["is", "it"]), (0, []))

    def test_keyword_needs_token_boundaries(self):
        self.assertEqual(self.score("A stressed beam is under load.", ["stress"])[0], 0)
        self.assertEqual(self.score("The stress is high.", ["stress"])[0], 1)

    def test_punctuated_keyword_matches_spaced_text(self):
        score, _ = self.score("In R.C.C. construction, what is the mix?", ["r.c.c"])
        self.assertGreater(score, 0)


class KeywordClassifierTests(unittest.TestCase):
    def run_one(self, text, keywords_by_code):
        subs = [(index, code) for index, code in
                enumerate(keywords_by_code, start=1)]
        kw = {code: prepare_keywords(words)
              for code, words in keywords_by_code.items()}
        return classify_one(1, "1", text, {"1": subs}, {"1": kw}, {"1": 99})

    def test_bare_word_inside_a_phrase_adds_no_evidence(self):
        # 1.2 has the generic "stress" as well as the phrase, but a single
        # mention of "principal stress" is one and the same piece of evidence.
        result = self.run_one(
            "What is principal stress?",
            {"1.2": ["stress", "principal stress"], "1.3": ["principal stress"]},
        )
        self.assertEqual(result[1], 99, "a genuine tie must stay unclassified")

    def test_specific_phrase_breaks_a_generic_tie(self):
        result = self.run_one(
            "Condition for equilibrium in plastic analysis.",
            {"1.6": ["plastic analysis"], "1.7": ["analysis"]},
        )
        self.assertEqual(result[1], 1)

    def test_clear_winner_is_classified(self):
        result = self.run_one(
            "The theodolite measures horizontal angles.",
            {"2.1": ["theodolite"], "2.2": ["contour"]},
        )
        self.assertEqual(result[1], 1)
        self.assertTrue(result[4])

    def test_no_match_falls_back_to_unclassified(self):
        result = self.run_one("Unrelated text about poetry.", {"2.1": ["theodolite"]})
        self.assertEqual(result[1], 99)
        self.assertFalse(result[4])


class FallbackTests(unittest.TestCase):
    def test_stopword_overlap_is_not_evidence(self):
        hints = {"1.1": ["of", "in", "the"]}
        self.assertEqual(keyword_fallback("What is the design of a beam in RCC?", [],
                                          hints, {"1.1"}), (None, 0.0))

    def test_specific_single_phrase_is_accepted(self):
        hints = {"2.11": ["simpson's rule"]}
        code, conf = keyword_fallback("State Simpson's rule for areas.", [],
                                      hints, {"2.11"})
        self.assertEqual(code, "2.11")
        self.assertGreater(conf, 0.0)

    def test_lone_generic_word_is_rejected(self):
        self.assertEqual(keyword_fallback("What is design?", [], {"1.1": ["design"]},
                                          {"1.1"}), (None, 0.0))

    def test_tie_between_subtopics_abstains(self):
        hints = {"1.1": ["cement"], "1.2": ["cement"]}
        self.assertEqual(keyword_fallback("Explain cement.", [], hints,
                                          {"1.1", "1.2"}), (None, 0.0))

    def test_two_generic_hits_are_enough(self):
        hints = {"1.1": ["cement", "concrete"]}
        code, _ = keyword_fallback("Cement concrete mix design.", [],
                                   hints, {"1.1"})
        self.assertEqual(code, "1.1")


if __name__ == "__main__":
    unittest.main()
