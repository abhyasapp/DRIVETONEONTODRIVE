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


def identity(stem, options):
    payload = {
        "cw":   sorted(content_words(stem)),
        "opts": _sorted_options(options),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def canonical_answer(s):
    return _canonical(s).replace(" ", "")


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