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


def canonical_answer(s):
    return _canonical(s).replace(" ", "")


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