-- Admin review helpers for the hosted console (index.html).
--
-- The console talks to Neon over HTTPS with no long-lived session, so every
-- destructive review action must be a SINGLE statement. These functions put the
-- full safety logic (same chapter, verified and agreeing answers, child rows
-- preserved, audit snapshot) inside Postgres, which makes one click atomic.
--
-- They mirror dedupe/merge_questions.py and review/duplicate_reviews.py:
--   * abhyas_answer_key       -> dedupe/content_identity.answer_identity
--   * abhyas_merge_questions  -> dedupe/merge_questions.merge_question_rows
--   * abhyas_merge_exact_group-> review/dashboard.api_apply_exact_group
--   * abhyas_delete_question  -> review/dashboard.api_delete
--
-- Install or refresh them by running this whole file once. The console's
-- "Install / refresh helpers" button does exactly that.

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
);
ALTER TABLE dedup_deletions ADD COLUMN IF NOT EXISTS snapshot JSONB;
ALTER TABLE dedup_deletions ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ NOT NULL DEFAULT now();

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
);

CREATE TABLE IF NOT EXISTS review_deletions (
    id BIGSERIAL PRIMARY KEY,
    question_id BIGINT NOT NULL,
    snapshot JSONB NOT NULL,
    reason TEXT,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    restored_at TIMESTAMPTZ
);

-- Canonical answer identity. Must stay in step with
-- dedupe/content_identity.answer_identity: lowercase, drop punctuation and
-- whitespace, keep +-*/=<>^%~, and refuse inconsistent answer data.
-- One canonical answer string. A hyphen joining two letters is typography, so
-- "back-sight" equals "back sight"; a numeric hyphen is kept, because
-- "5-10%" is a range and must not collapse into "510%".
CREATE OR REPLACE FUNCTION abhyas_answer_norm(p_value text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT regexp_replace(
               regexp_replace(
                   regexp_replace(lower(coalesce(p_value, '')), '\s*-\s*', '-', 'g'),
                   '([a-z])-(?=[a-z])', '\1', 'g'),
               '[^a-z0-9=+*/<>^%~-]', '', 'g')
$fn$;

CREATE OR REPLACE FUNCTION abhyas_answer_key(
    p_correct_text text,
    p_correct_index integer,
    p_options jsonb
) RETURNS text
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE
        WHEN p_correct_index IS NOT NULL
             AND (p_options IS NULL
                  OR jsonb_typeof(p_options) <> 'array'
                  OR p_correct_index < 0
                  OR p_correct_index >= jsonb_array_length(p_options))
            THEN '__answer_conflict__'
        WHEN p_correct_index IS NOT NULL
             AND p_options IS NOT NULL
             AND jsonb_typeof(p_options) = 'array'
             AND nullif(btrim(coalesce(p_correct_text, '')), '') IS NOT NULL
             AND abhyas_answer_norm(p_correct_text)
                 <> abhyas_answer_norm(p_options ->> p_correct_index)
            THEN '__answer_conflict__'
        WHEN nullif(btrim(coalesce(p_correct_text, '')), '') IS NOT NULL
            THEN abhyas_answer_norm(p_correct_text)
        WHEN p_correct_index IS NOT NULL
             AND p_options IS NOT NULL
             AND jsonb_typeof(p_options) = 'array'
            THEN abhyas_answer_norm(p_options ->> p_correct_index)
        ELSE '__unanswered__'
    END
$fn$;

-- Merge one duplicate into a keeper. Raises instead of merging when the two
-- rows are not in the same chapter or do not share one verified answer.
CREATE OR REPLACE FUNCTION abhyas_merge_questions(
    p_keeper bigint,
    p_duplicate bigint,
    p_reason text DEFAULT 'review',
    p_signature text DEFAULT 'manual'
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_keeper_chapter bigint;
    v_duplicate_chapter bigint;
    v_keeper_answer text;
    v_duplicate_answer text;
    v_question jsonb;
    v_text text;
    v_options jsonb;
    v_index integer;
    v_correct_text text;
    v_snapshot jsonb;
    v_child record;
    v_child_rows jsonb;
    v_base integer;
    v_key text;
BEGIN
    IF p_keeper = p_duplicate THEN
        RAISE EXCEPTION 'keeper and duplicate must be different questions';
    END IF;

    PERFORM 1 FROM questions q WHERE q.id = p_keeper FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'keeper question % no longer exists', p_keeper;
    END IF;
    PERFORM 1 FROM questions q WHERE q.id = p_duplicate FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'duplicate question % no longer exists', p_duplicate;
    END IF;

    SELECT c.id INTO v_keeper_chapter
    FROM questions q
    JOIN files f ON f.id = q.file_ref_id
    JOIN books b ON b.id = f.book_id
    JOIN chapters c ON c.id = b.chapter_id
    WHERE q.id = p_keeper;

    SELECT c.id INTO v_duplicate_chapter
    FROM questions q
    JOIN files f ON f.id = q.file_ref_id
    JOIN books b ON b.id = f.book_id
    JOIN chapters c ON c.id = b.chapter_id
    WHERE q.id = p_duplicate;

    IF v_keeper_chapter IS NULL OR v_duplicate_chapter IS NULL THEN
        RAISE EXCEPTION 'both questions must belong to a chapter';
    END IF;
    IF v_keeper_chapter <> v_duplicate_chapter THEN
        RAISE EXCEPTION 'questions are not in the same chapter';
    END IF;

    SELECT abhyas_answer_key(q.correct_text, q.correct_index, q.options)
      INTO v_keeper_answer FROM questions q WHERE q.id = p_keeper;
    SELECT abhyas_answer_key(q.correct_text, q.correct_index, q.options)
      INTO v_duplicate_answer FROM questions q WHERE q.id = p_duplicate;

    IF left(v_keeper_answer, 2) = '__' THEN
        RAISE EXCEPTION 'keeper question has no valid, consistent answer';
    END IF;
    IF left(v_duplicate_answer, 2) = '__' THEN
        RAISE EXCEPTION 'duplicate question has no valid, consistent answer';
    END IF;
    IF v_keeper_answer <> v_duplicate_answer THEN
        RAISE EXCEPTION 'answers conflict; make both answers agree before merging';
    END IF;

    SELECT to_jsonb(q) INTO v_question FROM questions q WHERE q.id = p_duplicate;
    SELECT q.question_text, q.options, q.correct_index, q.correct_text
      INTO v_text, v_options, v_index, v_correct_text
      FROM questions q WHERE q.id = p_duplicate;

    v_snapshot := jsonb_build_object('question', v_question, 'child_rows', '{}'::jsonb);

    FOR v_child IN
        SELECT ns.nspname AS sch, rel.relname AS tbl, att.attname AS col
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        JOIN pg_attribute att
          ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
        WHERE con.contype = 'f'
          AND con.confrelid = 'questions'::regclass
          AND array_length(con.conkey, 1) = 1
        ORDER BY ns.nspname, rel.relname, att.attname
    LOOP
        v_key := v_child.sch || '.' || v_child.tbl || '.' || v_child.col;
        EXECUTE format(
            'SELECT coalesce(jsonb_agg(to_jsonb(child)), ''[]''::jsonb) FROM %I.%I child WHERE %I = $1',
            v_child.sch, v_child.tbl, v_child.col)
        INTO v_child_rows USING p_duplicate;
        v_snapshot := jsonb_set(v_snapshot, ARRAY['child_rows', v_key], v_child_rows);

        IF v_child.tbl = 'question_images' THEN
            SELECT coalesce(max(dst.position), -1) INTO v_base
              FROM question_images dst WHERE dst.question_id = p_keeper;
            INSERT INTO question_images (question_id, mime_type, data_base64, position)
            SELECT p_keeper, src.mime_type, src.data_base64,
                   v_base + row_number() OVER (ORDER BY src.position, src.id)
            FROM question_images src
            WHERE src.question_id = p_duplicate
              AND NOT EXISTS (
                  SELECT 1 FROM question_images dst
                  WHERE dst.question_id = p_keeper
                    AND dst.mime_type IS NOT DISTINCT FROM src.mime_type
                    AND dst.data_base64 IS NOT DISTINCT FROM src.data_base64);
            DELETE FROM question_images WHERE question_id = p_duplicate;
        ELSIF v_child.tbl = 'question_subtopics' THEN
            INSERT INTO question_subtopics
                (question_id, subtopic_id, confidence, matched_keywords, classified_by)
            SELECT p_keeper, src.subtopic_id, src.confidence, src.matched_keywords,
                   src.classified_by
            FROM question_subtopics src
            WHERE src.question_id = p_duplicate
              AND NOT EXISTS (
                  SELECT 1 FROM question_subtopics dst
                  WHERE dst.question_id = p_keeper
                    AND dst.subtopic_id = src.subtopic_id);
            DELETE FROM question_subtopics WHERE question_id = p_duplicate;
        ELSIF v_child.tbl = 'questions' AND v_child.col = 'canonical_id' THEN
            UPDATE questions SET canonical_id = NULL WHERE canonical_id = p_duplicate;
        ELSE
            EXECUTE format('UPDATE %I.%I SET %I = $1 WHERE %I = $2',
                           v_child.sch, v_child.tbl, v_child.col, v_child.col)
            USING p_keeper, p_duplicate;
        END IF;
    END LOOP;

    -- Remember the source row that was merged so a later Drive sync does not
    -- recreate it from the stale source JSON.
    IF (v_question ->> 'file_ref_id') IS NOT NULL
       AND coalesce(v_question ->> 'source_identity', v_question ->> 'identity_key', '') <> ''
    THEN
        INSERT INTO dedupe_source_tombstones
            (file_ref_id, source_question_key, identity_key,
             deleted_question_id, keeper_id, reason)
        VALUES (
            (v_question ->> 'file_ref_id')::bigint,
            coalesce(v_question ->> 'source_question_key', ''),
            coalesce(nullif(v_question ->> 'source_identity', ''), v_question ->> 'identity_key'),
            p_duplicate, p_keeper, p_reason)
        ON CONFLICT (file_ref_id, source_question_key, identity_key) DO UPDATE
            SET keeper_id = EXCLUDED.keeper_id,
                reason = EXCLUDED.reason,
                created_at = now();

        IF coalesce(v_question ->> 'identity_key', '') <> ''
           AND (v_question ->> 'identity_key') IS DISTINCT FROM (v_question ->> 'source_identity')
        THEN
            INSERT INTO dedupe_source_tombstones
                (file_ref_id, source_question_key, identity_key,
                 deleted_question_id, keeper_id, reason)
            VALUES (
                (v_question ->> 'file_ref_id')::bigint,
                coalesce(v_question ->> 'source_question_key', ''),
                v_question ->> 'identity_key',
                p_duplicate, p_keeper, p_reason)
            ON CONFLICT (file_ref_id, source_question_key, identity_key) DO UPDATE
                SET keeper_id = EXCLUDED.keeper_id,
                    reason = EXCLUDED.reason,
                    created_at = now();
        END IF;
    END IF;

    INSERT INTO dedup_deletions
        (signature, canonical_id, deleted_id, question_text, options,
         correct_index, correct_text, reason, snapshot)
    VALUES (coalesce(p_signature, 'manual'), p_keeper, p_duplicate, v_text, v_options,
            v_index, v_correct_text, coalesce(p_reason, 'manual'), v_snapshot);

    IF to_regclass('dedupe_review_candidates') IS NOT NULL THEN
        UPDATE dedupe_review_candidates
           SET status = 'rejected', review_note = 'question merged', reviewed_at = now()
         WHERE status = 'pending' AND (q1_id = p_duplicate OR q2_id = p_duplicate);
    END IF;

    DELETE FROM questions WHERE id = p_duplicate;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'question % was not deleted', p_duplicate;
    END IF;

    RETURN jsonb_build_object('ok', true, 'keeper_id', p_keeper,
                              'deleted_id', p_duplicate, 'reason', p_reason);
END
$fn$;

-- Merge every answer-agreeing copy in one chapter-scoped exact group.
-- Conflicting or unanswered copies are left alone for a human decision.
CREATE OR REPLACE FUNCTION abhyas_merge_exact_group(
    p_chapter_id bigint,
    p_identity_key text,
    p_reason text DEFAULT 'review_exact_group'
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_group_key record;
    v_keeper bigint;
    v_ids bigint[];
    v_id bigint;
    v_total integer;
    v_merged integer := 0;
BEGIN
    IF p_identity_key IS NULL OR p_identity_key = '' THEN
        RAISE EXCEPTION 'identity key is required';
    END IF;

    SELECT count(*)::int INTO v_total
    FROM questions q
    JOIN files f ON f.id = q.file_ref_id
    JOIN books b ON b.id = f.book_id
    JOIN chapters c ON c.id = b.chapter_id
    WHERE c.id = p_chapter_id
      AND q.identity_key = p_identity_key
      AND q.canonical_id IS NULL;

    IF v_total < 2 THEN
        RAISE EXCEPTION 'this exact duplicate group no longer has multiple live questions';
    END IF;

    FOR v_group_key IN
        SELECT abhyas_answer_key(q.correct_text, q.correct_index, q.options) AS answer_key
        FROM questions q
        JOIN files f ON f.id = q.file_ref_id
        JOIN books b ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id
        WHERE c.id = p_chapter_id
          AND q.identity_key = p_identity_key
          AND q.canonical_id IS NULL
        GROUP BY 1
        HAVING count(*) > 1
    LOOP
        CONTINUE WHEN left(v_group_key.answer_key, 2) = '__';

        SELECT q.id INTO v_keeper
        FROM questions q
        JOIN files f ON f.id = q.file_ref_id
        JOIN books b ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id
        WHERE c.id = p_chapter_id
          AND q.identity_key = p_identity_key
          AND q.canonical_id IS NULL
          AND abhyas_answer_key(q.correct_text, q.correct_index, q.options)
              = v_group_key.answer_key
        ORDER BY (q.corrected_at IS NULL), q.corrected_at DESC NULLS LAST,
                 q.quality_score DESC NULLS LAST, q.id
        LIMIT 1;

        SELECT array_agg(q.id ORDER BY q.id) INTO v_ids
        FROM questions q
        JOIN files f ON f.id = q.file_ref_id
        JOIN books b ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id
        WHERE c.id = p_chapter_id
          AND q.identity_key = p_identity_key
          AND q.canonical_id IS NULL
          AND q.id <> v_keeper
          AND abhyas_answer_key(q.correct_text, q.correct_index, q.options)
              = v_group_key.answer_key;

        FOREACH v_id IN ARRAY coalesce(v_ids, '{}'::bigint[]) LOOP
            PERFORM abhyas_merge_questions(v_keeper, v_id, p_reason, p_identity_key);
            v_merged := v_merged + 1;
        END LOOP;
    END LOOP;

    RETURN jsonb_build_object('ok', true, 'merged', v_merged,
                              'retained', v_total - v_merged,
                              'reason', 'only copies with an exact identity and a matching verified answer merge');
END
$fn$;

-- Delete one question with an audit snapshot, its child rows, and any pending
-- review pair that pointed at it.
CREATE OR REPLACE FUNCTION abhyas_delete_question(
    p_id bigint,
    p_reason text DEFAULT 'console delete'
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_question jsonb;
    v_snapshot jsonb;
    v_child record;
    v_child_rows jsonb;
    v_key text;
    v_audit_id bigint;
BEGIN
    PERFORM 1 FROM questions q WHERE q.id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'question % no longer exists', p_id;
    END IF;

    SELECT to_jsonb(q) INTO v_question FROM questions q WHERE q.id = p_id;
    v_snapshot := jsonb_build_object('question', v_question, 'child_rows', '{}'::jsonb);

    FOR v_child IN
        SELECT ns.nspname AS sch, rel.relname AS tbl, att.attname AS col
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        JOIN pg_attribute att
          ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
        WHERE con.contype = 'f'
          AND con.confrelid = 'questions'::regclass
          AND array_length(con.conkey, 1) = 1
        ORDER BY ns.nspname, rel.relname, att.attname
    LOOP
        v_key := v_child.sch || '.' || v_child.tbl || '.' || v_child.col;
        EXECUTE format(
            'SELECT coalesce(jsonb_agg(to_jsonb(child)), ''[]''::jsonb) FROM %I.%I child WHERE %I = $1',
            v_child.sch, v_child.tbl, v_child.col)
        INTO v_child_rows USING p_id;
        v_snapshot := jsonb_set(v_snapshot, ARRAY['child_rows', v_key], v_child_rows);
    END LOOP;

    INSERT INTO review_deletions (question_id, snapshot, reason)
    VALUES (p_id, v_snapshot, coalesce(p_reason, 'console delete'))
    RETURNING id INTO v_audit_id;

    IF to_regclass('dedupe_review_candidates') IS NOT NULL THEN
        DELETE FROM dedupe_review_candidates WHERE q1_id = p_id OR q2_id = p_id;
    END IF;

    FOR v_child IN
        SELECT ns.nspname AS sch, rel.relname AS tbl, att.attname AS col
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        JOIN pg_attribute att
          ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
        WHERE con.contype = 'f'
          AND con.confrelid = 'questions'::regclass
          AND array_length(con.conkey, 1) = 1
        ORDER BY ns.nspname, rel.relname, att.attname
    LOOP
        IF v_child.tbl = 'questions' AND v_child.col = 'canonical_id' THEN
            EXECUTE format('UPDATE %I.%I SET %I = NULL WHERE %I = $1',
                           v_child.sch, v_child.tbl, v_child.col, v_child.col)
            USING p_id;
        ELSE
            EXECUTE format('DELETE FROM %I.%I WHERE %I = $1',
                           v_child.sch, v_child.tbl, v_child.col)
            USING p_id;
        END IF;
    END LOOP;

    DELETE FROM questions WHERE id = p_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'question % was not deleted', p_id;
    END IF;

    RETURN jsonb_build_object('ok', true, 'id', p_id, 'audit_id', v_audit_id);
END
$fn$;
