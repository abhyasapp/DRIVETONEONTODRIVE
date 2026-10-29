"""High-recall fuzzy duplicate candidate discovery; never auto-merges."""

import re
from collections import Counter, defaultdict

from psycopg2.extras import Json, execute_values
from rapidfuzz import fuzz

from dedupe.content_identity import STOPWORDS

MIN_STEM_SCORE = 80
MIN_OPTIONS_SCORE = 65
MAX_TOKEN_POSTING = 1000
MAX_CANDIDATES_PER_QUESTION = 50


def _norm(value):
    return " ".join(re.findall(r"[^\W_]+", str(value or "").casefold(), re.UNICODE))


def score_candidate(left, right):
    """Return stem score and option-set score (order independent)."""
    left_text, right_text = _norm(left.get("text")), _norm(right.get("text"))
    stem = max(fuzz.ratio(left_text, right_text),
               fuzz.token_sort_ratio(left_text, right_text))
    left_options = sorted(_norm(x) for x in (left.get("options") or []))
    right_options = sorted(_norm(x) for x in (right.get("options") or []))

    if not left_options or len(left_options) != len(right_options):
        return float(stem), 0.0
    option_scores = [
        max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))
        for a, b in zip(left_options, right_options)
    ]
    return float(stem), sum(option_scores) / len(option_scores)


def find_candidate_pairs(rows, *, min_stem=MIN_STEM_SCORE,
                         min_options=MIN_OPTIONS_SCORE):
    """Block by any rare content token; compare only within a chapter.

    Pairs with empty option sets are excluded even when a caller lowers the
    options threshold; there is no evidence in options to validate the match.
    Pairs of empty/very short stems are excluded because tokenless items would
    produce an unbounded all-to-all group. Candidate limits cap pathological
    broad questions while preserving the strongest candidates per item.
    """
    by_chapter = defaultdict(list)
    for row in rows:
        by_chapter[row["chapter_id"]].append(row)

    found = {}
    for chapter_id, items in by_chapter.items():
        document_frequency = Counter()
        tokens_by_id = {}
        for item in items:
            tokens = {t for t in re.findall(r"[^\W_]+", _norm(item["text"]))
                      if len(t) > 2 and t not in STOPWORDS}
            tokens_by_id[item["id"]] = tokens
            document_frequency.update(tokens)

        postings = defaultdict(list)
        for item in items:
            tokens = tokens_by_id[item["id"]]
            rare = sorted(tokens, key=lambda t: (document_frequency[t], t))[:8]
            for token in rare:
                if document_frequency[token] <= MAX_TOKEN_POSTING:
                    postings[token].append(item)

        candidate_pairs = set()
        for bucket in postings.values():
            for i, left in enumerate(bucket):
                for right in bucket[i + 1:]:
                    if left["identity_key"] == right["identity_key"]:
                        continue
                    a, b = sorted((left["id"], right["id"]))
                    candidate_pairs.add((a, b))

        by_id = {item["id"]: item for item in items}
        per_question = defaultdict(list)
        for left_id, right_id in candidate_pairs:
            left, right = by_id[left_id], by_id[right_id]
            stem_score, options_score = score_candidate(left, right)
            if (not left.get("options") or not right.get("options")
                    or stem_score < min_stem or options_score < min_options):
                continue
            candidate = (chapter_id, left_id, right_id, stem_score, options_score)
            per_question[left_id].append(candidate)
            per_question[right_id].append(candidate)

        kept = set()
        for pairs in per_question.values():
            pairs.sort(key=lambda p: (-(p[3] + p[4]) / 2, -p[3], p[1], p[2]))
            kept.update((p[1], p[2]) for p in pairs[:MAX_CANDIDATES_PER_QUESTION])
        for left_id, right_id in kept:
            left, right = by_id[left_id], by_id[right_id]
            stem_score, options_score = score_candidate(left, right)
            found[(left_id, right_id)] = (
                chapter_id, left_id, right_id, round(stem_score, 2),
                round(options_score, 2),
            )
    return sorted(found.values(), key=lambda r: (r[0], r[1], r[2]))


def refresh_review_candidates(cur, conn):
    """Upsert fuzzy candidates without deleting pending or resolved reviews."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dedupe_review_candidates (
            id BIGSERIAL PRIMARY KEY,
            chapter_id INTEGER NOT NULL,
            q1_id BIGINT NOT NULL,
            q2_id BIGINT NOT NULL,
            stem_score REAL NOT NULL,
            options_score REAL NOT NULL,
            q1_identity_key TEXT,
            q2_identity_key TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            review_note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reviewed_at TIMESTAMPTZ,
            UNIQUE (q1_id, q2_id),
            CHECK (q1_id < q2_id),
            CHECK (status IN ('pending', 'merged', 'rejected'))
        )
    """)
    cur.execute("ALTER TABLE dedupe_review_candidates ADD COLUMN IF NOT EXISTS q1_identity_key TEXT")
    cur.execute("ALTER TABLE dedupe_review_candidates ADD COLUMN IF NOT EXISTS q2_identity_key TEXT")
    cur.execute("""
        SELECT c.id, q.id, q.identity_key, q.question_text, q.options
        FROM questions q
        JOIN files f ON f.id = q.file_ref_id
        JOIN books b ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id
        WHERE q.canonical_id IS NULL
        ORDER BY c.id, q.id
    """)
    rows = [dict(chapter_id=r[0], id=r[1], identity_key=r[2],
                 text=r[3] or "", options=r[4] or []) for r in cur.fetchall()]
    candidates = find_candidate_pairs(rows)
    if candidates:
        candidate_rows = []
        for chapter_id, q1_id, q2_id, stem_score, options_score in candidates:
            by_id = {row["id"]: row for row in rows}
            candidate_rows.append((chapter_id, q1_id, q2_id, stem_score, options_score,
                                   by_id[q1_id]["identity_key"],by_id[q2_id]["identity_key"]))
        execute_values(cur, """
            INSERT INTO dedupe_review_candidates
                (chapter_id, q1_id, q2_id, stem_score, options_score,
                 q1_identity_key,q2_identity_key)
            VALUES %s
            ON CONFLICT (q1_id, q2_id) DO UPDATE SET
                chapter_id = EXCLUDED.chapter_id,
                stem_score = EXCLUDED.stem_score,
                options_score = EXCLUDED.options_score,
                q1_identity_key=EXCLUDED.q1_identity_key,
                q2_identity_key=EXCLUDED.q2_identity_key
            WHERE dedupe_review_candidates.status = 'pending'
        """, candidate_rows, page_size=1000)
    cur.execute("""
        DELETE FROM dedupe_review_candidates d
        WHERE status = 'pending' AND (
            NOT EXISTS (SELECT 1 FROM questions q WHERE q.id = d.q1_id)
            OR NOT EXISTS (SELECT 1 FROM questions q WHERE q.id = d.q2_id)
        )
    """)
    conn.commit()
    return len(candidates)
