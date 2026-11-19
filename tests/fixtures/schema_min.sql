
CREATE TABLE chapters (id bigserial PRIMARY KEY, chapter_name text, level_id bigint);
CREATE TABLE books (id bigserial PRIMARY KEY, chapter_id bigint REFERENCES chapters(id), book_name text);
CREATE TABLE files (id bigserial PRIMARY KEY, book_id bigint REFERENCES books(id), subtopic text, file_id text);
CREATE TABLE subtopics (id bigserial PRIMARY KEY, code text, title text);
CREATE TABLE questions (
    id bigserial PRIMARY KEY,
    question_number integer,
    question_text text,
    options jsonb,
    correct_index integer,
    correct_text text,
    explanation text,
    quality_score real DEFAULT 0,
    identity_key text,
    source_identity text,
    source_question_key text,
    identity_version smallint,
    canonical_id bigint,
    corrected_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    file_ref_id bigint REFERENCES files(id),
    stable_qn_id text,
    raw jsonb
);
CREATE TABLE question_subtopics (
    id bigserial PRIMARY KEY,
    question_id bigint REFERENCES questions(id) ON DELETE CASCADE,
    subtopic_id bigint REFERENCES subtopics(id),
    confidence real,
    matched_keywords jsonb,
    classified_by text
);
CREATE TABLE question_images (
    id bigserial PRIMARY KEY,
    question_id bigint REFERENCES questions(id) ON DELETE CASCADE,
    mime_type text,
    data_base64 text,
    position integer
);
CREATE TABLE dedupe_review_candidates (
    id bigserial PRIMARY KEY,
    chapter_id integer NOT NULL,
    q1_id bigint NOT NULL,
    q2_id bigint NOT NULL,
    stem_score real NOT NULL,
    options_score real NOT NULL,
    q1_identity_key text,
    q2_identity_key text,
    status text NOT NULL DEFAULT 'pending',
    review_note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz,
    UNIQUE (q1_id, q2_id),
    CHECK (q1_id < q2_id),
    CHECK (status IN ('pending', 'merged', 'rejected'))
);
CREATE TABLE levels (id bigserial PRIMARY KEY, level_key text);
CREATE TABLE applied_corrections (
    uid text NOT NULL, corrected_at timestamptz NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(), matched_by text,
    question_id bigint, file_id text, before_identity text, after_identity text,
    notes text, PRIMARY KEY (uid, corrected_at)
);
CREATE TABLE correction_source_aliases_v2 (
    alias_id bigserial PRIMARY KEY,
    chapter_id integer NOT NULL,
    source_identity text NOT NULL,
    source_location text NOT NULL,
    source_number text NOT NULL DEFAULT '',
    question_text text NOT NULL,
    options jsonb NOT NULL,
    correct_index integer,
    correct_text text,
    explanation text,
    identity_key text NOT NULL,
    quality_score real NOT NULL DEFAULT 0,
    corrected_at timestamptz NOT NULL DEFAULT now()
);
