#!/usr/bin/env python3
"""
normalize_books.py
------------------
Normalizes books.book_name values in the DB to a small canonical set:

    DPARSAD, RK SHRESTHA, Sunil Sah, GATE, SAARC

Files whose book name can't be mapped are reported but not changed.

Steps:
  1. Show current book names and counts (read-only preview).
  2. Apply the mapping if --apply is passed.
  3. Report any ambiguous/unmapped names.

Run:
    python normalize_books.py             # preview only
    python normalize_books.py --apply     # rewrite names
"""

import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()


# ── canonicalization rule (identical to generator's canon_book) ────────

def canon(name):
    n = (name or "").strip().lower()
    n = n.replace(".", " ").replace("_", " ").replace("-", " ")
    n = " ".join(n.split())

    # explicit overrides first
    if n in ("rk",):
        return "RK SHRESTHA"
    # GK chapter-folder names caught as book names → GK default book
    if n in ("international affairs", "planning and management"):
        return "GATE"

    # pattern rules
    if "parsad" in n or "prasad" in n:
        return "DPARSAD"
    if "shrestha" in n or "sherestha" in n:
        return "RK SHRESTHA"
    if "sunil" in n:
        return "Sunil Sah"
    if "gate" in n:
        return "GATE"
    if "saarc" in n:
        return "SAARC"
    return None    # cannot map — leave alone


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually update the DB. Default: preview only.")
    args = p.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()

    # ── 1. what's there now ───────────────────────────────────────────
    cur.execute("""
        SELECT book_name, COUNT(*) AS files
        FROM books
        GROUP BY book_name
        ORDER BY book_name
    """)
    rows = cur.fetchall()
    print(f"{'book_name':<30}{'files':>8}{'-> canonical':>20}")
    print("-" * 60)
    changes = {}       # old -> new
    unmapped = []
    for name, n in rows:
        target = canon(name)
        arrow = target if target else "(skip)"
        print(f"{name!r:<30}{n:>8}{arrow:>20}")
        if target and target != name:
            changes[name] = target
        elif not target:
            unmapped.append(name)

    print()
    if unmapped:
        print(f"{len(unmapped)} book name(s) can't be mapped — will be left as-is:")
        for u in unmapped:
            print(f"  {u!r}")
        print()

    if not changes:
        print("Nothing to change.")
        cur.close(); conn.close()
        return

    print(f"{len(changes)} distinct book name(s) to canonicalize:")
    for old, new in sorted(changes.items()):
        print(f"  {old!r}  ->  {new!r}")

    if not args.apply:
        print()
        print("Preview only. Re-run with --apply to rewrite.")
        cur.close(); conn.close()
        return

    # ── 2. apply ──────────────────────────────────────────────────────
    print()
    print("Applying ...")
    for old, new in changes.items():
        # Move files rows from old -> new; if the target already exists,
        # we need to merge rather than rename (unique(book_id, subtopic)
        # would collide otherwise). Simplest merge: reassign the files
        # rows to the canonical book's id, delete the old book row.
        cur.execute("SELECT id, chapter_id FROM books WHERE book_name = %s", (old,))
        old_rows = cur.fetchall()
        for old_id, chapter_id in old_rows:
            # does a canonical book already exist for this chapter?
            cur.execute("""
                SELECT id FROM books
                WHERE chapter_id = %s AND book_name = %s
            """, (chapter_id, new))
            row = cur.fetchone()
            if row:
                new_id = row[0]
                # Reassign files to the canonical book, skipping collisions
                cur.execute("""
                    UPDATE files
                    SET book_id = %s
                    WHERE book_id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM files f2
                          WHERE f2.book_id = %s AND f2.subtopic = files.subtopic
                      )
                """, (new_id, old_id, new_id))
                # Anything still pointing at old_id has a duplicate subtopic
                # under new_id — leave those alone (rare; user can review)
                cur.execute("SELECT COUNT(*) FROM files WHERE book_id = %s", (old_id,))
                leftover = cur.fetchone()[0]
                if leftover:
                    print(f"  ! {old!r}/{chapter_id}: {leftover} row(s) "
                          f"still under old book — duplicate subtopic with new")
                cur.execute("DELETE FROM books WHERE id = %s", (old_id,))
            else:
                # No collision — just rename in place
                cur.execute("UPDATE books SET book_name = %s WHERE id = %s",
                            (new, old_id))
        print(f"  {old!r}  ->  {new!r}")

    conn.commit()

    # ── 3. verify ─────────────────────────────────────────────────────
    cur.execute("""
        SELECT book_name, COUNT(*) FROM books
        GROUP BY book_name ORDER BY book_name
    """)
    print("\nAfter:")
    for name, n in cur.fetchall():
        print(f"  {name!r:<28} {n} chapter(s)")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()