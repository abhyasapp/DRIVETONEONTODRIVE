#!/usr/bin/env python3
"""Content-word identity + answer normalization + quality scoring."""

import hashlib
import json
import re
import unicodedata
from typing import Sequence

from rapidfuzz import fuzz


STOPWORDS = frozenset("""
a an the
is are was were be been being am
do does did doing done
will would shall should can could may might must
have has had having
of in on at to for with by from into onto upon about
and or but so yet if then than
that this these those
it its they them their theirs
he she him her his hers
we us our you your i me my
""".split())


_WS_RE        = re.compile(r"\s+", re.UNICODE)
_INVISIBLE_RE = re.compile("[\u00ad\u200b\u200c\u200d\u200e\u200f\u2060\ufeff]")
_TOKEN_RE     = re.compile(r"[a-z0-9]+")

_PUNCT_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2032": "'", "\u2035": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2033": '"', "\u2036": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2026": "...",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ",
})


def _canonical(s):
    if s is None:
        return ""
    s = unicodedata.normalize("NFC", str(s))
    s = _INVISIBLE_RE.sub("", s)
    s = s.translate(_PUNCT_MAP).lower()
    out = []
    for ch in s:
        if ch.isspace():
            out.append(" ")
        elif ch.isalnum() or ch in "+-*/=<>^%~":
            out.append(ch)
    return _WS_RE.sub(" ", "".join(out)).strip()


def content_words(stem):
    tokens = _TOKEN_RE.findall(_canonical(stem))
    return frozenset(t for t in tokens if t not in STOPWORDS)


def _sorted_options(options):
    if not options:
        return ()
    return tuple(sorted(_canonical(o) for o in options))


def _strip_question_number(stem):
    text = str(stem or "")
    match = re.match(r"^\s*\d+\s*[.)]\s*", text)
    return text[match.end():] if match else text


def legacy_identity(stem, options):
    """Return the pre-v2 identity hash for safe one-time migration/matching."""
    # Preserve compatibility with the old content identity, which hashed the
    # stopword-stripped words without removing a leading printed question number.
    payload = {"cw": sorted(content_words(stem)), "opts": _sorted_options(options)}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def source_question_key(question_number, stem, options):
    """Stable source key; source number is authoritative when it exists.

    Text and choices often change after a correction is adopted. Including a
    content hash in a numbered key would prevent the same source item from
    matching its adopted edit on the next sync.
    """
    number = str(question_number).strip() if question_number is not None else ""
    if number:
        return f"n:{number}"
    return f"i:{legacy_identity(_strip_question_number(stem), options)}"


def source_location_key(book_name, subtopic):
    """Stable source-file scope, independent of a rotating Drive file ID."""
    payload = [_canonical(book_name), _canonical(subtopic)]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


IDENTITY_VERSION = 2


def identity(stem, options):
    """High-precision exact identity; fuzzy variants go to human review.

    The old identity used a set of stopword-stripped stem words, so word order
    and repetition vanished (for example, two different statements could hash
    as identical). Canonical full text preserves meaning-bearing order while
    remaining insensitive to case, Unicode punctuation and whitespace.
    Options remain an unordered canonical set to tolerate source option order;
    answer agreement is checked independently before merge.
    """
    canonical_stem = _canonical(_strip_question_number(stem))
    canonical_stem = re.sub(r"\s+([,.;:!?%])", r"\1", canonical_stem)
    payload = {
        "stem": canonical_stem,
        "opts": _sorted_options(options),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


_LETTER_HYPHEN_RE = re.compile(r"(?<=[a-z])-(?=[a-z])")
_SPACED_HYPHEN_RE = re.compile(r"\s*-\s*")


def _answer_text(s):
    """Canonical answer text, keeping symbols that carry meaning.

    Unlike _canonical (used for the stem hash, where dropping symbols keeps
    identity stable), an answer must keep "\u00b0" and "\u221a": "40\u00b0" is not
    "40" and "l/\u221a2" is not "l/2". Non-ASCII characters outside letters and
    digits therefore survive.
    """
    text = unicodedata.normalize("NFC", str(s if s is not None else ""))
    text = _INVISIBLE_RE.sub("", text).translate(_PUNCT_MAP).lower()
    out = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
        elif ch.isalnum() or ch in "+-*/=<>^%~" or ord(ch) > 127:
            out.append(ch)
    return _WS_RE.sub(" ", "".join(out)).strip()


def canonical_answer(s):
    """Answer text without spaces or typographic hyphens.

    "back-sight", "back - sight" and "back sight" are the same answer, so a
    hyphen joining two letters is dropped. Numeric hyphens are kept: "5-10%"
    is a range, not the number "510".
    """
    text = _SPACED_HYPHEN_RE.sub("-", _answer_text(s))
    return _LETTER_HYPHEN_RE.sub("", text).replace(" ", "")


def answer_identity(correct_text=None, correct_index=None, options=None):
    """Return a verified canonical answer, or a sentinel for unsafe answer data."""
    has_text = correct_text is not None and bool(str(correct_text).strip())
    has_index = correct_index is not None
    valid_index = (
        isinstance(correct_index, int) and not isinstance(correct_index, bool)
        and isinstance(options, (list, tuple))
        and 0 <= correct_index < len(options)
    )
    if has_index and not valid_index:
        return "__answer_conflict__"
    indexed_answer = canonical_answer(options[correct_index]) if valid_index else None
    if has_text:
        text_answer = canonical_answer(correct_text)
        if indexed_answer is not None and text_answer != indexed_answer:
            return "__answer_conflict__"
        return text_answer
    if indexed_answer is not None:
        return indexed_answer
    return "__unanswered__"


_ANSWER_LETTERS_RE = re.compile(r"[a-z]+")


def _within_edits(a, b, limit):
    """True when the Levenshtein distance between a and b is at most limit."""
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(a) + 1))
    for j, right in enumerate(b, start=1):
        current = [j]
        for i, left in enumerate(a, start=1):
            cost = 0 if left == right else 1
            current.append(min(current[i - 1] + 1, previous[i] + 1, previous[i - 1] + cost))
        previous = current
    return previous[len(a)] <= limit


def answers_equivalent(key_a, key_b):
    """True when two canonical answers mean the same thing, typos included.

    The identity keys are already punctuation-free, so what remains is either a
    real difference or a misspelling from the source books ("Gnatt chart" vs
    "Gantt chart", "perfomance" vs "performance"). Only that last class is
    treated as equal:

    * everything that is not a letter must match exactly — digits, signs and
      non-ASCII symbols alike — so "5-10%" is never "10-15%", "2" is never
      "20", and "2/5mr\u00b2" is never "2/5mr";
    * the letters must start and end with the same character, so "increase" is
      never "decrease" however close they look;
    * words shorter than five letters must match exactly;
    * at most one wrong character up to eight letters, two from nine letters.

    Explanations, images and quality scores are never part of the comparison.
    """
    if not key_a or not key_b or key_a.startswith("__") or key_b.startswith("__"):
        return False
    if key_a == key_b:
        return True
    if _ANSWER_LETTERS_RE.sub("", key_a) != _ANSWER_LETTERS_RE.sub("", key_b):
        return False
    letters_a = "".join(_ANSWER_LETTERS_RE.findall(key_a))
    letters_b = "".join(_ANSWER_LETTERS_RE.findall(key_b))
    if letters_a == letters_b:
        return True
    if len(letters_a) < 5 or len(letters_b) < 5:
        return False
    if letters_a[0] != letters_b[0] or letters_a[-1] != letters_b[-1]:
        return False
    allowed = 2 if max(len(letters_a), len(letters_b)) >= 9 else 1
    return _within_edits(letters_a, letters_b, allowed)


# From here up, a merge a reviewer clicks by hand is allowed. Merges made
# without a human in the loop stay on answers_equivalent only.
MERGE_SIMILARITY = 0.80


def _edit_distance(a, b):
    """Levenshtein distance between two canonical strings."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current.append(min(current[j - 1] + 1, previous[j] + 1,
                               previous[j - 1] + cost))
        previous = current
    return previous[len(b)]


def answers_similarity(key_a, key_b):
    """How alike two canonical answers are: 1.0 identical, 0.0 unrelated."""
    if not key_a or not key_b or key_a.startswith("__") or key_b.startswith("__"):
        return 0.0
    span = max(len(key_a), len(key_b))
    if not span:
        return 0.0
    return 1.0 - (_edit_distance(key_a, key_b) / span)


def answers_mergeable(key_a, key_b, threshold=MERGE_SIMILARITY):
    """True when a reviewer may merge these two answers.

    A typo is the same answer (answers_equivalent). Past that, a merge a
    person clicks is allowed while the answers are still at least 80% alike,
    so "2-3% of estmated amount" and "2-3% of estimated amount" (94%) can be
    joined by hand. Nothing below the threshold passes, and the reviewer sees
    the percentage and both answers before choosing.
    """
    if answers_equivalent(key_a, key_b):
        return True
    if not key_a or not key_b or key_a.startswith("__") or key_b.startswith("__"):
        return False
    # Two answers that differ only in the middle are often opposites
    # ("increase"/"decrease", 87% alike), so first and last must match too.
    if key_a[0] != key_b[0] or key_a[-1] != key_b[-1]:
        return False
    return answers_similarity(key_a, key_b) >= threshold


def compute_quality_score(explanation, options, text_len):
    score = 0.0
    if explanation and str(explanation).strip():
        score += 20.0
    n = len(options or [])
    if   n == 4: score += 10.0
    elif n == 3: score += 5.0
    elif n >= 5: score += 8.0
    if 40 <= (text_len or 0) <= 200:
        score += 3.0
    return score