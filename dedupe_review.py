#!/usr/bin/env python3
"""
dedupe_review.py  (v4)
======================
Read-only. Exports identity groups to CSV so you can eyeball them.
Uses identity_key. Shows what WOULD be deleted and what is in conflict.

SCOPE (matches dedupe_exact v9): a group is one identity INSIDE ONE
CHAPTER. Inside a conflicted group, every copy that agrees with a sibling
is marked DELETE; only singleton copies without an agreeing sibling are kept.

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
            SELECT c.id, q.identity_key, q.id, q.question_text, q.options,
                   q.correct_index, q.correct_text, q.quality_score,
                   EXTRACT(EPOCH FROM q.corrected_at)
            FROM questions q
            JOIN files f    ON f.id = q.file_ref_id
            JOIN books b    ON b.id = f.book_id
            JOIN chapters c ON c.id = b.chapter_id
            WHERE (c.id, q.identity_key) IN (
                SELECT c.id, q.identity_key
                FROM questions q
                JOIN files f    ON f.id = q.file_ref_id
                JOIN books b    ON b.id = f.book_id
                JOIN chapters c ON c.id = b.chapter_id
                WHERE q.identity_key IS NOT NULL
                  AND q.canonical_id IS NULL
                GROUP BY c.id, q.identity_key HAVING COUNT(*) > 1
            )
              AND q.canonical_id IS NULL
            ORDER BY c.id, q.identity_key, q.id
        """)

        groups = {}
        for row in cur.fetchall():
            groups.setdefault((row[0], row[1]), []).append(row)

        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([
                "action", "reason", "group_size",
                "identity_short",
                "chapter", "id", "question_text", "options",
                "correct_index", "correct_text",
                "quality_score", "keeper_id",
            ])

            total_del = 0
            total_conflict = 0

            for (chapter, ident), rows in groups.items():
                def key(r):
                    qid, qs, corrected = r[2], r[7], r[8]
                    return (
                        -float(corrected or 0),
                        -float(qs or 0),
                        qid,
                    )

                # bucket by normalized answer (matches dedupe_exact v9)
                buckets = {}
                for r in rows:
                    k = canonical_answer(r[6]) if r[6] is not None else "__unanswered__"
                    buckets.setdefault(k, []).append(r)

                conflicted = len(buckets) > 1
                deletable_ids = set()
                keepers = set()
                for b in buckets.values():
                    if len(b) < 2:
                        continue          # singleton copy: nothing to merge
                    b_keeper = min(b, key=key)
                    keepers.add(b_keeper[2])
                    for r in b:
                        if r[2] != b_keeper[2]:
                            deletable_ids.add(r[2])

                if args.conflicts_only and not conflicted:
                    continue

                for r in rows:
                    qid = r[2]
                    action = "KEEP" if qid not in deletable_ids else "DELETE"
                    if action == "DELETE":
                        total_del += 1
                    elif conflicted:
                        total_conflict += 1
                    w.writerow([
                        action,
                        "conflict" if conflicted else "exact",
                        len(rows), ident[:12],
                        chapter, qid, r[3], r[4],
                        r[5], r[6], r[7],
                        ",".join(str(k) for k in sorted(keepers)),
                    ])

        print(f"Wrote {args.out}")
        print(f"  rows that would be deleted (copies that agree with a sibling): {total_del}")
        print(f"  rows kept in conflict (no agreeing sibling):                    {total_conflict}")
        print()
        print("Note: matches dedupe_exact v9 -- agreeing copies are merged even")
        print("      inside a group where other copies disagree on the answer.")

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()