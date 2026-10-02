"""
l7_load_hierarchy.py
--------------------
Populates levels/chapters/books/files/subtopics for LEVEL 7.
Uses chompjs (Python 3.12 compatible) instead of js2py.
Safe to re-run.
"""

import os, re, json
import psycopg2
import chompjs
from dotenv import load_dotenv

load_dotenv()
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()


def extract_js_object(js_text, var_name):
    """
    Find `const VAR_NAME = { ... };` or `VAR_NAME = { ... };`
    and return the { ... } part as a Python dict using chompjs.
    """
    # match "VAR_NAME = {  ...  };" (greedy up to last closing brace before semicolon)
    pattern = rf"{var_name}\s*=\s*(\{{.*?\}})\s*;"
    m = re.search(pattern, js_text, re.DOTALL)
    if not m:
        raise RuntimeError(f"Could not find {var_name} in JS file")
    return chompjs.parse_js_object(m.group(1))


print("Reading chapters-data.js ...")
with open("chapters-data.js", "r", encoding="utf-8") as f:
    js_text = f.read()

CH_NAMES    = extract_js_object(js_text, "CH_NAMES")
LEVEL_LABELS = extract_js_object(js_text, "LEVEL_LABELS")
DRIVE       = extract_js_object(js_text, "DRIVE")
print("  done.")

LEVEL = "level7"
LABEL = LEVEL_LABELS[LEVEL]
CHAPTERS = DRIVE[LEVEL]
CH_NAMES_L7 = CH_NAMES[LEVEL]

SKIP_BOOKS = {"Abhyas"}

# --- level ---
cur.execute("""
    INSERT INTO levels (level_key, label)
    VALUES (%s, %s)
    ON CONFLICT (level_key) DO UPDATE SET label = EXCLUDED.label
    RETURNING id
""", (LEVEL, LABEL))
level_id = cur.fetchone()[0]
print(f"Level: {LABEL}  (id={level_id})")

# --- chapters / books / files / subtopics ---
for chapter_key, book_map in CHAPTERS.items():
    chapter_name = CH_NAMES_L7.get(chapter_key, f"Chapter {chapter_key}")
    cur.execute("""
        INSERT INTO chapters (level_id, chapter_key, chapter_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (level_id, chapter_key)
        DO UPDATE SET chapter_name = EXCLUDED.chapter_name
        RETURNING id
    """, (level_id, chapter_key, chapter_name))
    chapter_id = cur.fetchone()[0]
    print(f"  Ch {chapter_key}: {chapter_name}")

    # subtopics from Abhyas keys
    if "Abhyas" in book_map:
        for key in book_map["Abhyas"]:
            m = re.match(r"^(\S+)\s+(.*)$", key.strip())
            code, title = (m.group(1), m.group(2)) if m else (key, key)
            cur.execute("""
                INSERT INTO subtopics (chapter_id, code, title)
                VALUES (%s, %s, %s)
                ON CONFLICT (chapter_id, code)
                DO UPDATE SET title = EXCLUDED.title
            """, (chapter_id, code, title))

    # source books (skip Abhyas and _meta)
    for book_name, subtopic_map in book_map.items():
        if book_name in SKIP_BOOKS or book_name.startswith("_"):
            continue
        cur.execute("""
            INSERT INTO books (chapter_id, book_name)
            VALUES (%s, %s)
            ON CONFLICT (chapter_id, book_name)
            DO UPDATE SET book_name = EXCLUDED.book_name
            RETURNING id
        """, (chapter_id, book_name))
        book_id = cur.fetchone()[0]

        for subtopic, file_id in subtopic_map.items():
            if not file_id:
                continue
            cur.execute("""
                INSERT INTO files (book_id, subtopic, file_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (book_id, subtopic)
                DO UPDATE SET file_id = EXCLUDED.file_id
            """, (book_id, subtopic, file_id))

conn.commit()

# --- summary ---
cur.execute("""
    SELECT
      (SELECT COUNT(*) FROM chapters   WHERE level_id = %s)          AS ch,
      (SELECT COUNT(*) FROM books b JOIN chapters c ON c.id=b.chapter_id
         WHERE c.level_id = %s)                                       AS bk,
      (SELECT COUNT(*) FROM files f JOIN books b ON b.id=f.book_id
         JOIN chapters c ON c.id=b.chapter_id WHERE c.level_id = %s)  AS fl,
      (SELECT COUNT(*) FROM subtopics s JOIN chapters c ON c.id=s.chapter_id
         WHERE c.level_id = %s)                                       AS st
""", (level_id, level_id, level_id, level_id))
ch, bk, fl, st = cur.fetchone()
print(f"\nSummary:")
print(f"  Chapters:  {ch}")
print(f"  Books:     {bk}")
print(f"  Files:     {fl}")
print(f"  Subtopics: {st}")
print("\nDone.")

cur.close()
conn.close()