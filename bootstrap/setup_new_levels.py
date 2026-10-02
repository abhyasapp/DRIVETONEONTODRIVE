"""
setup_new_levels.py
-------------------
Populates the database for Level 5 and GK.

Level 7 is not touched.

Creates:
  • levels rows for level5 and gk
  • chapters, subtopics (from levels_data.py)
  • books, files (from levels_data.py manifests)
  • an extra subtopic per chapter: UNCLASSIFIED
"""

import os
import psycopg2
from dotenv import load_dotenv
from levels_data import (
    LEVEL5_STRUCTURE, GK_STRUCTURE,
    LEVEL5_FILES, GK_FILES,
)

load_dotenv()
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()


def setup_level(level_key, structure, files):
    label = structure["label"]
    print(f"\n=== {level_key}: {label} ===")

    # 1. level row
    cur.execute("""
        INSERT INTO levels (level_key, label)
        VALUES (%s, %s)
        ON CONFLICT (level_key) DO UPDATE SET label = EXCLUDED.label
        RETURNING id
    """, (level_key, label))
    level_id = cur.fetchone()[0]

    # 2. chapters + subtopics
    chapter_ids = {}
    for ch_key, ch in structure["chapters"].items():
        cur.execute("""
            INSERT INTO chapters (level_id, chapter_key, chapter_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (level_id, chapter_key)
            DO UPDATE SET chapter_name = EXCLUDED.chapter_name
            RETURNING id
        """, (level_id, ch_key, ch["name"]))
        chapter_id = cur.fetchone()[0]
        chapter_ids[ch_key] = chapter_id

        for code, title in ch["subtopics"].items():
            cur.execute("""
                INSERT INTO subtopics (chapter_id, code, title)
                VALUES (%s, %s, %s)
                ON CONFLICT (chapter_id, code)
                DO UPDATE SET title = EXCLUDED.title
            """, (chapter_id, code, title))

        # UNCLASSIFIED subtopic
        cur.execute("""
            INSERT INTO subtopics (chapter_id, code, title)
            VALUES (%s, 'UNCLASSIFIED', 'Unclassified questions')
            ON CONFLICT (chapter_id, code)
            DO UPDATE SET title = EXCLUDED.title
        """, (chapter_id,))

    # 3. books + files
    books = {}  # (chapter_key, book_name) -> book_id
    file_count = 0
    for ch_key, book_name, subtopic, file_id in files:
        chapter_id = chapter_ids.get(ch_key)
        if not chapter_id:
            print(f"  WARNING: chapter {ch_key} not found")
            continue

        key = (ch_key, book_name)
        if key not in books:
            cur.execute("""
                INSERT INTO books (chapter_id, book_name)
                VALUES (%s, %s)
                ON CONFLICT (chapter_id, book_name)
                DO UPDATE SET book_name = EXCLUDED.book_name
                RETURNING id
            """, (chapter_id, book_name))
            books[key] = cur.fetchone()[0]
        book_id = books[key]

        cur.execute("""
            INSERT INTO files (book_id, subtopic, file_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (book_id, subtopic)
            DO UPDATE SET file_id = EXCLUDED.file_id
        """, (book_id, subtopic, file_id))
        file_count += 1

    print(f"  chapters: {len(chapter_ids)}")
    print(f"  books:    {len(books)}")
    print(f"  files:    {file_count}")


setup_level("level5", LEVEL5_STRUCTURE, LEVEL5_FILES)
setup_level("gk",     GK_STRUCTURE,     GK_FILES)

conn.commit()

# summary
print("\n=== DB summary ===")
cur.execute("""
    SELECT l.level_key,
           COUNT(DISTINCT c.id) AS chapters,
           COUNT(DISTINCT b.id) AS books,
           COUNT(DISTINCT f.id) AS files,
           COUNT(DISTINCT s.id) AS subtopics
    FROM levels l
    LEFT JOIN chapters c ON c.level_id = l.id
    LEFT JOIN books b    ON b.chapter_id = c.id
    LEFT JOIN files f    ON f.book_id = b.id
    LEFT JOIN subtopics s ON s.chapter_id = c.id
    GROUP BY l.level_key
    ORDER BY l.level_key
""")
print(f"{'level':<10}{'chapters':>10}{'books':>10}{'files':>10}{'subtopics':>12}")
for lk, ch, bk, fl, st in cur.fetchall():
    print(f"{lk:<10}{ch:>10}{bk:>10}{fl:>10}{st:>12}")

cur.close()
conn.close()
print("\nDone.")