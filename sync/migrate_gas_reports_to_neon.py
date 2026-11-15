#!/usr/bin/env python3
"""
migrate_gas_reports_to_neon.py
-------------------------------
One-time migration: copy the GAS Q-reports sheet (exported via
exportAllQReports() in the Apps Script editor) into Neon `reported_questions`.
Idempotent (upsert by report id).

Usage:
    # 1. In Apps Script editor: run exportAllQReports(), copy the JSON.
    # 2. Save it as e.g. gas_reports.json in the project root.
    # 3. Run:
    python sync/migrate_gas_reports_to_neon.py --file gas_reports.json            # dry run
    python sync/migrate_gas_reports_to_neon.py --file gas_reports.json --apply     # writes
"""
import argparse
import json
import os

from dotenv import load_dotenv

from utils.db import connect as db_connect

load_dotenv()


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


def to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def to_ts(v):
    if v is None or v == "":
        return None
    return v


def to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v) if v is not None else False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as f:
        payload = json.load(f)
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if rows is None:
        # Maybe the file is a raw array
        rows = payload if isinstance(payload, list) else []
    print(f"Loaded {len(rows)} reports from {args.file}")

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
    for r in rows:
        rid = str(r.get("id") or "").strip()
        if not rid:
            continue
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
                rid,
                r.get("uid"),
                to_int(r.get("qid")),
                r.get("fileId"),
                r.get("questionSnapshot"),
                r.get("optionsSnapshot"),
                to_int(r.get("correctIndex")),
                to_int(r.get("chosenIndex")),
                r.get("reason"),
                r.get("note"),
                r.get("reportedBy"),
                to_ts(r.get("reportedAt")),
                r.get("status"),
                to_ts(r.get("resolvedAt")),
                r.get("resolvedBy"),
                to_bool(r.get("corrected")),
                r.get("correctedSnapshot"),
                r.get("section"),
                r.get("appVersion"),
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
EOF