-- ============================================================================
-- near_duplicates.sql  --  find the questions that are "the same question"
-- ============================================================================
-- dedupe/ only merges questions whose text AND options are exactly equal
-- after normalisation.  These queries use trigram similarity (pg_trgm) to
-- also catch the ones that differ by a space, a singular/plural, a stray
-- "*" or a slightly reworded option.
--
-- Run it as ONE script (the SET statements must run in the same session as
-- the queries that follow):
--
--     psql "$DATABASE_URL" -f tools/near_duplicates.sql
--
-- or paste section by section into the Neon SQL editor.
--
-- Cost on the current DB (13,106 questions): ~17 s for the pair scan,
-- thanks to the trigram index in section 0.
-- ============================================================================


-- 0) one-time setup --------------------------------------------------------
-- pg_trgm and the index are already present on this database.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_q_text_trgm
  ON questions USING gin (question_text gin_trgm_ops);


-- 1) quick count, bucketed by text similarity --------------------------------
-- 0.90 is a good "probably the same question" line; 0.99 is "literally the
-- same words, only punctuation/spacing moved".

SET pg_trgm.similarity_threshold = 0.90;

SELECT round(similarity(a.question_text, b.question_text)::numeric, 2) AS text_similarity,
       COUNT(*)                                                AS pairs
FROM questions a
JOIN questions b ON b.id > a.id
WHERE a.question_text % b.question_text
GROUP BY 1
ORDER BY 1 DESC;


-- 2) THE LIST -- same question AND same options ------------------------------
--
--   q_sim    similarity of the question text                      0..1
--   opt_sim  similarity of the option lists, punctuation stripped 0..1
--   answer   'same'     both copies point at the same correct option
--            'CONFLICT' they disagree -- do NOT auto-merge these
--            'none'     at least one copy has no answer
--
-- Change both thresholds to 0.99 if you only want near-identical rows.

SET pg_trgm.similarity_threshold = 0.90;

SELECT
    a.id            AS id_a,
    b.id            AS id_b,
    la.level_key    AS level_a,
    lb.level_key    AS level_b,
    ca.chapter_key  AS chapter_a,
    cb.chapter_key  AS chapter_b,
    (ca.id = cb.id) AS same_chapter,
    round(similarity(a.question_text, b.question_text)::numeric, 3) AS q_sim,
    round(similarity(aopt.t, bopt.t)::numeric, 3)                   AS opt_sim,
    CASE
      WHEN coalesce(a.correct_text, '') = '' OR coalesce(b.correct_text, '') = ''
        THEN 'none'
      WHEN lower(regexp_replace(a.correct_text, '[^[:alnum:]]+', ' ', 'g'))
           = lower(regexp_replace(b.correct_text, '[^[:alnum:]]+', ' ', 'g'))
        THEN 'same'
      ELSE 'CONFLICT'
    END AS answer,
    a.question_text AS question_a,
    b.question_text AS question_b,
    a.options       AS options_a,
    b.options       AS options_b
FROM questions a
JOIN questions b ON b.id > a.id
JOIN files     fa ON fa.id = a.file_ref_id
JOIN books     ba ON ba.id = fa.book_id
JOIN chapters  ca ON ca.id = ba.chapter_id
JOIN levels    la ON la.id = ca.level_id
JOIN files     fb ON fb.id = b.file_ref_id
JOIN books     bb ON bb.id = fb.book_id
JOIN chapters  cb ON cb.id = bb.chapter_id
JOIN levels    lb ON lb.id = cb.level_id
CROSS JOIN LATERAL (
    SELECT lower(regexp_replace(coalesce(a.options::text, ''),
                                '[^[:alnum:]]+', ' ', 'g'))
) aopt(t)
CROSS JOIN LATERAL (
    SELECT lower(regexp_replace(coalesce(b.options::text, ''),
                                '[^[:alnum:]]+', ' ', 'g'))
) bopt(t)
WHERE a.question_text % b.question_text
  AND similarity(a.question_text, b.question_text) >= 0.90
  AND similarity(aopt.t, bopt.t)                   >= 0.90
ORDER BY opt_sim DESC, q_sim DESC, a.id;


-- 3) one-line summary at the usual thresholds --------------------------------
-- If the 99/99 column is what you care about, use 0.99 in section 2.

SET pg_trgm.similarity_threshold = 0.90;

SELECT
    COUNT(*) FILTER (WHERE q >= 0.90 AND o >= 0.90) AS pairs_90_90,
    COUNT(*) FILTER (WHERE q >= 0.95 AND o >= 0.95) AS pairs_95_95,
    COUNT(*) FILTER (WHERE q >= 0.99 AND o >= 0.99) AS pairs_99_99,
    COUNT(*) FILTER (WHERE q >= 0.99 AND o >= 0.90
                       AND same_chapter)            AS same_chapter_99
FROM (
    SELECT similarity(a.question_text, b.question_text) AS q,
           similarity(aopt.t, bopt.t)                   AS o,
           (ca.id = cb.id)                              AS same_chapter
    FROM questions a
    JOIN questions b ON b.id > a.id
    JOIN files     fa ON fa.id = a.file_ref_id
    JOIN books     ba ON ba.id = fa.book_id
    JOIN chapters  ca ON ca.id = ba.chapter_id
    JOIN files     fb ON fb.id = b.file_ref_id
    JOIN books     bb ON bb.id = fb.book_id
    JOIN chapters  cb ON cb.id = bb.chapter_id
    CROSS JOIN LATERAL (
        SELECT lower(regexp_replace(coalesce(a.options::text, ''),
                                    '[^[:alnum:]]+', ' ', 'g'))
    ) aopt(t)
    CROSS JOIN LATERAL (
        SELECT lower(regexp_replace(coalesce(b.options::text, ''),
                                    '[^[:alnum:]]+', ' ', 'g'))
    ) bopt(t)
    WHERE a.question_text % b.question_text
) s;

-- same_chapter_99 is the exact scope dedupe_exact v8 uses (one chapter).
-- Add "AND ca.id = cb.id" to section 2 to see those rows in full.


-- 4) the "why does the app keep showing me this" view ------------------------
-- The same question reachable from two different files inside one chapter --
-- i.e. what a user hits while practising.

SET pg_trgm.similarity_threshold = 0.90;

SELECT
    ca.chapter_key,
    la.level_key,
    COUNT(*) AS duplicate_pairs
FROM questions a
JOIN questions b ON b.id > a.id
JOIN files     fa ON fa.id = a.file_ref_id
JOIN books     ba ON ba.id = fa.book_id
JOIN chapters  ca ON ca.id = ba.chapter_id
JOIN levels    la ON la.id = ca.level_id
JOIN files     fb ON fb.id = b.file_ref_id
JOIN books     bb ON bb.id = fb.book_id
JOIN chapters  cb ON cb.id = bb.chapter_id
CROSS JOIN LATERAL (
    SELECT lower(regexp_replace(coalesce(a.options::text, ''),
                                '[^[:alnum:]]+', ' ', 'g'))
) aopt(t)
CROSS JOIN LATERAL (
    SELECT lower(regexp_replace(coalesce(b.options::text, ''),
                                '[^[:alnum:]]+', ' ', 'g'))
) bopt(t)
WHERE a.question_text % b.question_text
  AND similarity(a.question_text, b.question_text) >= 0.90
  AND similarity(aopt.t, bopt.t)                   >= 0.90
  AND ca.id = cb.id                                -- same chapter only
  AND fa.id <> fb.id                               -- from two different files
GROUP BY 1, 2
ORDER BY duplicate_pairs DESC, 1;
