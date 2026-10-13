#!/usr/bin/env python3
"""
apply_corrections.py
--------------------
Pull corrections from the Abhyas Apps Script backend and apply them to
Neon BEFORE dedupe and export.

Run order in run_pipeline.py:  sync -> corrections -> dedupe -> classify -> export.

Usage:
    python apply_corrections.py                        # dry-run
    python apply_corrections.py --apply                # write
    python apply_corrections.py --apply --force        # ignore last_read, rescan all
    python apply_corrections.py --apply --level level5 # one level only
"""
import argparse
import json
import os
import re
import sys
import urllib.request

from psycopg2.extras import Json
from dotenv import load_dotenv

from utils.db import connect as db_connect

from dedupe.dedupe_exact import signature

load_dotenv()

APPS_URL = os.getenv("ABHYAS_APPS_SCRIPT_URL")
SYNC_KEY = os.getenv("ABHYAS_SYNC_KEY")
FETCH_TIMEOUT = 90


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
    body = json.dumps({
        "action": "syncCorrectionsSince",
        "key": SYNC_KEY,
        "since": since_ms,
        "limit": limit,
    }).encode("utf-8")
    req = urllib.request.Request(
        APPS_URL, data=body,
        headers={"Content-Type": "text/plain"}, method="POST")
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def normalize_question_text(text):
    return re.sub(r"^\s*\d+\s*[\.\)]\s*", "", str(text or "")).strip()


def find_match(cur, before_question, before_options, before_hash):
    cur.execute("""
        SELECT id, question_text, options, file_ref_id
        FROM questions
        WHERE canonical_id IS NULL AND strict_sig = %s
        LIMIT 5
    """, (before_hash,))
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0], "strict_sig"
    if len(rows) > 1:
        return None, f"ambiguous_strict_sig({len(rows)})"

    norm_text = normalize_question_text(before_question)
    cur.execute("""
        SELECT id, question_text, options, file_ref_id
        FROM questions
        WHERE canonical_id IS NULL
          AND regexp_replace(question_text, '^\\s*\\d+\\s*[\\.\\)]\\s*', '') = %s
          AND options = %s
        LIMIT 5
    """, (norm_text, Json(before_options)))
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0], "text_options"
    if len(rows) > 1:
        return None, f"ambiguous_text({len(rows)})"

    return None, "not_found"


def apply_one(cur, corr):
    uid = corr["uid"]
    corrected_at = corr["correctedAt"]

    cur.execute("""
        SELECT 1 FROM applied_corrections
        WHERE uid = %s AND corrected_at = %s
    """, (uid, corrected_at))
    if cur.fetchone():
        return None, "already_applied"

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

    before_hash = signature(bq, bo)
    row, why = find_match(cur, bq, bo, before_hash)
    if row is None:
        return None, why

    qid, _old_text, _old_opts, file_ref_id = row
    new_text = normalize_question_text(aq)
    new_sig  = signature(aq, ao)
    correct_text = ao[ac] if isinstance(ac, int) and 0 <= ac < len(ao) else None

    cur.execute("""
        UPDATE questions
        SET question_text = %s,
            options       = %s,
            correct_index = %s,
            correct_text  = %s,
            explanation   = COALESCE(NULLIF(%s, ''), explanation),
            strict_sig    = %s,
            corrected_at  = %s
        WHERE id = %s
    """, (
        new_text,
        Json(ao),
        ac if isinstance(ac, int) else None,
        correct_text,
        ax,
        new_sig,
        corrected_at,
        qid,
    ))

    if file_ref_id:
        cur.execute("UPDATE files SET last_synced_at = NOW() WHERE id = %s",
                    (file_ref_id,))

    cur.execute("""
        INSERT INTO applied_corrections
            (uid, corrected_at, matched_by, question_id, file_id, notes)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (uid, corrected_at, why, qid, corr.get("fileId"), corr.get("note") or ""))

    return qid, f"applied_{why}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--level", default=None)
    args = ap.parse_args()

    if not APPS_URL or not SYNC_KEY:
        print("ERROR: ABHYAS_APPS_SCRIPT_URL and ABHYAS_SYNC_KEY must be set.")
        sys.exit(1)
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
            WHERE f.file_id = %s AND c.level_id = %s
            LIMIT 1
        """, (fid, level_id))
        return cur.fetchone() is not None

    since = 0 if args.force else get_last_read(cur)
    print(f"Fetching corrections since {since} "
          f"({'full scan' if args.force else 'incremental'})")

    try:
        resp = fetch_corrections(since)
    except Exception as e:
        print(f"ERROR fetching: {e}")
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
        print("  Re-run with --apply to write.")

    if errors:
        print()
        print("Errors:")
        for uid, msg in errors[:20]:
            print(f"  {uid}: {msg}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
