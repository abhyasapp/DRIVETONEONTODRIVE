#!/usr/bin/env python3
"""
dedupe_review.py  (v2)
======================
Read-only. Exports identity groups to CSV so you can eyeball them.
Uses identity_key (v7). Shows what WOULD be deleted and what is in conflict.

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
from dedupe.content_identity import canonical_answer

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
            SELECT q.identity_key, q.id, q.question_text, q.options,
                   q.correct_index, q.correct_text, q.quality_score,
                   EXTRACT(EPOCH FROM q.corrected_at)
            FROM questions q
            WHERE q.identity_key IN (
                SELECT identity_key FROM questions
                WHERE identity_key IS NOT NULL AND canonical_id IS NULL
                GROUP BY identity_key HAVING COUNT(*) > 1
            )
              AND q.canonical_id IS NULL
            ORDER BY q.identity_key, q.id
        """)

        groups = {}
        for row in cur.fetchall():
            groups.setdefault(row[0], []).append(row)

        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([
                "action", "reason", "group_size",
                "identity_short",
                "id", "question_text", "options",
                "correct_index", "correct_text",
                "quality_score", "keeper_id",
            ])

            total_del = 0
            total_conflict = 0

            for ident, rows in groups.items():
                def key(r):
                    qid, txt, opts, ci, ct, qs, corrected = (
                        r[1], r[2] or "", r[3], r[4], r[5], r[6], r[7]
                    )
                    return (
                        -float(corrected or 0),
                        -float(qs or 0),
                        qid,
                    )
                keeper = min(rows, key=key)

                answers = {
                    canonical_answer(r[5]) if r[5] is not None else "__unanswered__"
                    for r in rows
                }
                reason = "conflict" if len(answers) > 1 else "exact"

                if args.conflicts_only and reason != "conflict":
                    continue

                for r in rows:
                    qid = r[1]
                    action = "KEEP" if qid == keeper[1] else "DELETE"
                    if action == "DELETE" and reason == "exact":
                        total_del += 1
                    if reason == "conflict":
                        total_conflict += 1
                    w.writerow([
                        action, reason, len(rows), ident[:12],
                        qid, r[2], r[3],
                        r[4], r[5], r[6], keeper[1],
                    ])

        print(f"Wrote {args.out}")
        print(f"  rows that would be deleted (exact): {total_del}")
        print(f"  rows in conflict (skipped):         {total_conflict}")
        print()
        print("Note: conflicts are SKIPPED by dedupe_exact v7.")

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()