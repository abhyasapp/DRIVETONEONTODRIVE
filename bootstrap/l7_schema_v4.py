"""
l7_schema_v4.py
---------------
Adds columns for dedup, confidence, manual override, and Drive tracking.
Safe to re-run.
"""

import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

# questions: fingerprint for dedup
cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
cur.execute("CREATE INDEX IF NOT EXISTS idx_q_fingerprint ON questions(fingerprint);")

# question_subtopics: confidence + matched keywords + who classified it
cur.execute("ALTER TABLE question_subtopics ADD COLUMN IF NOT EXISTS confidence INTEGER;")
cur.execute("ALTER TABLE question_subtopics ADD COLUMN IF NOT EXISTS matched_keywords JSONB;")
cur.execute("ALTER TABLE question_subtopics ADD COLUMN IF NOT EXISTS classified_by TEXT DEFAULT 'auto';")

# files: track when Drive last changed + when we last synced
cur.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS drive_modified_time TIMESTAMPTZ;")
cur.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ;")

conn.commit()
print("Schema v4 ready.")
cur.close()
conn.close()