"""
classify_any_level.py  (v2 — fast, bulk operations)
---------------------------------------------------
Classifies questions for a given level using keyword matching.

Strategy:
  1. Load ALL questions in one query
  2. Load ALL subtopics + keyword dict in memory
  3. Classify in-process (threaded)
  4. Bulk DELETE old links (one query)
  5. Bulk INSERT new links (one query)
  6. Commit once

For 7,000 questions: ~5 DB round-trips instead of ~14,000.

Usage:
  python classify_any_level.py level5
  python classify_any_level.py gk
  python classify_any_level.py level7
"""

import os, re, sys, json, time
from concurrent.futures import ThreadPoolExecutor
import psycopg2
from psycopg2.extras import Json, execute_values
from dotenv import load_dotenv

load_dotenv()

KEYWORD_FILE_FOR = {
    "level5": "l5_keywords.json",
    "gk":     "gk_keywords.json",
    "level7": "l7_keywords.json",
}

# ---------- text helpers ----------
_NORM_KEEP = re.compile(r"[^a-z0-9\s\-/]")
_WS        = re.compile(r"\s+")


def normalize(text):
    text = (text or "").lower()
    text = _NORM_KEEP.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return " " + text + " "


def find_matches(q_norm, keywords):
    hits = []
    for kw in keywords:
        if not kw:
            continue
        if (" " + kw + " ") in q_norm:
            hits.append(kw)
        elif q_norm.startswith(kw + " ") or q_norm.endswith(" " + kw):
            hits.append(kw)
    return hits


# ---------- core classify-one (runs in worker threads) ----------
def classify_one(qid, ch_key, qtext, subs_by_chapter, kw_by_chapter,
                 unclassified_id):
    subs = subs_by_chapter.get(ch_key, [])
    kw_map = kw_by_chapter.get(ch_key, {})
    q_norm = normalize(qtext)

    best_score = 0
    best_id = None
    best_hits = []
    second_score = 0

    for sub_id, code in subs:
        hits = find_matches(q_norm, kw_map.get(code, ()))
        score = len(hits)
        if score > best_score:
            second_score = best_score
            best_score = score
            best_id = sub_id
            best_hits = hits
        elif score > second_score:
            second_score = score

    if best_id is None or best_score == 0 or best_score == second_score:
        return (qid, unclassified_id.get(ch_key), best_score, best_hits, False)
    return (qid, best_id, best_score, best_hits, True)


# ---------- main ----------
def main():
    if len(sys.argv) < 2:
        print("Usage: python classify_any_level.py <level_key> [workers]")
        sys.exit(1)

    level_key = sys.argv[1]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    kw_file = KEYWORD_FILE_FOR.get(level_key)
    if not kw_file:
        print(f"No keyword file mapped for '{level_key}'.")
        sys.exit(1)

    # ---- load keyword dict ----
    with open(kw_file, "r", encoding="utf-8") as f:
        RAW = json.load(f)

    KEYWORDS = {}
    for ch_key, subs in RAW.items():
        KEYWORDS[ch_key] = {}
        for full_key, words in subs.items():
            code = full_key.split()[0]
            KEYWORDS[ch_key][code] = tuple(
                w.lower().strip() for w in words if w.strip()
            )

    start = time.time()
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    cur.execute("SELECT id FROM levels WHERE level_key = %s", (level_key,))
    row = cur.fetchone()
    if not row:
        print(f"ERROR: level '{level_key}' not found.")
        sys.exit(1)
    level_id = row[0]

    # ---- ensure mixedqn per chapter (batch) ----
    cur.execute("SELECT id, chapter_key FROM chapters WHERE level_id = %s",
                (level_id,))
    chapters = cur.fetchall()

    unclassified_id = {}
    for ch_id, ch_key in chapters:
        cur.execute("""
            INSERT INTO subtopics (chapter_id, code, title)
            VALUES (%s, 'mixedqn', 'Unclassified questions')
            ON CONFLICT (chapter_id, code)
            DO UPDATE SET title = EXCLUDED.title
            RETURNING id
        """, (ch_id,))
        unclassified_id[ch_key] = cur.fetchone()[0]
    conn.commit()

    # ---- load all real subtopics ----
    cur.execute("""
        SELECT c.chapter_key, s.id, s.code
        FROM subtopics s
        JOIN chapters c ON c.id = s.chapter_id
        WHERE c.level_id = %s AND s.code <> 'mixedqn'
    """, (level_id,))
    subs_by_chapter = {}
    for ch_key, sub_id, code in cur.fetchall():
        subs_by_chapter.setdefault(ch_key, []).append((sub_id, code))

    # ---- load all questions to process (one query) ----
    cur.execute("""
        SELECT q.id, c.chapter_key, q.question_text
        FROM questions q
        JOIN files f    ON f.id = q.file_ref_id
        JOIN books b    ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id
        WHERE c.level_id = %s
          AND q.canonical_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM question_subtopics qs
              JOIN subtopics s ON s.id = qs.subtopic_id
              WHERE qs.question_id = q.id
                AND (s.code <> 'mixedqn' OR qs.classified_by = 'manual')
          )
        ORDER BY q.id
    """, (level_id,))
    questions = cur.fetchall()

    print(f"Level '{level_key}': {len(questions)} questions to classify "
          f"({workers} workers).\n")

    if not questions:
        print("Nothing to do.")
        cur.close(); conn.close()
        return

    # ---- classify in parallel ----
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(classify_one, qid, ch_key, qtext,
                        subs_by_chapter, KEYWORDS, unclassified_id)
            for qid, ch_key, qtext in questions
        ]
        for i, fut in enumerate(futures, 1):
            results.append(fut.result())
            if i % 1000 == 0:
                print(f"  classified {i}/{len(questions)}")
    print(f"Parallel classify: {time.time()-t0:.1f}s")

    # ---- bulk delete old links (one query) ----
    qids = [r[0] for r in results]
    t0 = time.time()
    cur.execute(
        "DELETE FROM question_subtopics WHERE question_id = ANY(%s)",
        (qids,)
    )
    print(f"Bulk delete: {time.time()-t0:.1f}s")

    # ---- bulk insert new links (one query, chunked) ----
    rows = []
    classified = unclassified = ambiguous = 0
    per_chapter = {}

    for (qid, sub_id, score, hits, was_classified) in results:
        if not sub_id:
            continue
        rows.append((qid, sub_id, score, Json(hits), "auto"))
        ch_key = next((q[1] for q in questions if q[0] == qid), None)
        per_chapter.setdefault(ch_key, {"class": 0, "unclass": 0})
        if was_classified:
            classified += 1
            per_chapter[ch_key]["class"] += 1
        else:
            unclassified += 1
            per_chapter[ch_key]["unclass"] += 1
            if score > 0:
                ambiguous += 1

    t0 = time.time()
    execute_values(
        cur,
        """
        INSERT INTO question_subtopics
            (question_id, subtopic_id, confidence, matched_keywords, classified_by)
        VALUES %s
        """,
        rows,
        template="(%s, %s, %s, %s, %s)",
        page_size=1000,
    )
    print(f"Bulk insert: {time.time()-t0:.1f}s")

    conn.commit()
    print(f"Total: {time.time()-start:.1f}s\n")

    print(f"Classified:    {classified}")
    print(f"Unclassified:  {unclassified}  (ties: {ambiguous})")
    print()
    print(f"  {'Ch':<4}{'Classified':>12}{'Unclass.':>12}")
    for ch_key in sorted(per_chapter, key=lambda x: (len(x) if x else 0, x or "")):
        print(f"  {ch_key:<4}{per_chapter[ch_key]['class']:>12}"
              f"{per_chapter[ch_key]['unclass']:>12}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
    