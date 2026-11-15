"""Keyword matching shared by both classifiers.

The keyword classifier (`classify_any_level`) and the LLM fallback
(`classify_with_gemini.keyword_fallback`) must agree on what a keyword match
means, so the rules live here once.

Three accuracy rules, each fixing a real distortion found in the keyword
files:

1. A keyword is normalized exactly like the question text. The old matcher
   lowercased keywords but left their punctuation intact while the text had
   punctuation stripped, so every keyword spelled with an apostrophe
   ("young's modulus", "hooke's law", "simpson's rule" - 29 of them) could
   never match anything.

2. A keyword made only of stopwords ("is", "it", "if then") is dropped. Such a
   keyword matches almost every question and used to hand its subtopic a free
   point.

3. Nested keywords count once, but a match is weighted by how specific it is.
   Keyword lists contain pairs like "stress" / "principal stress" (1,724 such
   pairs), so one mention of "principal stress" used to score 2 there while a
   precise match elsewhere scored 1. Now each distinct matched phrase scores its
   own number of content words, and a keyword lying inside an already counted
   phrase adds nothing: "principal stress" is worth 2, bare "stress" 1.
"""

import re
from functools import lru_cache

from dedupe.content_identity import STOPWORDS

_NORM_KEEP = re.compile(r"[^a-z0-9\s\-/]")
_WS = re.compile(r"\s+")


def normalize(text):
    """Lowercase, strip punctuation and collapse whitespace, space padded."""
    text = (text or "").lower()
    text = _NORM_KEEP.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return " " + text + " "


def _content_tokens(text):
    return [t for t in normalize(text).split() if t not in STOPWORDS]


@lru_cache(maxsize=1024)
def _prepare(words):
    """(normalized, original) pairs, deduped, meaningless keywords dropped."""
    out = []
    seen = set()
    for word in words:
        original = str(word).strip()
        if not original:
            continue
        norm = normalize(original).strip()
        if not norm or norm in seen:
            continue
        if not _content_tokens(norm):
            continue          # "is", "it", "of the" - no evidence at all
        seen.add(norm)
        out.append((norm, original))
    return tuple(out)


def prepare_keywords(words):
    """Cached, normalized keyword list for one subtopic."""
    return _prepare(tuple(words or ()))


def matched_keywords(q_norm, keywords):
    """Score one subtopic against a normalized question.

    Every distinct matched phrase contributes its own specificity (its count of
    content words), so "principal stress" outweighs bare "stress" while a
    keyword nested inside it adds nothing. Returns (score, hits), with `hits`
    holding the original spellings of the phrases that scored. Both token
    boundaries are honoured, so "stress" does not match "stressed".
    """
    spans = []
    for norm, original in keywords:
        weight = len(_content_tokens(norm)) or 1
        needle = f" {norm} "
        start = q_norm.find(needle)
        while start >= 0:
            # Record the keyword itself, not the padding spaces: otherwise two
            # adjacent keywords ("opc cement") would look like one overlap.
            spans.append((start + 1, start + len(needle) - 1, weight, original))
            start = q_norm.find(needle, start + 1)
    if not spans:
        return 0, []

    # Longest phrase first at a shared start, so "plain scale" claims the
    # region before the bare "scale" inside it can.
    spans.sort(key=lambda span: (span[0], -span[1]))
    score = 0
    covered_to = -1
    hits = []
    for start, end, weight, original in spans:
        if start >= covered_to:
            score += weight
            covered_to = end
            hits.append(original)
        else:
            # Inside (or a repeat of) a phrase already counted: same mention.
            covered_to = max(covered_to, end)
    return score, hits
