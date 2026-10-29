"""Transactional question merge with child preservation and an audit snapshot."""

import json

from psycopg2 import sql
from psycopg2.extras import Json

from dedupe.content_identity import answer_identity


def _answer_key(correct_text, correct_index=None, options=None):
    return answer_identity(correct_text, correct_index, options)


def ensure_merge_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dedup_deletions (
            id BIGSERIAL PRIMARY KEY,
            signature TEXT NOT NULL,
            canonical_id BIGINT NOT NULL,
            deleted_id BIGINT NOT NULL,
            question_text TEXT,
            options JSONB,
            correct_index INTEGER,
            correct_text TEXT,
            reason TEXT NOT NULL,
            snapshot JSONB,
            merged_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cur.execute("ALTER TABLE dedup_deletions ADD COLUMN IF NOT EXISTS snapshot JSONB")
    cur.execute("ALTER TABLE dedup_deletions ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dedupe_source_tombstones (
            id BIGSERIAL PRIMARY KEY,
            file_ref_id BIGINT NOT NULL,
            source_question_key TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            deleted_question_id BIGINT NOT NULL,
            keeper_id BIGINT NOT NULL,
            reason TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (file_ref_id, source_question_key, identity_key)
        )
    """)


def discover_question_child_fks(cur):
    """Return all simple FK columns that point to questions, including CASCADE FKs."""
    cur.execute("""
        SELECT ns.nspname, rel.relname, att.attname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        JOIN pg_attribute att
          ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
        WHERE con.contype = 'f'
          AND con.confrelid = 'questions'::regclass
          AND array_length(con.conkey, 1) = 1
        ORDER BY ns.nspname, rel.relname, att.attname
    """)
    return cur.fetchall()


def _json_rows(cur, schema_name, table_name, column_name, qid):
    statement = sql.SQL("SELECT to_jsonb(child) FROM {}.{} child WHERE {} = %s").format(
        sql.Identifier(schema_name), sql.Identifier(table_name), sql.Identifier(column_name)
    )
    cur.execute(statement, (qid,))
    return [row[0] for row in cur.fetchall()]


def _move_images(cur, keeper_id, duplicate_id):
    cur.execute("""
        INSERT INTO question_images (question_id, mime_type, data_base64, position)
        SELECT %s, src.mime_type, src.data_base64,
               COALESCE((SELECT MAX(dst.position) FROM question_images dst
                         WHERE dst.question_id = %s), -1)
               + ROW_NUMBER() OVER (ORDER BY src.position, src.id)
        FROM question_images src
        WHERE src.question_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM question_images dst
              WHERE dst.question_id = %s
                AND dst.mime_type IS NOT DISTINCT FROM src.mime_type
                AND dst.data_base64 IS NOT DISTINCT FROM src.data_base64
          )
    """, (keeper_id, keeper_id, duplicate_id, keeper_id))
    cur.execute("DELETE FROM question_images WHERE question_id = %s", (duplicate_id,))


def _move_subtopics(cur, keeper_id, duplicate_id):
    cur.execute("""
        INSERT INTO question_subtopics
            (question_id, subtopic_id, confidence, matched_keywords, classified_by)
        SELECT %s, src.subtopic_id, src.confidence, src.matched_keywords, src.classified_by
        FROM question_subtopics src
        WHERE src.question_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM question_subtopics dst
              WHERE dst.question_id = %s AND dst.subtopic_id = src.subtopic_id
          )
    """, (keeper_id, duplicate_id, keeper_id))
    cur.execute("DELETE FROM question_subtopics WHERE question_id = %s", (duplicate_id,))


def merge_question_rows(cur, keeper_id, duplicate_id, *, reason="exact",
                        signature=None, child_fks=None, keeper_before=None,
                        source_question_key_override=None):
    """Merge duplicate into keeper; abort on unsafe/cross-chapter or FK conflicts."""
    if keeper_id == duplicate_id:
        raise ValueError("keeper and duplicate IDs must differ")
    cur.execute("""
        SELECT q.id, c.id, q.question_text, q.options, q.correct_index,
               q.correct_text
        FROM questions q
        JOIN files f ON f.id = q.file_ref_id
        JOIN books b ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id
        WHERE q.id = ANY(%s)
        ORDER BY q.id
    """, ([keeper_id, duplicate_id],))
    rows = cur.fetchall()
    by_id = {row[0]: row for row in rows}
    if keeper_id not in by_id or duplicate_id not in by_id:
        raise ValueError("keeper or duplicate question no longer exists in a chapter")
    if by_id[keeper_id][1] != by_id[duplicate_id][1]:
        raise ValueError("questions must belong to the same chapter")
    keeper_answer = _answer_key(by_id[keeper_id][5], by_id[keeper_id][4], by_id[keeper_id][3])
    duplicate_answer = _answer_key(
        by_id[duplicate_id][5], by_id[duplicate_id][4], by_id[duplicate_id][3]
    )
    if keeper_answer == "__unanswered__" or duplicate_answer == "__unanswered__":
        raise ValueError("cannot merge unanswered questions; verify both answers first")
    if keeper_answer != duplicate_answer:
        raise ValueError("cannot merge questions with conflicting correct answers")

    child_fks = child_fks if child_fks is not None else discover_question_child_fks(cur)
    snapshot = {"question": None, "child_rows": {}, "keeper_before": keeper_before}
    cur.execute("SELECT to_jsonb(q) FROM questions q WHERE id = %s", (duplicate_id,))
    snapshot["question"] = cur.fetchone()[0]
    deleted_question = snapshot["question"] or {}
    file_ref_id = deleted_question.get("file_ref_id")
    source_key = (source_question_key_override
                  or deleted_question.get("source_question_key") or "")
    content_key = deleted_question.get("identity_key") or ""
    identity_key = deleted_question.get("source_identity") or content_key
    if file_ref_id and identity_key:
        if not source_key:
            from dedupe.content_identity import source_question_key
            raw = deleted_question.get("raw") or {}
            source_key = source_question_key(
                deleted_question.get("question_number"),
                raw.get("q", deleted_question.get("question_text", "")),
                raw.get("options") or deleted_question.get("options") or [],
            )
        cur.execute("""
            INSERT INTO dedupe_source_tombstones
                (file_ref_id,source_question_key,identity_key,deleted_question_id,keeper_id,reason)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(file_ref_id,source_question_key,identity_key) DO UPDATE SET
                keeper_id=EXCLUDED.keeper_id,reason=EXCLUDED.reason,created_at=now()
        """, (file_ref_id,source_key,identity_key,duplicate_id,keeper_id,reason))
        if content_key and content_key != identity_key:
            cur.execute("""INSERT INTO dedupe_source_tombstones
                (file_ref_id,source_question_key,identity_key,deleted_question_id,keeper_id,reason)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(file_ref_id,source_question_key,identity_key) DO UPDATE SET
                    keeper_id=EXCLUDED.keeper_id,reason=EXCLUDED.reason,created_at=now()
                """,(file_ref_id,source_key,content_key,duplicate_id,keeper_id,reason))
    for schema_name, table_name, column_name in child_fks:
        snapshot["child_rows"][f"{schema_name}.{table_name}.{column_name}"] = _json_rows(
            cur, schema_name, table_name, column_name, duplicate_id
        )

    handled = set()
    for schema_name, table_name, column_name in child_fks:
        if table_name == "question_images":
            _move_images(cur, keeper_id, duplicate_id)
            handled.add((schema_name, table_name, column_name))
        elif table_name == "question_subtopics":
            _move_subtopics(cur, keeper_id, duplicate_id)
            handled.add((schema_name, table_name, column_name))

    for schema_name, table_name, column_name in child_fks:
        if (schema_name, table_name, column_name) in handled:
            continue
        statement = sql.SQL("UPDATE {}.{} SET {} = %s WHERE {} = %s").format(
            sql.Identifier(schema_name), sql.Identifier(table_name),
            sql.Identifier(column_name), sql.Identifier(column_name)
        )
        cur.execute(statement, (keeper_id, duplicate_id))

    # Do not leave review rows pointing at a question that is about to vanish.
    # The table is optional for legacy databases and is created by review setup.
    cur.execute("SELECT to_regclass('dedupe_review_candidates')")
    if cur.fetchone()[0]:
        cur.execute("""UPDATE dedupe_review_candidates
            SET status='rejected',review_note='question merged',reviewed_at=now()
            WHERE status='pending' AND (q1_id=%s OR q2_id=%s)""",
            (duplicate_id,duplicate_id))

    sig = signature or "manual"
    duplicate = by_id[duplicate_id]
    cur.execute("""
        INSERT INTO dedup_deletions
            (signature, canonical_id, deleted_id, question_text, options,
             correct_index, correct_text, reason, snapshot)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (sig, keeper_id, duplicate_id, duplicate[2], Json(duplicate[3]),
          duplicate[4], duplicate[5], reason,
          Json(snapshot, dumps=lambda value: json.dumps(value, default=str))))

    cur.execute("UPDATE questions SET canonical_id = NULL WHERE canonical_id = %s",
                (duplicate_id,))
    cur.execute("DELETE FROM questions WHERE id = %s", (duplicate_id,))
    if cur.rowcount != 1:
        raise RuntimeError(f"question {duplicate_id} was not deleted")
    return duplicate[2]
