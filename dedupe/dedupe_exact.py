#!/usr/bin/env python3
"""dedupe_exact.py (v9 — content identity, chapter scope).

Merges duplicates. Inside a group whose copies disagree on the answer,
the copies that AGREE with each other are still merged (keeper picked per
agreeing answer); only genuinely disagreeing single copies are left alone.

SCOPE: a duplicate group is one identity WITHIN ONE CHAPTER.  Questions are
only compared against copies that live in the same chapter (every book /
file / subtopic of that chapter counts) — never across chapters or levels.
"""

import argparse
import json
import os
import sys
import time

import psycopg2
from psycopg2.extras import Json, execute_values
from dotenv import load_dotenv

from utils.db import connect as db_connect
from dedupe.content_identity import (
    identity, canonical_answer, compute_quality_score,
)

load_dotenv()

ADVISORY_LOCK     = 0x64656475706571
STATEMENT_TIMEOUT = "30min"
CHUNK_SIZE        = 5000
DELETE_CHUNK      = 500

# ---------------------------------------------------------------------------
# Chapter scope
# ---------------------------------------------------------------------------
# A duplicate group is one identity_key INSIDE ONE CHAPTER.  A question is
# only compared against copies that live in the same chapter -- every book,
# file and subtopic of that chapter counts, but never another chapter and
# never another level.  The join is INNER: a question whose file -> book ->
# chapter chain is broken is left alone rather than merged with strangers.
CHAPTER_JOIN = """FROM questions q
        JOIN files f    ON f.id = q.file_ref_id
        JOIN books b    ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id"""

GROUP_COLS = "c.id, q.identity_key"      # what defines "the same question"


def coerce_options(opts):
    if isinstance(opts, str):
        try:
            opts = json.loads(opts)
        except ValueError:
            opts = []
    if not isinstance(opts, list):
        opts = []
    return opts


def chunked(iterable, n):
    buf = []
    for x in iterable:
        buf.append(x)
        if len(buf) == n:
            yield buf
            buf = []
    if buf:
        yield buf


def ensure_schema(cur, conn):
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS identity_key TEXT")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS quality_score REAL DEFAULT 0")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_q_identity "
        "ON questions(identity_key) WHERE canonical_id IS NULL"
    )
    conn.commit()


def acquire_lock(cur):
    cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK,))
    return cur.fetchone()[0]


def release_lock(cur):
    try:
        cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
    except psycopg2.Error:
        try:
            cur.connection.rollback()
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
        except psycopg2.Error:
            pass


def discover_child_fks(cur):
    cur.execute("""
        SELECT con.conrelid::regclass::text, quote_ident(att.attname), con.confdeltype
        FROM pg_constraint con
        JOIN pg_attribute att
          ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
        WHERE con.contype='f' AND con.confrelid='questions'::regclass
          AND array_length(con.conkey,1)=1
        ORDER BY 1,2
    """)
    out = []
    for table, col, action in cur.fetchall():
        if table.endswith("questions") and col == '"canonical_id"':
            continue
        if action in ("c", "n", "d"):
            continue
        out.append((table, col))
    return out


def populate_identities(cur, conn):
    cur.execute(
        "SELECT MIN(id), MAX(id), COUNT(*) FROM questions "
        "WHERE identity_key IS NULL OR quality_score IS NULL"
    )
    min_id, max_id, total = cur.fetchone()
    if not total:
        return 0

    print(f"      computing identity for {total} rows ...")
    processed = 0
    batch_start = min_id
    while batch_start <= max_id:
        batch_end = batch_start + CHUNK_SIZE
        cur.execute(
            """SELECT id, question_text, options, explanation, LENGTH(question_text)
               FROM questions
               WHERE (identity_key IS NULL OR quality_score IS NULL)
                 AND id >= %s AND id < %s""",
            (batch_start, batch_end),
        )
        rows = cur.fetchall()
        if rows:
            data = []
            for (qid, stem, opts, expl, tlen) in rows:
                o = coerce_options(opts)
                data.append((
                    identity(stem, o),
                    compute_quality_score(expl, o, tlen or 0),
                    qid,
                ))
            execute_values(
                cur,
                """UPDATE questions q SET
                     identity_key = v.ident, quality_score = v.qs
                   FROM (VALUES %s) AS v(ident, qs, id)
                   WHERE q.id = v.id::bigint""",
                data, template="(%s, %s, %s)", page_size=1000,
            )
            conn.commit()
            processed += len(rows)
        batch_start = batch_end

    # "uniq" counts groups per CHAPTER, so the same question sitting in two
    # different chapters is not counted as reducible.
    cur.execute(
        f"""SELECT COALESCE(SUM(n), 0), COUNT(*)
           FROM (SELECT COUNT(*) AS n
                 {CHAPTER_JOIN}
                 WHERE q.canonical_id IS NULL
                   AND q.identity_key IS NOT NULL
                 GROUP BY {GROUP_COLS}) g"""
    )
    total_rows, uniq = cur.fetchone()
    reducible = (total_rows or 0) - (uniq or 0)
    pct = (reducible / total_rows * 100) if total_rows else 0.0
    print(f"      reducible: {reducible} / {total_rows} rows ({pct:.1f}%)")
    return processed


def _keeper_key(x):
    return (-x["corrected_at"], -x["quality_score"], x["id"])


def pick_keeper(items):
    return min(items, key=_keeper_key)


def select_group_keys(cur, after=None, limit=5000):
    """(chapter_id, identity_key) pairs holding more than one live copy.

    Pairs are ordered by (chapter, identity), so `after` -- the last pair of
    -- the previous page -- is a cursor that never skips or repeats a group.
    """
    where = "q.identity_key IS NOT NULL AND q.canonical_id IS NULL"
    params = []
    if after is not None:
        where += " AND (c.id, q.identity_key) > (%s, %s)"
        params = [after[0], after[1]]
    params.append(limit)
    cur.execute(
        f"""SELECT c.id, q.identity_key
            {CHAPTER_JOIN}
            WHERE {where}
            GROUP BY {GROUP_COLS}
            HAVING COUNT(*) > 1
            ORDER BY c.id, q.identity_key
            LIMIT %s""",
        tuple(params),
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def load_groups(cur, keys):
    """keys: [(chapter_id, identity_key), ...] -> groups keyed the same way.

    The fetch is driven by identity_key alone (index-friendly); rows belonging
    to the same identity in ANOTHER chapter are dropped here, which is what
    keeps the merge inside a single chapter.
    """
    if not keys:
        return {}
    wanted = set(keys)
    idents = sorted({k[1] for k in keys})
    cur.execute(
        f"""SELECT q.id, c.id, q.identity_key, q.question_text, q.options,
                  q.correct_index, q.correct_text, q.quality_score,
                  EXTRACT(EPOCH FROM q.corrected_at)
            {CHAPTER_JOIN}
            WHERE q.identity_key = ANY(%s) AND q.canonical_id IS NULL
            ORDER BY q.identity_key, q.id""",
        (idents,),
    )
    groups = {}
    for (qid, chapter, ident, txt, opts, ci, ct, qs, corrected) in cur.fetchall():
        if (chapter, ident) not in wanted:
            continue
        groups.setdefault((chapter, ident), []).append({
            "id": qid, "text": txt or "",
            "options": coerce_options(opts),
            "ci": ci, "ct": ct,
            "quality_score": float(qs or 0),
            "corrected_at": float(corrected) if corrected is not None else 0.0,
        })
    return groups


def plan_deletions(groups):
    """
    Per group: partition by normalized answer, merge within each partition.
    A group where answers disagree is only PARTIALLY merged: every
    partition that internally agrees (2+ copies, same answer) is merged;
    singleton copies are left alone. `conflicts` counts groups that had
    more than one answer partition (i.e. copies that were kept).
    """
    plans, conflicts = [], 0
    for (_chapter, ident), items in groups.items():
        if len(items) < 2:
            continue
        buckets = {}
        for it in items:
            key = canonical_answer(it["ct"]) if it["ct"] is not None else "__unanswered__"
            buckets.setdefault(key, []).append(it)

        if len(buckets) > 1:
            conflicts += 1

        for bucket in buckets.values():
            keeper = pick_keeper(bucket)
            for it in bucket:
                if it["id"] == keeper["id"]:
                    continue
                plans.append((ident, keeper["id"], it["id"], it["text"],
                              Json(it["options"]), it["ci"], it["ct"], "exact"))
    return plans, conflicts


def apply_deletions(cur, conn, plans, child_fks):
    if not plans:
        return 0
    deleted_ids = [p[2] for p in plans]
    total = 0
    for chunk in chunked(deleted_ids, DELETE_CHUNK):
        cur.execute("UPDATE questions SET canonical_id = NULL WHERE canonical_id = ANY(%s)", (chunk,))
        for table, col in child_fks:
            cur.execute(f"DELETE FROM {table} WHERE {col} = ANY(%s)", (chunk,))
        cur.execute("DELETE FROM questions WHERE id = ANY(%s)", (chunk,))
        total += len(chunk)
    execute_values(
        cur,
        """INSERT INTO dedup_deletions
             (signature, canonical_id, deleted_id, question_text, options,
              correct_index, correct_text, reason)
           VALUES %s""",
        plans, template="(%s, %s, %s, %s, %s, %s, %s, %s)", page_size=500,
    )
    conn.commit()
    return total


def show_samples(cur, n=5):
    cur.execute(
        f"""SELECT c.id, q.identity_key
           {CHAPTER_JOIN}
           WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
           GROUP BY {GROUP_COLS} HAVING COUNT(*) > 1
           ORDER BY COUNT(*) DESC LIMIT %s""",
        (n,),
    )
    keys = [(r[0], r[1]) for r in cur.fetchall()]
    if not keys:
        print("      (no duplicate groups)")
        return
    groups = load_groups(cur, keys)
    for i, (chapter, ident) in enumerate(keys, 1):
        items = groups.get((chapter, ident), [])
        buckets = {}
        for it in items:
            k = canonical_answer(it["ct"]) if it["ct"] is not None else "__unanswered__"
            buckets.setdefault(k, []).append(it)
        flag = "  [CONFLICT — agreeing copies still merged]" if len(buckets) > 1 else ""
        print(f"\n      -- group {i}/{len(keys)} — {len(items)} copies — "
              f"chapter {chapter}  ident {ident[:12]}...{flag}")
        for bucket in buckets.values():
            keeper = pick_keeper(bucket)
            for it in sorted(bucket, key=_keeper_key):
                marker = "KEEP" if it["id"] == keeper["id"] else "DEL "
                ans = str(it["ct"])[:30] if it["ct"] is not None else "-"
                print(f"        {marker} id={it['id']:>8} ans={ans!r}  {it['text'][:60]!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--samples", type=int, default=10)
    ap.add_argument("--quiet", "-q", action="store_true")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print("=" * 66)
    print(f"CONTENT-IDENTITY DEDUPE  v9 (chapter scope)  [{mode}]")
    print("=" * 66)

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    conn = db_connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        if not acquire_lock(cur):
            print("ERROR: another dedupe is running — failing loudly so the "
                  "pipeline treats this run as skipped, not successful.")
            release_lock(cur)
            conn.close()
            sys.exit(1)

        try:
            print("\n[1/3] Schema")
            ensure_schema(cur, conn)

            print("[2/3] Identities")
            populate_identities(cur, conn)

            print("[3/3] Groups (chapter scope)")
            cur.execute(
                f"""SELECT COUNT(*) FROM (
                     SELECT 1 {CHAPTER_JOIN}
                     WHERE q.identity_key IS NOT NULL
                       AND q.canonical_id IS NULL
                     GROUP BY {GROUP_COLS} HAVING COUNT(*) > 1
                   ) x"""
            )
            n_groups = cur.fetchone()[0]
            print(f"      Duplicate groups: {n_groups}")

            if n_groups == 0:
                print("\nNothing to merge.")
                return

            if not args.apply:
                print("\nSample groups:")
                show_samples(cur, n=args.samples)
                print("\n" + "-" * 66)
                print("DRY-RUN. Run with --apply to merge.")
                print("-" * 66)
                return

            print("\nApplying ...")
            children = discover_child_fks(cur)

            # Page through (chapter, identity) groups
            last = None
            total_merged = 0
            total_conflicts = 0
            while True:
                page = select_group_keys(cur, after=last, limit=5000)
                if not page:
                    break
                last = page[-1]
                groups = load_groups(cur, page)
                plans, conflicts = plan_deletions(groups)
                total_conflicts += conflicts
                if plans:
                    total_merged += apply_deletions(cur, conn, plans, children)

            conn.commit()
            cur.execute("SELECT COUNT(*) FROM questions WHERE canonical_id IS NULL")
            remaining = cur.fetchone()[0]
            print("=" * 66)
            print(f"Rows merged:       {total_merged}")
            print(f"Conflicting groups (partially merged): {total_conflicts}")
            print(f"Questions live:    {remaining}")
            print("=" * 66)

        finally:
            release_lock(cur)

    except psycopg2.Error as e:
        print(f"DATABASE ERROR: {e}")
        try: conn.rollback()
        except Exception: pass
        sys.exit(2)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()