#!/usr/bin/env python3
"""
assign_stable_qn_ids.py
-----------------------

Assign the readable public qn id (`questions.stable_qn_id`) for classified
questions *after* classification/sorting and *before* export.

Purpose:
* This is the piece that turns the classified/sorted question set into a
  stable public identity that is the same across re-exports, reordering and
  corrections, as long as the question keeps its classified place.
* It does NOT assign ids during raw Drive -> Neon ingest. Ingest keeps
  `stable_qn_id` blank; this step fills it later.

Rules:
* Only fill rows that are classified and deduplicated
  (`canonical_id IS NULL`) and that belong to an exported classified
  subtopic. Unclassified rows and rows that are not in the export layout
  stay blank here.
* Same stable question identity keeps the same public qn id:
  - If the keeper already has a public id, reuse it across the whole duplicate
    group.
  - If a corrected-option revision of the same question already has a public
    id, that id is kept and reused for the same question identity.
* If multiple classified copies of the same stable identity still exist, keep
  the best one and assign/keep its public id. The rest are left without a
  public id in this run; the existing dedup machinery still owns merging or
  tombstoning them.
* Best keeper tie-break, in order:
  1. already has a public id
  2. latest corrected_at
  3. higher quality_score (fallback to classification confidence when present)
  4. lowest internal id
* Public qn id shape: LCCCSSSFQQQQ
  - L  : level token from `levels.label` first char (or literal fallback)
  - CCC: zero-padded chapter key
  - SSS: zero-padded exported subtopic code
  - F  : exported file scope index within the chapter/subtopic
  - QQQQ: zero-padded sequence within that file scope
* File scope `F` follows the exported file order used by
  `export_any_level.py`. Mixed-question exported parts are included when the
  question is classified into the mixedqn subtopic.
* If the assignment would create a duplicate public id, the step aborts the
  whole level with an error instead of silently overwriting a different
  question's public id. That makes collisions visible rather than hidden.

Public id scheme summary (for code review):
* Level token:
  - level5 -> 5
  - level7 -> 7
  - gk     -> G
  - anything else -> first character of the level label, uppercased, or "?"
* Chapter part: padded chapter_key to 3 digits.
* Subtopic part: padded exported subtopic code to 3 digits.
* File part: 1..N exported files for that chapter/subtopic.
* Sequence part: 1..M sequence inside that file.

Collision policy:
* Two different stable question identities may not share a public id.
* A corrected-option revision of the SAME question may reuse the same public id.
* If a revision would collide with a public id already owned by a DIFFERENT
  question identity, this step raises and stops, so the conflict is fixed
  explicitly instead of silently misapplied.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

from utils.db import connect as db_connect

load_dotenv()

# ---------------------------------------------------------------------------
# Level token mapping for the readable public qn id.
# ---------------------------------------------------------------------------

LEVEL_TOKEN = {
    "level5": "5",
    "level7": "7",
    "gk": "G",
}


# ---------------------------------------------------------------------------
# Padding helpers.
# ---------------------------------------------------------------------------

def _pad(s: str, n: int) -> str:
    return str(s).zfill(n)


# Resolved level tokens, cached so the fallback lookup is not repeated per question.
_LEVEL_TOKEN_CACHE: Dict[str, str] = {}


def level_token(level_key: str) -> str:
    """Return the one-character level token, consulting the DB at most once."""
    if level_key in LEVEL_TOKEN:
        return LEVEL_TOKEN[level_key]
    if level_key not in _LEVEL_TOKEN_CACHE:
        _LEVEL_TOKEN_CACHE[level_key] = _level_token_fallback(level_key)
    return _LEVEL_TOKEN_CACHE[level_key]


def stable_qn_id_from_parts(level_key: str, chapter_key: str, subtopic_code: str,
                            file_scope: int, seq: int) -> str:
    """Build one public qn id from classified/export scope parts."""
    l = level_token(level_key)
    c = _pad(chapter_key, 3)
    s = _pad(subtopic_code, 3)
    f = _pad(file_scope, 1)
    q = _pad(seq, 4)
    return f"{l}{c}{s}{f}{q}"


def _level_token_fallback(level_key: str) -> str:
    label = ""
    try:
        from utils.db import connect as _db_connect
        c2 = _db_connect(os.getenv("DATABASE_URL")).cursor()
        c2.execute("SELECT label FROM levels WHERE level_key = %s", (level_key,))
        row = c2.fetchone()
        if row:
            label = row[0] or ""
        c2.close()
    except Exception:
        label = ""
    first = (label or level_key or "?")[0]
    return str(first).upper()


# ---------------------------------------------------------------------------
# Exported file scope.
# ---------------------------------------------------------------------------

def build_export_scope_map(rows: List[Tuple[Any, ...]]) -> Dict[Tuple[int, str, str], Tuple[int, int]]:
    """Map question id -> (file_scope, seq) exactly as export_any_level.py lays it out.

    export_any_level.py groups by chapter then subtopic, orders chapters by
    (dot-count, chapter_key), orders subtopics by their code string, keeps
    question order from SQL, then splits each subtopic into parts of
    MAX_PER_FILE questions in order. `file_scope` is the 1-based part index and
    `seq` is the 1-based position inside that part, which is also the number
    the exporter prints as `"{new_number}. {question}"`.

    Returns {(question_id, chapter_key, code): (file_scope, seq)}. A question
    classified under two subtopics is exported in both, so its position has to
    be resolved per (chapter, subtopic) pair, not per question.
    """
    grouped: Dict[Tuple[str, str], List[Tuple[Any, ...]]] = {}
    for row in rows:
        grouped.setdefault((row[1], row[2]), []).append(row)

    chapters = sorted({ch for ch, _ in grouped},
                      key=lambda x: (str(x).count("."), str(x)))

    scope: Dict[Tuple[int, str, str], Tuple[int, int]] = {}
    for ch in chapters:
        codes = sorted({code for (ch2, code) in grouped if ch2 == ch})
        for code in codes:
            items = grouped[(ch, code)]
            for part_idx, start in enumerate(range(0, len(items), MAX_PER_FILE), start=1):
                chunk = items[start:start + MAX_PER_FILE]
                for pos, row in enumerate(chunk, start=1):
                    scope[(row[4], row[1], row[2])] = (part_idx, pos)
    return scope


def db_classified_questions_for_assignment(cur, level_key: str) -> List[Tuple[Any, ...]]:
    """Rows eligible for public qn id assignment, classified and deduplicated."""
    cur.execute("""
        SELECT
            l.level_key,
            c.chapter_key,
            s.code,
            s.title,
            q.id,
            q.question_number,
            q.question_text,
            q.options,
            q.correct_index,
            q.correct_text,
            q.source_question_key,
            q.identity_key,
            q.corrected_at,
            q.quality_score,
            q.stable_qn_id,
            qs.confidence
        FROM questions q
        JOIN question_subtopics qs ON qs.question_id = q.id
        JOIN subtopics s ON s.id = qs.subtopic_id
        JOIN chapters c ON c.id = s.chapter_id
        JOIN levels l   ON l.id = c.level_id
        WHERE c.level_id = (SELECT id FROM levels WHERE level_key = %s)
          AND q.canonical_id IS NULL
        ORDER BY c.chapter_key, s.code, q.question_number NULLS LAST, q.id
    """, (level_key,))
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Identity grouping.
# ---------------------------------------------------------------------------

_LOCAL_KEY_RE = re.compile(r"^n:\d+$")


def identity_group_key(row: Tuple[Any, ...]) -> str:
    """Stable identity key for grouping classified duplicates.

    A bare `n:<number>` source key is the question's number inside one source
    file, so the same value repeats across files and would merge unrelated
    questions. For those rows the content identity hash is the real identity.
    """
    src = row[10]
    ident = row[11]
    if src and not _LOCAL_KEY_RE.match(str(src)):
        return f"s:{src}"
    if ident:
        return f"i:{ident}"
    if src:
        return f"s:{src}"
    return ""


def best_keeper(rows: List[Tuple[Any, ...]]) -> Optional[Tuple[Any, ...]]:
    """Pick the best keeper among classified rows with the same stable identity."""
    if not rows:
        return None

    def sort_key(r: Tuple[Any, ...]) -> Tuple[Any, ...]:
        has_pub = bool(r[14])
        # corrected_at comes back as datetime for some columns and str for
        # others; normalise so mixed rows stay comparable.
        raw_corrected = r[12]
        if raw_corrected is None:
            corrected_at = ""
        elif hasattr(raw_corrected, "isoformat"):
            corrected_at = raw_corrected.isoformat()
        else:
            corrected_at = str(raw_corrected)
        quality = 0.0
        try:
            quality = float(r[13]) if r[13] is not None else 0.0
        except Exception:
            quality = 0.0
        conf = r[15]
        try:
            conf = float(conf) if conf is not None else 0.0
        except Exception:
            conf = 0.0
        return (
            has_pub,                          # already has public id -> prefer keeping it
            bool(corrected_at),               # corrected beats uncorrected
            corrected_at,                     # latest correction
            quality + conf,                   # best quality/confidence
            -int(r[4]),                       # lowest internal id wins (max of negatives)
        )

    return sorted(rows, key=sort_key)[-1]


def any_has_public_id(rows: List[Tuple[Any, ...]]) -> bool:
    return any(bool(r[14]) for r in rows)


# ---------------------------------------------------------------------------
# Resolution of file scope + sequence.
# ---------------------------------------------------------------------------

MAX_PER_FILE = 50


def resolve_export_scope(keeper: Tuple[Any, ...],
                         scope_map: Dict[Tuple[int, str, str], Tuple[int, int]]) -> Tuple[Optional[int], Optional[int]]:
    """Resolve (file_scope, seq) for the keeper from the export layout map.

    Returns (None, None) when the question is not in the exported layout.
    """
    entry = scope_map.get((keeper[4], keeper[1], keeper[2]))
    if not entry:
        return None, None
    return entry


# ---------------------------------------------------------------------------
# Public id ownership check.
# ---------------------------------------------------------------------------

def public_id_owner(cur, pub: str) -> Optional[int]:
    """Return the internal id that currently owns a public qn id, if any."""
    cur.execute("""
        SELECT id FROM questions
        WHERE stable_qn_id = %s
          AND canonical_id IS NULL
        LIMIT 1
    """, (pub,))
    row = cur.fetchone()
    return row[0] if row else None


def load_public_id_owners(cur) -> Dict[str, int]:
    """Return {public_qn_id: internal_id} for every id currently assigned."""
    cur.execute("""
        SELECT stable_qn_id, MIN(id)
        FROM questions
        WHERE stable_qn_id IS NOT NULL
          AND canonical_id IS NULL
        GROUP BY stable_qn_id
    """)
    return {pub: qid for pub, qid in cur.fetchall()}


def public_id_holders(cur, pub: str) -> List[int]:
    """Return all internal ids that currently claim a public qn id."""
    cur.execute("""
        SELECT id FROM questions
        WHERE stable_qn_id = %s
          AND canonical_id IS NULL
    """, (pub,))
    return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Main assignment for one level.
# ---------------------------------------------------------------------------

def assign_for_level(conn, level_key: str) -> Tuple[int, int, int, int]:
    """
    Assign/keep stable_qn_id for one level.
    Returns (assigned, reused, skipped, rejected).
    """
    cur = conn.cursor()
    rows = db_classified_questions_for_assignment(cur, level_key)
    print(f"  [{level_key}] {len(rows)} classified questions", flush=True)
    scope_map = build_export_scope_map(rows)
    print(f"  [{level_key}] {len(scope_map)} questions in export layout", flush=True)
    owners = load_public_id_owners(cur)
    print(f"  [{level_key}] {len(owners)} public ids already assigned", flush=True)

    groups: Dict[str, List[Tuple[Any, ...]]] = {}
    for row in rows:
        gk = identity_group_key(row)
        if not gk:
            continue
        groups.setdefault(gk, []).append(row)

    assigned = reused = skipped = rejected = 0

    # Pass 1: decide every public id in memory so collisions abort before any
    # write, then pass 2 writes them as batched UPDATE statements (one round
    # trip per batch instead of one per question).
    pending: List[Tuple[int, str]] = []

    for gk, group in groups.items():
        # Prefer a keeper row that is actually laid out in the export, so the
        # public id's chapter/subtopic always matches the position it encodes.
        exportable = [r for r in group if (r[4], r[1], r[2]) in scope_map]
        keeper = best_keeper(exportable or group)
        if keeper is None:
            skipped += 1
            continue

        keeper_id = keeper[4]

        # A corrected-option revision of the same question keeps the identity's
        # existing public id, even when the keeper itself has none yet.
        existing_pub = keeper[14] or next((r[14] for r in group if r[14]), "")

        # Resolve where it lives in the export layout.
        file_scope, seq = resolve_export_scope(keeper, scope_map)

        if file_scope is None or seq is None:
            skipped += 1
            continue

        if existing_pub:
            candidate = existing_pub
            reused += 1
        else:
            candidate = stable_qn_id_from_parts(
                level_key, keeper[1], keeper[2], file_scope, seq
            )
            assigned += 1

        # Collision guard. Same question identity may share a public id; a
        # different identity may not, so stop instead of overwriting.
        owner_id = owners.get(candidate)
        if owner_id is not None and owner_id != keeper_id:
            if not any(r[4] == owner_id for r in group):
                rejected += 1
                raise RuntimeError(
                    f"Assignment collision for level {level_key}: public id "
                    f"{candidate} is owned by question id {owner_id}, but the "
                    f"current keeper is question id {keeper_id} with group key "
                    f"{gk}. Fix the classified set or existing public ids before re-running."
                )

        owners[candidate] = keeper_id
        pending.append((keeper_id, candidate))

    # Only the keeper (the row that is actually exported) carries the public id.
    # `stable_qn_id` is UNIQUE, so the other copies of the same identity stay
    # blank here; the dedup machinery owns merging or tombstoning them, and the
    # reuse path above makes a later keeper inherit the same id.
    print(f"  [{level_key}] writing {len(pending)} public ids", flush=True)
    BATCH = 500
    for i in range(0, len(pending), BATCH):
        batch = pending[i:i + BATCH]
        values_sql = ",".join(
            cur.mogrify("(%s::int,%s::text)", (qid, pub)).decode() for qid, pub in batch
        )
        cur.execute(
            "UPDATE questions q SET stable_qn_id = v.pub "
            "FROM (VALUES " + values_sql + ") AS v(id, pub) "
            "WHERE q.id = v.id AND q.canonical_id IS NULL"
        )

    cur.close()
    return assigned, reused, skipped, rejected


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main() -> None:
    levels = [a for a in sys.argv[1:] if a] or ["level5", "level7", "gk"]
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)

    conn = db_connect(dsn)
    cur = conn.cursor()
    cur.execute("SELECT level_key FROM levels WHERE level_key = ANY(%s)", (levels,))
    known = {r[0] for r in cur.fetchall()}
    unknown = [l for l in levels if l not in known]
    if unknown:
        print("Unknown levels (skipping):", ", ".join(unknown))
        levels = [l for l in levels if l in known]

    total_assigned = total_reused = total_skipped = total_rejected = 0
    for lv in levels:
        a, r, s, x = assign_for_level(conn, lv)
        total_assigned += a
        total_reused += r
        total_skipped += s
        total_rejected += x
        print(f"{lv}: assigned={a} reused={r} skipped={s} rejected={x}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"TOTAL: assigned={total_assigned} reused={total_reused} skipped={total_skipped} rejected={total_rejected}")


if __name__ == "__main__":
    main()
