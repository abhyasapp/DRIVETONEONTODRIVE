#!/usr/bin/env python3
"""
apply_corrections.py  (v2)
--------------------------
Pull corrections from the Abhyas Apps Script backend and apply them to
Neon BEFORE dedupe and export.

Run order: sync -> corrections -> dedupe -> classify -> export.

Usage:
    python apply_corrections.py                        # dry-run
    python apply_corrections.py --apply                # write
    python apply_corrections.py --apply --force        # ignore last_read
    python apply_corrections.py --apply --level level5 # one level only
"""
import argparse
import json
import os
import re
import sys

import requests
from psycopg2.extras import Json
from dotenv import load_dotenv

from utils.db import connect as db_connect
from dedupe.content_identity import (
    identity, legacy_identity, source_question_key, source_location_key,
    compute_quality_score,
)
from dedupe.merge_questions import ensure_merge_schema, merge_question_rows
from dedupe.source_aliases import ensure_correction_source_alias_schema

load_dotenv()

APPS_URL = os.getenv("ABHYAS_APPS_SCRIPT_URL")
SYNC_KEY = os.getenv("ABHYAS_SYNC_KEY")
FETCH_TIMEOUT = 90


class BackendError(RuntimeError):
    pass


def ensure_schema(cur, conn):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS applied_corrections (
            uid           TEXT        NOT NULL,
            corrected_at  TIMESTAMPTZ NOT NULL,
            applied_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            matched_by    TEXT,
            question_id   BIGINT,
            file_id       TEXT,
            before_identity TEXT,
            after_identity TEXT,
            notes         TEXT,
            PRIMARY KEY (uid, corrected_at)
        )
    """)
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS corrected_at TIMESTAMPTZ")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS source_question_key TEXT")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS identity_key TEXT")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS quality_score REAL DEFAULT 0")
    cur.execute("ALTER TABLE applied_corrections ADD COLUMN IF NOT EXISTS before_identity TEXT")
    cur.execute("ALTER TABLE applied_corrections ADD COLUMN IF NOT EXISTS after_identity TEXT")
    cur.execute("""CREATE TABLE IF NOT EXISTS correction_backlog (
        uid TEXT NOT NULL,
        corrected_at TIMESTAMPTZ NOT NULL,
        correction JSONB NOT NULL,
        reason TEXT NOT NULL,
        candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
        attempts INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (uid, corrected_at)
    )""")
    ensure_merge_schema(cur)
    ensure_correction_source_alias_schema(cur)
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS source_identity TEXT")
    conn.commit()


def get_last_read(cur):
    cur.execute("SELECT value FROM settings WHERE key='last_read_corrections_at'")
    row = cur.fetchone()
    return int(row[0]) if row and row[0] else 0


def set_last_read(cur, ts):
    cur.execute("""
        INSERT INTO settings (key, value)
        VALUES ('last_read_corrections_at', %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """, (str(ts),))


def fetch_corrections(since_ms, limit=500):
    if not APPS_URL:
        raise BackendError("ABHYAS_APPS_SCRIPT_URL not set in .env")
    if not SYNC_KEY:
        raise BackendError("ABHYAS_SYNC_KEY not set in .env")

    body = json.dumps({
        "action": "syncCorrectionsSince",
        "key": SYNC_KEY,
        "since": since_ms,
        "limit": limit,
    })

    try:
        r = requests.post(
            APPS_URL, data=body,
            headers={"Content-Type": "text/plain"},
            timeout=FETCH_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        raise BackendError(f"timeout after {FETCH_TIMEOUT}s reaching {APPS_URL}")
    except requests.exceptions.ConnectionError as e:
        raise BackendError(f"connection failed to {APPS_URL}: {type(e).__name__}")

    if r.status_code in (401, 403):
        raise BackendError(
            f"HTTP {r.status_code} — ABHYAS_SYNC_KEY does not match "
            f"SYNC_KEY in the Apps Script project."
        )
    if 400 <= r.status_code < 500:
        raise BackendError(f"HTTP {r.status_code} — {r.text[:200]!r}")
    if r.status_code >= 500:
        raise BackendError(f"HTTP {r.status_code} — {r.text[:200]!r}")

    try:
        payload = r.json()
    except ValueError:
        raise BackendError(
            f"non-JSON response ({r.headers.get('Content-Type','?')}): "
            f"{r.text[:200]!r}"
        )

    return payload


def normalize_question_text(text):
    return re.sub(r"^\s*\d+\s*[\.\)]\s*", "", str(text or "")).strip()


def source_location_key_for_file(cur, file_id):
    cur.execute("""SELECT b.book_name,f.subtopic FROM files f
        JOIN books b ON b.id=f.book_id WHERE f.file_id=%s ORDER BY f.id""", (file_id,))
    rows = cur.fetchall()
    keys = {source_location_key(book, subtopic) for book, subtopic in rows}
    return next(iter(keys)) if len(keys) == 1 else None


def find_match(cur, before_question, before_options, before_identity, file_id=None):
    base = """
        SELECT q.id, q.question_text, q.options, q.file_ref_id,
               q.source_question_key, q.corrected_at, f.file_id,
               c.id AS chapter_id
        FROM questions q
        LEFT JOIN files f ON f.id = q.file_ref_id
        LEFT JOIN books b ON b.id = f.book_id
        LEFT JOIN chapters c ON c.id = b.chapter_id
        WHERE q.canonical_id IS NULL
          AND q.identity_key = %s
    """

    if file_id:
        # Prefer the durable source question number within this file. Wording,
        # options and content hashes may all change after a correction.
        number_match = re.match(r"^\s*(\d+)\s*[.)]", str(before_question))
        source_number = str(number_match.group(1)) if number_match else ""
        source_key = source_question_key(source_number or None, before_question, before_options)
        cur.execute("""SELECT q.id,q.question_text,q.options,q.file_ref_id,
                q.source_question_key,q.corrected_at,f.file_id,c.id
            FROM questions q JOIN files f ON f.id=q.file_ref_id
            LEFT JOIN books b ON b.id=f.book_id
            LEFT JOIN chapters c ON c.id=b.chapter_id
            WHERE q.canonical_id IS NULL AND f.file_id=%s
              AND (q.source_question_key=%s OR
                   (q.source_question_key IS NULL AND q.question_number::text=%s))""",
            (file_id,source_key,source_number))
        numbered=cur.fetchall() if source_number else []
        if len(numbered)==1:
            return numbered[0],"source_number_file"
        if len(numbered)>1:
            return None,f"ambiguous_source_number_file({len(numbered)})"

        cur.execute(base + " AND f.file_id = %s", (before_identity,file_id))
        local=cur.fetchall()
        if len(local)==1:
            return local[0],"identity_file"
        if len(local)>1:
            corrected=[r for r in local if r[5] is not None]
            if len(corrected)==1:
                return corrected[0],"identity_file_corrected"
            return None,f"ambiguous_identity_in_source_file({len(local)})"

        norm=normalize_question_text(before_question)
        cur.execute("""SELECT q.id,q.question_text,q.options,q.file_ref_id,
                q.source_question_key,q.corrected_at,f.file_id,c.id
            FROM questions q JOIN files f ON f.id=q.file_ref_id
            LEFT JOIN books b ON b.id=f.book_id
            LEFT JOIN chapters c ON c.id=b.chapter_id
            WHERE q.canonical_id IS NULL AND f.file_id=%s
              AND regexp_replace(q.question_text, '^\s*\d+\s*[\.\)]\s*', '')=%s
              AND q.options=%s""", (file_id,norm,Json(before_options)))
        local=cur.fetchall()
        if len(local)==1:
            return local[0],"text_options_file"
        if len(local)>1:
            corrected=[r for r in local if r[5] is not None]
            if len(corrected)==1:
                return corrected[0],"text_options_file_corrected"
            return None,f"ambiguous_text_in_source_file({len(local)})"
        return None,"not_found_in_source_file"

    cur.execute(base,(before_identity,))
    rows=cur.fetchall()
    if len(rows)==1:
        return rows[0],"identity"
    if len(rows)>1:
        corrected=[r for r in rows if r[5] is not None]
        if len(corrected)==1:
            return corrected[0],"identity_corrected"
        return None,f"ambiguous_identity({len(rows)})"

    norm=normalize_question_text(before_question)
    cur.execute("""SELECT q.id,q.question_text,q.options,q.file_ref_id,
            q.source_question_key,q.corrected_at,f.file_id,c.id
        FROM questions q LEFT JOIN files f ON f.id=q.file_ref_id
        LEFT JOIN books b ON b.id=f.book_id LEFT JOIN chapters c ON c.id=b.chapter_id
        WHERE q.canonical_id IS NULL
          AND regexp_replace(q.question_text, '^\s*\d+\s*[\.\)]\s*', '')=%s
          AND q.options=%s""", (norm,Json(before_options)))
    rows=cur.fetchall()
    if len(rows)==1:
        return rows[0],"text_options"
    if len(rows)>1:
        return None,f"ambiguous_text({len(rows)})"
    return None,"not_found"


def apply_one(cur, corr):
    uid = corr["uid"]
    corrected_at = corr["correctedAt"]
    cur.execute("""SELECT 1 FROM applied_corrections
                   WHERE uid = %s AND corrected_at = %s""",
                (uid, corrected_at))
    if cur.fetchone():
        return None, "already_applied"

    if not uid or not corrected_at:
        return None, "missing_uid_or_corrected_at"

    before = corr.get("before") or {}
    after  = corr.get("after")  or {}
    bq = before.get("question") or ""
    bo = before.get("options")  or []
    aq = after.get("question")  or ""
    ao = after.get("options")   or []
    ac = after.get("correctIndex")
    ax = after.get("explanation") or ""

    if not bq or not aq:
        return None, "missing_before_after"
    if not isinstance(bo, list) or not isinstance(ao, list):
        return None, "invalid_before_or_after_options"

    source_identity = identity(bq, bo)
    before_id = source_identity
    row, why = find_match(cur, bq, bo, before_id, corr.get("fileId"))
    if row is None:
        row, why = find_match(cur, bq, bo, legacy_identity(bq, bo), corr.get("fileId"))
        if row is not None:
            why = "legacy_" + why
    if row is None:
        cur.execute("""
            INSERT INTO correction_backlog (uid, corrected_at, correction, reason)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (uid, corrected_at) DO UPDATE SET
                correction = EXCLUDED.correction,
                reason = EXCLUDED.reason,
                attempts = correction_backlog.attempts + 1,
                updated_at = now()
        """, (uid, corrected_at, Json(corr), why))
        return None, why

    qid, old_text, old_opts, file_ref_id, source_key, _old_corrected, _file_id, chapter_id = row
    new_text = normalize_question_text(aq)
    new_identity = identity(aq, ao)
    new_quality = compute_quality_score(ax, ao, len(new_text))
    correct_text = ao[ac] if isinstance(ac, int) and not isinstance(ac, bool) and 0 <= ac < len(ao) else None

    if not source_key:
        before_number = re.match(r"^\s*(\d+)\s*[\.\)]", str(bq))
        source_key = source_question_key(
            before_number.group(1) if before_number else None, bq, bo)

    valid_correct_index = (ac if isinstance(ac, int) and not isinstance(ac, bool)
                           and 0 <= ac < len(ao) else None)
    correct_text = ao[valid_correct_index] if valid_correct_index is not None else None

    cur.execute("""
        UPDATE questions q
        SET question_text = %s,
            options       = %s,
            correct_index = %s,
            correct_text  = %s,
            explanation   = COALESCE(NULLIF(%s, ''), q.explanation),
            identity_key  = %s,
            quality_score = %s,
            corrected_at  = %s,
            source_identity = COALESCE(q.source_identity, %s),
            source_question_key = COALESCE(q.source_question_key, %s)
        FROM files f JOIN books b ON b.id = f.book_id
                     JOIN chapters c ON c.id = b.chapter_id
        WHERE q.file_ref_id = f.id
          AND c.id = %s
          AND q.canonical_id IS NULL
          AND q.id = %s
    """, (
        new_text, Json(ao), valid_correct_index, correct_text, ax,
        new_identity, new_quality, corrected_at, source_identity,
        source_key, chapter_id, qid,
    ))

    # NOTE: an earlier version invalidated `similarity_reviews` rows here, but
    # that table never existed in this schema — the UPDATE threw UndefinedTable
    # and rolled back every successfully matched correction. Dedupe review state
    # lives in dedup_conflicts, which re-evaluates on the next dedupe pass.

    if file_ref_id:
        cur.execute("UPDATE files SET last_synced_at = NOW() WHERE id = %s",
                    (file_ref_id,))

    cur.execute("DELETE FROM correction_backlog WHERE uid = %s AND corrected_at = %s",
                (uid, corrected_at))
    cur.execute("""SELECT b.book_name,f.subtopic,f.file_id FROM files f
        JOIN books b ON b.id=f.book_id WHERE f.id=%s""", (file_ref_id,))
    location_row = cur.fetchone()
    source_location = source_location_key(*location_row[:2]) if location_row else None
    before_number = re.match(r"^\s*(\d+)\s*[\.\)]", str(bq))
    source_number = str(before_number.group(1)) if before_number else ""
    if not source_number and source_key.startswith("n:"):
        source_number = source_key[2:]
    if source_location is None:
        raise RuntimeError("correction matched but source file path could not be resolved")
    cur.execute("""
        INSERT INTO correction_source_aliases_v2
            (chapter_id, source_identity, source_location, source_number,
             question_text, options, correct_index, correct_text, explanation,
             identity_key, quality_score, corrected_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (chapter_id, source_identity, source_location, source_number,
          new_text, Json(ao), valid_correct_index, correct_text, ax,
          new_identity, new_quality, corrected_at))


    cur.execute("""
        INSERT INTO applied_corrections
            (uid, corrected_at, matched_by, question_id, file_id, notes,
             before_identity, after_identity)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (uid, corrected_at) DO UPDATE SET
            matched_by  = EXCLUDED.matched_by,
            question_id = EXCLUDED.question_id,
            file_id     = EXCLUDED.file_id,
            notes       = EXCLUDED.notes,
            applied_at  = NOW()
    """, (uid, corrected_at, why, qid, corr.get("fileId"),
          corr.get("note") or "", before_id, new_identity))

    return qid, f"applied_{why}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--level", default=None)
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    conn = db_connect(dsn)
    cur = conn.cursor()
    ensure_schema(cur, conn)

    level_id = None
    if args.level:
        cur.execute("SELECT id FROM levels WHERE level_key = %s", (args.level,))
        row = cur.fetchone()
        if not row:
            print(f"ERROR: level '{args.level}' not found.")
            sys.exit(1)
        level_id = row[0]

    def file_in_level(fid):
        if not level_id or not fid:
            return True
        cur.execute("""
            SELECT 1 FROM files f
            JOIN books b ON b.id = f.book_id
            JOIN chapters c ON c.id = b.chapter_id
            WHERE f.file_id = %s AND c.level_id = %s LIMIT 1
        """, (fid, level_id))
        return cur.fetchone() is not None

    since = 0 if args.force else get_last_read(cur)
    print(f"Fetching corrections since {since} "
          f"({'full scan' if args.force else 'incremental'})")

    try:
        resp = fetch_corrections(since)
    except BackendError as e:
        print(f"Backend error: {e}")
        sys.exit(1)

    if not resp.get("success"):
        print(f"Backend error: {resp.get('error')}")
        sys.exit(1)

    corrections = resp.get("corrections") or []
    server_now = resp.get("now") or 0
    print(f"Received {len(corrections)} correction(s)")

    applied = skipped = failed = 0
    errors = []

    for i, c in enumerate(corrections, 1):
        if args.level and not file_in_level(c.get("fileId")):
            skipped += 1
            continue
        cur.execute("SAVEPOINT correction_item")
        try:
            qid, why = apply_one(cur, c)
            cur.execute("RELEASE SAVEPOINT correction_item")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT correction_item")
            cur.execute("RELEASE SAVEPOINT correction_item")
            failed += 1
            errors.append((c.get("uid"), str(e)))
            cur.execute("""
                INSERT INTO correction_backlog (uid, corrected_at, correction, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (uid, corrected_at) DO UPDATE SET
                    correction = EXCLUDED.correction,
                    reason = EXCLUDED.reason,
                    attempts = correction_backlog.attempts + 1,
                    updated_at = now()
            """, (c.get("uid", "unknown"), c.get("correctedAt"), Json(c),
                  f"failed: {type(e).__name__}: {e}"[:500]))
            print(f"  [{i}] {c.get('uid')}: FAILED — {e}")
            continue

        if why.startswith("applied"):
            applied += 1
            print(f"  [{i}] {c.get('uid')}: {why} qid={qid}")
        else:
            skipped += 1
            if why != "already_applied":
                print(f"  [{i}] {c.get('uid')}: queued ({why})")

    print()
    print(f"  applied: {applied}")
    print(f"  skipped: {skipped}")
    print(f"  failed:  {failed}")

    if args.apply:
        if server_now:
            set_last_read(cur, server_now)
        conn.commit()
        cur.execute("SELECT count(*) FROM correction_backlog")
        print(f"  correction backlog: {cur.fetchone()[0]}")
        print("  Committed.")
    else:
        conn.rollback()
        print("  Dry-run — no changes written.")

    if errors:
        print()
        print("Errors:")
        for uid, msg in errors[:20]:
            print(f"  {uid}: {msg}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()