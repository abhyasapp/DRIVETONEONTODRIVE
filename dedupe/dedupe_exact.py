#!/usr/bin/env python3
"""Exact duplicate detection scoped to one chapter.

Only exact question+option identities merge automatically. A merge is allowed
only when both copies carry the same valid, verified answer. Fuzzy candidates
are generated separately for human review.
"""

import argparse
import json
import os
import sys

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json, execute_values

from utils.db import connect as db_connect
from dedupe.content_identity import (
    IDENTITY_VERSION, answer_identity, compute_quality_score, identity,
    source_question_key,
)
from dedupe.merge_questions import (
    discover_question_child_fks, ensure_merge_schema, merge_question_rows,
)

load_dotenv()

ADVISORY_LOCK = 0x64656475706571
STATEMENT_TIMEOUT = "30min"
CHUNK_SIZE = 5000
DELETE_CHUNK = 500

CHAPTER_JOIN = """FROM questions q
        JOIN files f ON f.id = q.file_ref_id
        JOIN books b ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id"""
GROUP_COLS = "c.id, q.identity_key"


def coerce_options(opts):
    if isinstance(opts, str):
        try:
            opts = json.loads(opts)
        except ValueError:
            return []
    return opts if isinstance(opts, list) else []


def ensure_schema(cur, conn):
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS identity_key TEXT")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS identity_version SMALLINT")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS quality_score REAL DEFAULT 0")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_q_identity_chapter "
                "ON questions(identity_key) WHERE canonical_id IS NULL")
    ensure_merge_schema(cur)
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


def populate_identities(cur, conn):
    cur.execute("""SELECT MIN(id),MAX(id),COUNT(*) FROM questions
        WHERE identity_key IS NULL OR quality_score IS NULL
           OR identity_version IS DISTINCT FROM %s""", (IDENTITY_VERSION,))
    min_id, max_id, total = cur.fetchone()
    if not total:
        return 0
    print(f"      computing identity for {total} rows ...")
    processed = 0
    batch_start = min_id
    while batch_start <= max_id:
        batch_end = batch_start + CHUNK_SIZE
        cur.execute("""SELECT id,question_text,options,explanation,LENGTH(question_text)
            FROM questions
            WHERE (identity_key IS NULL OR quality_score IS NULL
                   OR identity_version IS DISTINCT FROM %s)
              AND id >= %s AND id < %s""", (IDENTITY_VERSION,batch_start,batch_end))
        rows = cur.fetchall()
        if rows:
            data = [(identity(stem, coerce_options(opts)),
                     compute_quality_score(expl, coerce_options(opts), text_len or 0),
                     IDENTITY_VERSION, qid)
                    for qid,stem,opts,expl,text_len in rows]
            execute_values(cur,"""UPDATE questions q SET identity_key=v.ident,
                quality_score=v.qs,identity_version=v.version
                FROM (VALUES %s) AS v(ident,qs,version,id)
                WHERE q.id=v.id::bigint""", data,
                template="(%s,%s,%s,%s)", page_size=1000)
            conn.commit()
            processed += len(rows)
        batch_start = batch_end
    cur.execute(f"""SELECT COALESCE(SUM(n),0),COUNT(*) FROM (
        SELECT COUNT(*) n {CHAPTER_JOIN}
        WHERE q.canonical_id IS NULL AND q.identity_key IS NOT NULL
        GROUP BY {GROUP_COLS}) g""")
    total_rows, unique_groups = cur.fetchone()
    reducible = (total_rows or 0) - (unique_groups or 0)
    pct = reducible / total_rows * 100 if total_rows else 0
    print(f"      reducible: {reducible} / {total_rows} rows ({pct:.1f}%)")
    return processed


def _keeper_key(item):
    return (-item["corrected_at"], -item["quality_score"], item["id"])


def pick_keeper(items):
    return min(items, key=_keeper_key)


def select_group_keys(cur, after=None, limit=5000):
    where = "q.identity_key IS NOT NULL AND q.canonical_id IS NULL"
    params = []
    if after is not None:
        where += " AND (c.id,q.identity_key)>(%s,%s)"
        params.extend(after)
    params.append(limit)
    cur.execute(f"""SELECT c.id,q.identity_key {CHAPTER_JOIN}
        WHERE {where} GROUP BY {GROUP_COLS} HAVING COUNT(*)>1
        ORDER BY c.id,q.identity_key LIMIT %s""", tuple(params))
    return [(r[0],r[1]) for r in cur.fetchall()]


def load_groups(cur, keys):
    if not keys:
        return {}
    wanted = set(keys)
    identities = sorted({identity_key for _,identity_key in keys})
    cur.execute(f"""SELECT q.id,c.id,q.identity_key,q.question_text,q.options,
            q.correct_index,q.correct_text,q.quality_score,
            EXTRACT(EPOCH FROM q.corrected_at),q.question_number,q.raw,
            q.source_question_key,q.source_identity
        {CHAPTER_JOIN}
        WHERE q.identity_key=ANY(%s) AND q.canonical_id IS NULL
        ORDER BY q.identity_key,q.id""", (identities,))
    groups = {}
    for row in cur.fetchall():
        qid,chapter,identity_key,text,options,ci,ct,quality,corrected = row[:9]
        question_number,raw,source_key,source_identity = row[9:13]
        if (chapter,identity_key) not in wanted:
            continue
        raw = raw if isinstance(raw, dict) else {}
        opts = coerce_options(options)
        groups.setdefault((chapter,identity_key), []).append({
            "id":qid,"text":text or "","options":opts,"ci":ci,"ct":ct,
            "quality_score":float(quality or 0),
            "corrected_at":float(corrected) if corrected is not None else 0.0,
            "question_number":question_number,"raw":raw,
            "source_question_key":source_key,"source_identity":source_identity,
            "source_text":raw.get("q",text or ""),
            "source_options":raw.get("options",opts) or [],
        })
    return groups


def plan_deletions(groups):
    plans, conflicts = [], 0
    for (_chapter,identity_key), items in groups.items():
        if len(items)<2:
            continue
        buckets = {}
        for item in items:
            answer_key = answer_identity(item["ct"],item["ci"],item["options"])
            buckets.setdefault(answer_key,[]).append(item)
        if len(buckets)>1:
            conflicts += 1
        for answer_key,bucket in buckets.items():
            if answer_key.startswith("__") or len(bucket)<2:
                continue
            keeper = pick_keeper(bucket)
            for item in bucket:
                if item["id"]==keeper["id"]:
                    continue
                source_key = item.get("source_question_key") or source_question_key(
                    item.get("question_number"),item.get("source_text",item["text"]),
                    item.get("source_options",item["options"]))
                plans.append((identity_key,keeper["id"],item["id"],source_key))
    return plans,conflicts


def apply_deletions(cur,conn,plans,child_fks=None):
    if not plans:
        return 0
    children = child_fks if child_fks is not None else discover_question_child_fks(cur)
    merged = 0
    for identity_key,keeper_id,duplicate_id,source_key in plans:
        merge_question_rows(cur,keeper_id,duplicate_id,reason="exact",
            signature=identity_key,child_fks=children,
            source_question_key_override=source_key)
        merged += 1
        if merged % DELETE_CHUNK == 0:
            conn.commit()
    conn.commit()
    return merged


def show_samples(cur,n=5):
    cur.execute(f"""SELECT c.id,q.identity_key {CHAPTER_JOIN}
        WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
        GROUP BY {GROUP_COLS} HAVING COUNT(*)>1
        ORDER BY COUNT(*) DESC LIMIT %s""",(n,))
    keys=[(r[0],r[1]) for r in cur.fetchall()]
    if not keys:
        print("      (no duplicate groups)")
        return
    for idx,((chapter,identity_key),items) in enumerate(load_groups(cur,keys).items(),1):
        print(f"\n      -- group {idx}/{len(keys)} — {len(items)} copies — chapter {chapter} {identity_key[:12]}...")
        plans,_=plan_deletions({(chapter,identity_key):items})
        merge_ids={p[2] for p in plans}
        for item in sorted(items,key=_keeper_key):
            answer=answer_identity(item["ct"],item["ci"],item["options"])
            label="MERGE" if item["id"] in merge_ids else "KEEP"
            print(f"        {label} id={item['id']:>8} ans={answer[:30]!r} {item['text'][:60]!r}")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--samples",type=int,default=10)
    parser.add_argument("--quiet","-q",action="store_true")
    args=parser.parse_args()
    mode="APPLY" if args.apply else "DRY-RUN"
    print("="*66)
    print(f"CONTENT-IDENTITY DEDUPE v10 (chapter scope) [{mode}]")
    print("="*66)
    dsn=os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)
    conn=db_connect(dsn)
    conn.autocommit=False
    cur=conn.cursor()
    try:
        cur.execute(f"SET statement_timeout='{STATEMENT_TIMEOUT}'")
        if not acquire_lock(cur):
            print("ERROR: another dedupe is running — aborting.")
            release_lock(cur)
            sys.exit(1)
        try:
            print("\n[1/3] Schema")
            ensure_schema(cur,conn)
            print("[2/3] Identities")
            populate_identities(cur,conn)
            print("[3/3] Groups (chapter scope)")
            cur.execute(f"""SELECT COUNT(*) FROM (
                SELECT 1 {CHAPTER_JOIN} WHERE q.identity_key IS NOT NULL
                AND q.canonical_id IS NULL GROUP BY {GROUP_COLS} HAVING COUNT(*)>1) x""")
            group_count=cur.fetchone()[0]
            print(f"      Duplicate groups: {group_count}")
            if not args.apply:
                print("\nSample groups:")
                show_samples(cur,args.samples)
                print("\nDRY-RUN. No database writes performed.")
                return
            last=None
            total_merged=total_conflicts=0
            children=discover_question_child_fks(cur)
            while True:
                page=select_group_keys(cur,after=last,limit=5000)
                if not page:
                    break
                last=page[-1]
                groups=load_groups(cur,page)
                plans,conflicts=plan_deletions(groups)
                total_conflicts+=conflicts
                total_merged+=apply_deletions(cur,conn,plans,children)
            from dedupe.near_candidates import refresh_review_candidates
            candidates=refresh_review_candidates(cur,conn)
            conn.commit()
            cur.execute("SELECT COUNT(*) FROM questions WHERE canonical_id IS NULL")
            remaining=cur.fetchone()[0]
            print(f"Rows merged: {total_merged}")
            print(f"Conflicting/unverified groups: {total_conflicts}")
            print(f"Fuzzy candidates awaiting review: {candidates}")
            print(f"Questions live: {remaining}")
        finally:
            release_lock(cur)
    except psycopg2.Error as exc:
        print(f"DATABASE ERROR: {exc}")
        conn.rollback()
        sys.exit(2)
    finally:
        cur.close()
        conn.close()


if __name__=="__main__":
    main()
