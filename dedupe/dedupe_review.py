#!/usr/bin/env python3
"""
dedupe_review.py
================

Read-only. Exports everything dedupe_exact.py --apply would delete to
a CSV, so you can eyeball the rows before pulling the trigger.

USAGE
-----
    python dedupe_review.py                  # writes dedupe_review.csv
    python dedupe_review.py --out review.csv
    python dedupe_review.py --conflicts-only # only answer disagreements
"""

import argparse
import csv
import os
import sys
from dotenv import load_dotenv

from utils.db import connect as db_connect

load_dotenv()


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="dedupe_review.csv")
    p.add_argument("--conflicts-only", action="store_true",
                   help="Only export groups where answers disagree.")
    return p.parse_args()


def main():
    args = parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)

    conn = db_connect(dsn)
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT q.strict_sig, q.id, q.question_text, q.options,
                   q.correct_index, q.correct_text,
                   EXTRACT(EPOCH FROM f.last_synced_at),
                   EXTRACT(EPOCH FROM q.corrected_at)
            FROM questions q
            LEFT JOIN files f ON f.id = q.file_ref_id
            WHERE q.strict_sig IN (
                SELECT strict_sig FROM questions
                WHERE strict_sig IS NOT NULL
                GROUP BY strict_sig HAVING COUNT(*) > 1
            )
            ORDER BY q.strict_sig, q.id
        """)

        groups = {}
        for row in cur.fetchall():
            groups.setdefault(row[0], []).append(row)

        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([
                "action", "reason", "group_size",
                "strict_sig_short",
                "id", "question_text", "options",
                "correct_index", "correct_text",
                "synced_at", "keeper_id",
            ])

            total_del = 0
            total_conflict = 0

            for sig, rows in groups.items():
                def key(r):
                    qid, txt, opts, ci, ct, synced, corrected = (
                        r[1], r[2] or "", r[3], r[4], r[5], r[6], r[7]
                    )
                    return (
                        -float(corrected or 0),
                        -float(synced or 0),
                        0 if (ci is not None or ct is not None) else 1,
                        -len(txt),
                        qid,
                    )
                keeper = min(rows, key=key)

                answers = {
                    (r[4], r[5]) for r in rows
                    if r[4] is not None or r[5] is not None
                }
                reason = "conflict" if len(answers) > 1 else "exact"

                if args.conflicts_only and reason != "conflict":
                    continue

                for r in rows:
                    qid = r[1]
                    action = "KEEP" if qid == keeper[1] else "DELETE"
                    if action == "DELETE":
                        total_del += 1
                        if reason == "conflict":
                            total_conflict += 1
                    w.writerow([
                        action, reason, len(rows), sig[:12],
                        qid, r[2], r[3],
                        r[4], r[5], r[6], keeper[1],
                    ])

        print(f"Wrote {args.out}")
        print(f"  rows to delete:       {total_del}")
        print(f"  of which conflicts:   {total_conflict}")
        print()
        print("Open the CSV in Excel. Filter action=DELETE, reason=conflict")
        print("to review the risky ones first.")

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
