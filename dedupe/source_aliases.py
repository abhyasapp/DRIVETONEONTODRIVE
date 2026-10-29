"""Persistent source-location aliases used to reapply adopted question edits."""


def ensure_correction_source_alias_schema(cur):
    """Create the scoped alias store without altering any legacy table."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS correction_source_aliases_v2 (
            alias_id BIGSERIAL PRIMARY KEY,
            chapter_id INTEGER NOT NULL,
            source_identity TEXT NOT NULL,
            source_location TEXT NOT NULL,
            source_number TEXT NOT NULL DEFAULT '',
            question_text TEXT NOT NULL,
            options JSONB NOT NULL,
            correct_index INTEGER,
            correct_text TEXT,
            explanation TEXT,
            identity_key TEXT NOT NULL,
            quality_score REAL NOT NULL DEFAULT 0,
            corrected_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_correction_source_alias_v2_scope
        ON correction_source_aliases_v2
            (chapter_id, source_location, source_number, corrected_at DESC, alias_id DESC)
    """)
