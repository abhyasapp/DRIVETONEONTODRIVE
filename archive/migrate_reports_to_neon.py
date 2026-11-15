#!/usr/bin/env python3
"""
migrate_reports_to_neon.py
---------------------------
One-time migration: copy every Supabase question_report into a Neon
`reported_questions` table so the Drive->Neon store becomes the single home
for reported questions. Idempotent (upserts by report id).

Run:
    python sync/migrate_reports_to_neon.py            # dry-run (prints counts)
    python sync/migrate_reports_to_neon.py --apply    # writes
"""
import argparse
import json
import os

import requests
from dotenv import load_dotenv

from utils.db import connect as db_connect

load_dotenv()

APPS_URL = os.getenv("ABHYAS_APPS_SCRIPT_URL")
SYNC_KEY = os.getenv("ABHYAS_SYNC_KEY")
FETCH_TIMEOUT = 90


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS reported_questions (
    id              TEXT PRIMARY KEY,
    uid             TEXT,
    qid             BIGINT,
    file_id         TEXT,
    question_snapshot TEXT,
    options_snapshot  TEXT,
    correct_index   INTEGER,
    chosen_index    INTEGER,
    reason          TEXT,
    note            TEXT,
    reported_by     TEXT,
    reported_at     TIMESTAMPTZ,
    status          TEXT,
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,
    corrected       BOOLEAN DEFAULT FALSE,
    corrected_snapshot TEXT,
    section         TEXT,
    app_version     TEXT,
    migrated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def fetch_reports(limit=100000):
    if not APPS_URL or not SYNC_KEY:
        raise RuntimeError("ABHYAS_APPS_SCRIPT_URL / ABHYAS_SYNC_KEY not set")
    body = json.dumps({"action": "syncreportssince", "key": SYNC_KEY,
                       "since": 0, "limit": limit})
    r = requests.post(APPS_URL, data=body,
                      headers={"Content-Type": "text/plain"},
                      timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or "fetch failed")
    return payload.get("reports", [])


def to_ts(v):
    if not v:
        return None
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    reports = fetch_reports()
    print(f"Fetched {len(reports)} reports from Supabase")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(CREATE_TABLE)
    conn.commit()

    if not args.apply:
        cur.execute("SELECT COUNT(*) FROM reported_questions")
        print(f"Neon reported_questions currently has {cur.fetchone()[0]} rows (dry run, no writes)")
        cur.close()
        conn.close()
        return

    inserted = 0
    for r in reports:
        cur.execute(
            """INSERT INTO reported_questions
               (id, uid, qid, file_id, question_snapshot, options_snapshot,
                correct_index, chosen_index, reason, note, reported_by,
                reported_at, status, resolved_at, resolved_by, corrected,
                corrected_snapshot, section, app_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET
                 uid=EXCLUDED.uid, qid=EXCLUDED.qid, file_id=EXCLUDED.file_id,
                 question_snapshot=EXCLUDED.question_snapshot,
                 options_snapshot=EXCLUDED.options_snapshot,
                 correct_index=EXCLUDED.correct_index,
                 chosen_index=EXCLUDED.chosen_index,
                 reason=EXCLUDED.reason, note=EXCLUDED.note,
                 reported_by=EXCLUDED.reported_by, reported_at=EXCLUDED.reported_at,
                 status=EXCLUDED.status, resolved_at=EXCLUDED.resolved_at,
                 resolved_by=EXCLUDED.resolved_by, corrected=EXCLUDED.corrected,
                 corrected_snapshot=EXCLUDED.corrected_snapshot,
                 section=EXCLUDED.section, app_version=EXCLUDED.app_version,
                 migrated_at=now()""",
            (
                str(r["id"]), r.get("uid"), r.get("qid"), r.get("fileId"),
                r.get("questionSnapshot"), r.get("optionsSnapshot"),
                r.get("correctIndex"), r.get("chosenIndex"), r.get("reason"),
                r.get("note"), r.get("reportedBy"), r.get("reportedAt"),
                r.get("status"), (r.get("resolvedAt") or None), r.get("resolvedBy"),
                bool(r.get("corrected")), r.get("correctedSnapshot"),
                r.get("section"), r.get("appVersion"),
            ),
        )
        inserted += 1
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM reported_questions")
    total = cur.fetchone()[0]
    print(f"Migrated {inserted} reports; Neon now has {total} rows")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()