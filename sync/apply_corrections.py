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
from dedupe.content_identity import identity, compute_quality_score

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
            notes         TEXT,
            PRIMARY KEY (uid, corrected_at)
        )
    """)
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS corrected_at TIMESTAMPTZ")
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


def find_match(cur, before_question, before_options, before_identity):
    cur.execute("""
        SELECT id, question_text, options, file_ref_id
        FROM questions
        WHERE canonical_id IS NULL AND identity_key = %s
        LIMIT 5
    """, (before_identity,))
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0], "identity"
    if len(rows) > 1:
        return None, f"ambiguous_identity({len(rows)})"

    norm = normalize_question_text(before_question)
    cur.execute("""
        SELECT id, question_text, options, file_ref_id
        FROM questions
        WHERE canonical_id IS NULL
          AND regexp_replace(question_text, '^\\s*\\d+\\s*[\\.\\)]\\s*', '') = %s
          AND options = %s
        LIMIT 5
    """, (norm, Json(before_options)))
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0], "text_options"
    if len(rows) > 1:
        return None, f"ambiguous_text({len(rows)})"

    return None, "not_found"


def apply_one(cur, corr):
    uid = corr["uid"]
    corrected_at = corr["correctedAt"]

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
    if not isinstance(ao, list):
        return None, "invalid_after_options"

    before_id = identity(bq, bo)
    row, why = find_match(cur, bq, bo, before_id)
    if row is None:
        return None, why

    qid, _old_text, _old_opts, file_ref_id = row
    new_text = normalize_question_text(aq)
    new_identity = identity(aq, ao)
    new_quality = compute_quality_score(ax, ao, len(new_text))
    correct_text = ao[ac] if isinstance(ac, int) and 0 <= ac < len(ao) else None

    cur.execute("""
        UPDATE questions
        SET question_text = %s,
            options       = %s,
            correct_index = %s,
            correct_text  = %s,
            explanation   = COALESCE(NULLIF(%s, ''), explanation),
            identity_key  = %s,
            quality_score = %s,
            corrected_at  = %s
        WHERE id = %s
    """, (
        new_text, Json(ao),
        ac if isinstance(ac, int) else None,
        correct_text,
        ax,
        new_identity, new_quality,
        corrected_at, qid,
    ))

    cur.execute("""
        UPDATE similarity_reviews
           SET status = 'stale'
         WHERE status = 'pending'
           AND (q1_id = %s OR q2_id = %s)
    """, (qid, qid))

    if file_ref_id:
        cur.execute("UPDATE files SET last_synced_at = NOW() WHERE id = %s",
                    (file_ref_id,))

    cur.execute("""
        INSERT INTO applied_corrections
            (uid, corrected_at, matched_by, question_id, file_id, notes)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (uid, corrected_at) DO UPDATE SET
            matched_by  = EXCLUDED.matched_by,
            question_id = EXCLUDED.question_id,
            file_id     = EXCLUDED.file_id,
            notes       = EXCLUDED.notes,
            applied_at  = NOW()
    """, (uid, corrected_at, why, qid, corr.get("fileId"),
          corr.get("note") or ""))

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
        try:
            qid, why = apply_one(cur, c)
        except Exception as e:
            conn.rollback()
            failed += 1
            errors.append((c.get("uid"), str(e)))
            print(f"  [{i}] {c.get('uid')}: FAILED — {e}")
            continue

        if why.startswith("applied"):
            applied += 1
            print(f"  [{i}] {c.get('uid')}: {why} qid={qid}")
        else:
            skipped += 1
            if why != "already_applied":
                print(f"  [{i}] {c.get('uid')}: skip ({why})")

    print()
    print(f"  applied: {applied}")
    print(f"  skipped: {skipped}")
    print(f"  failed:  {failed}")

    if args.apply:
        if server_now:
            set_last_read(cur, server_now)
        conn.commit()
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