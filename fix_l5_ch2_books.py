#!/usr/bin/env python3
"""
fix_l5_ch2_books.py
-------------------
Level 5 chapter 2 has 6 source files whose subtopic names say "sunil"
but whose book is currently 'DPARSAD'. This reassigns them (and only
them) to a new 'Sunil Sah' book under the same chapter.

Idempotent: safe to re-run.

Usage:
    python fix_l5_ch2_books.py             # preview
    python fix_l5_ch2_books.py --apply     # rewrite
"""

import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

SUNIL_FIDS = [
    "1DCi7TZlsRLXbswMXZ_phkNSvpvR4qEYC",
    "1l2_oKmLGjbMZJAY2EXniIcsXBjgox1LI",
    "1D-Q5Dx7r_PeLb8tuQSJrfdDsSFwje__V",
    "1Ofpj_R63e8ibarImI4Kx4Hjk1GZ5aknd",
    "1WLSUMqyN8bnj9WuQPGRNxK0ssMqDtJ-O",
    "1bQ-eFt4DnPTkejie6Jf435EtGEiwVobO",
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()

    # Find the level5 chapter 2
    cur.execute("""
        SELECT c.id
        FROM chapters c
        JOIN levels l ON l.id = c.level_id
        WHERE l.level_key = 'level5' AND c.chapter_key = '2'
    """)
    row = cur.fetchone()
    if not row:
        print("ERROR: level5 chapter 2 not found.")
        cur.close(); conn.close()
        sys.exit(1)
    chapter_id = row[0]

    # What do the target files currently look like?
    cur.execute("""
        SELECT f.id, b.book_name, f.subtopic
        FROM files f
        JOIN books b ON b.id = f.book_id
        WHERE f.file_id = ANY(%s)
        ORDER BY f.subtopic
    """, (SUNIL_FIDS,))
    rows = cur.fetchall()
    print(f"Target files ({len(rows)} found):")
    for fid, book, sub in rows:
        print(f"  {book!r:<20} {sub!r}")

    # Create or find the Sunil Sah book under chapter 2
    cur.execute("""
        INSERT INTO books (chapter_id, book_name)
        VALUES (%s, 'Sunil Sah')
        ON CONFLICT (chapter_id, book_name)
        DO UPDATE SET book_name = EXCLUDED.book_name
        RETURNING id
    """, (chapter_id,))
    sunil_book_id = cur.fetchone()[0]
    print(f"\nSunil Sah book id: {sunil_book_id}")

    if not args.apply:
        print("\nPreview only. Re-run with --apply to rewrite.")
        cur.close(); conn.close()
        return

    # Reassign the 6 files
    cur.execute("""
        UPDATE files
        SET book_id = %s
        WHERE file_id = ANY(%s)
    """, (sunil_book_id, SUNIL_FIDS))
    moved = cur.rowcount
    print(f"Moved {moved} file(s) to Sunil Sah")

    conn.commit()

    # After
    cur.execute("""
        SELECT f.id, b.book_name, f.subtopic
        FROM files f
        JOIN books b ON b.id = f.book_id
        WHERE f.file_id = ANY(%s)
        ORDER BY f.subtopic
    """, (SUNIL_FIDS,))
    print("\nAfter:")
    for fid, book, sub in cur.fetchall():
        print(f"  {book!r:<20} {sub!r}")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
    