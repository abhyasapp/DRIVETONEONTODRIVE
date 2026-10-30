/* Runs review/admin_functions.sql against a real Postgres (PGlite, WASM) and
 * exercises the merge/delete paths the hosted console uses.
 *
 *   npm install --no-save @electric-sql/pglite      (once, at the repo root)
 *   node tests/test_admin_sql.mjs
 *
 * PGLITE_MODULE can point at a different install, which keeps the repo free of
 * a permanent Node dependency:
 *   PGLITE_MODULE=.tmp/pglite/node_modules/@electric-sql/pglite/dist/index.cjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');

async function loadPglite() {
  const explicit = process.env.PGLITE_MODULE;
  const candidates = explicit
    ? [explicit.startsWith('.') ? path.resolve(root, explicit) : explicit]
    : ['@electric-sql/pglite', path.join(root, 'node_modules', '@electric-sql', 'pglite')];
  let lastError;
  for (const candidate of candidates) {
    try {
      const specifier = candidate.startsWith('/') || /^[A-Za-z]:/.test(candidate)
        ? pathToFileURL(candidate).href
        : candidate;
      return await import(specifier);
    } catch (error) { lastError = error; }
  }
  throw new Error(`PGlite is not installed (${lastError && lastError.message}). `
    + 'Run: npm install --no-save @electric-sql/pglite');
}

vm.runInThisContext(readFileSync(path.join(root, 'review', 'sqlsplit.js'), 'utf8'));
vm.runInThisContext(readFileSync(path.join(root, 'review', 'identity.js'), 'utf8'));
const Identity = globalThis.AbhyasIdentity;

const { PGlite } = await loadPglite();
const db = new PGlite();

const failures = [];
let checks = 0;
function check(label, condition, detail) {
  checks += 1;
  if (!condition) failures.push(`${label}${detail === undefined ? '' : `\n  got: ${JSON.stringify(detail)}`}`);
}
const rows = async (sql, params = []) => (await db.query(sql, params)).rows;

const SCHEMA = `
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
CREATE TABLE applied_corrections (uid text, corrected_at timestamptz);
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
`;

for (const statement of globalThis.AbhyasSql.split(SCHEMA)) {
  await db.exec(statement);
}

const ddl = readFileSync(path.join(root, 'review', 'admin_functions.sql'), 'utf8');
const statements = globalThis.AbhyasSql.split(ddl);
for (const statement of statements) await db.exec(statement);
check('admin_functions.sql splits into executable statements', statements.length >= 4,
  statements.length);

await db.exec(`
INSERT INTO chapters (id, chapter_name) VALUES (1, 'Surveying'), (2, 'Hydraulics');
INSERT INTO books (id, chapter_id, book_name) VALUES (1, 1, 'Sunil Sah'), (2, 2, 'RK Shrestha');
INSERT INTO files (id, book_id, subtopic, file_id) VALUES
    (1, 1, 'Chain survey', 'drive-1'), (2, 2, 'Flow', 'drive-2');
INSERT INTO subtopics (id, code, title) VALUES (1, 'cs1', 'Chain survey'), (2, 'h1', 'Flow');
`);

const NEWTON = JSON.stringify(['Newton', 'Joule', 'Watt', 'Pascal']);
async function insertQuestion({ chapter = 1, fileRef = 1, identity = 'aaa', sourceIdentity = 'bbb',
  text = 'What is the unit of force?', options = NEWTON, index = 0, correctText = 'Newton',
  quality = 10, corrected = null, number = 1, sourceKey = 'n:1' }) {
  const result = await rows(`
    INSERT INTO questions (question_number, question_text, options, correct_index, correct_text,
        explanation, quality_score, identity_key, source_identity, source_question_key,
        identity_version, corrected_at, file_ref_id, raw)
    VALUES ($1,$2,$3::jsonb,$4,$5,'because', $6,$7,$8,$9,2,$10,$11,$12::jsonb)
    RETURNING id`,
  [number, text, options, index, correctText, quality, identity, sourceIdentity, sourceKey,
    corrected, fileRef, JSON.stringify({ q: text, options })]);
  return result[0].id;
}

const keeper = await insertQuestion({ quality: 30, corrected: '2026-01-01T00:00:00Z', number: 1 });
const twin = await insertQuestion({ quality: 33, number: 2 });
const lowerQ = await insertQuestion({ quality: 5, number: 3, sourceKey: 'n:3' });
const blankA = await insertQuestion({ index: null, correctText: null, quality: 0, number: 4 });
const blankB = await insertQuestion({ index: null, correctText: null, quality: 0, number: 5 });
const conflict = await insertQuestion({ index: 1, correctText: 'Joule', quality: 20, number: 6 });

await db.exec(`
INSERT INTO question_subtopics (question_id, subtopic_id, confidence, matched_keywords, classified_by)
VALUES (${keeper}, 1, 0.9, '[]'::jsonb, 'keyword'),
       (${twin}, 2, 0.4, '[]'::jsonb, 'gemini'),
       (${twin}, 1, 0.3, '[]'::jsonb, 'gemini');
INSERT INTO question_images (question_id, mime_type, data_base64, position)
VALUES (${keeper}, 'image/png', 'AAAA', 0), (${twin}, 'image/png', 'BBBB', 0);
INSERT INTO dedupe_review_candidates (chapter_id, q1_id, q2_id, stem_score, options_score)
VALUES (1, ${keeper}, ${twin}, 99, 99);
`);

// -- answer key matches the Python/JS answer identity --------------------------
const SIGHT = JSON.stringify(['foresight', 'intermediate sight', 'back-sight', 'any sight']);
const RANGE = JSON.stringify(['2-3%', '5-10%', '10-15%', 'none']);

const answerKeySql = async (text, index, options) => {
  const value = await rows('SELECT abhyas_answer_key($1,$2,$3::jsonb) AS k', [text, index, options]);
  return value[0].k;
};
const compare = async (vector) => {
  const sql = await answerKeySql(vector.text, vector.index, JSON.stringify(JSON.parse(vector.options)));
  const js = Identity.answerKey(vector.text, vector.index, JSON.parse(vector.options));
  check(`abhyas_answer_key parity for ${JSON.stringify(vector)}`, sql === js, { sql, js });
};
for (const vector of [
  { text: 'Newton', index: 0, options: NEWTON },
  { text: null, index: 1, options: NEWTON },
  { text: 'Joule', index: 0, options: NEWTON },
  { text: null, index: null, options: NEWTON },
  { text: null, index: 5, options: NEWTON },
  { text: '  newton ', index: null, options: NEWTON },
  { text: null, index: null, options: '[]' },
  // typographic hyphen joined vs spaced vs absent must collapse to one key;
  // a numeric range must keep its hyphen
  { text: 'back-sight', index: null, options: SIGHT },
  { text: 'back - sight', index: null, options: SIGHT },
  { text: null, index: 2, options: SIGHT },
  { text: 'back sight', index: 2, options: SIGHT },
  { text: '5-10%', index: null, options: RANGE },
  { text: '5 - 10%', index: null, options: RANGE },
  { text: '1-2 m', index: null, options: RANGE },
]) {
  await compare(vector);
}
check('hyphenated and spaced answer agree',
  await answerKeySql('back-sight', null, SIGHT) === await answerKeySql('back sight', null, SIGHT));
check('numeric range keeps its hyphen',
  (await answerKeySql('5-10%', null, RANGE)) !== (await answerKeySql('510%', null, RANGE)));

// -- group merge --------------------------------------------------------------
const group = await rows('SELECT abhyas_merge_exact_group(1, $1) AS result', ['aaa']);
const groupResult = group[0].result;
check('group merge keeps one copy per verified answer', groupResult.merged === 2, groupResult);
check('group merge leaves unanswered and conflicting copies for review',
  groupResult.retained === 4, groupResult);

const survivors = await rows('SELECT id, correct_text, correct_index FROM questions WHERE identity_key=$1 ORDER BY id', ['aaa']);
check('keeper is the corrected, highest-quality copy',
  survivors[0].id === keeper, { survivors: survivors.map(row => row.id), keeper });
check('answer-conflicting copy survived', survivors.some(row => row.id === conflict));

const movedTopics = await rows('SELECT subtopic_id FROM question_subtopics WHERE question_id=$1 ORDER BY subtopic_id', [keeper]);
check('subtopic links moved to the keeper and de-duplicated',
  movedTopics.map(row => Number(row.subtopic_id)).join(',') === '1,2', movedTopics);

const movedImages = await rows('SELECT data_base64, position FROM question_images WHERE question_id=$1 ORDER BY position', [keeper]);
check('images moved to the keeper with new positions',
  movedImages.length === 2 && movedImages[0].data_base64 === 'AAAA'
  && movedImages[1].data_base64 === 'BBBB', movedImages);

const audits = await rows(`SELECT canonical_id, deleted_id, reason, snapshot FROM dedup_deletions
  WHERE signature=$1 ORDER BY id`, ['aaa']);
check('every merge writes an audit row', audits.length === 2, audits.length);
check('audit row keeps the deleted question snapshot',
  audits.every(row => row.snapshot && row.snapshot.question && row.snapshot.question.id), audits[0]);
check('audit records the keeper', audits.every(row => Number(row.canonical_id) === keeper), audits[0].canonical_id);

const tombstones = await rows(`SELECT identity_key, keeper_id FROM dedupe_source_tombstones
  WHERE deleted_question_id=$1 ORDER BY identity_key`, [twin]);
check('source tombstones cover both the source and content identity',
  tombstones.map(row => row.identity_key).join(',') === 'aaa,bbb'
  && tombstones.every(row => Number(row.keeper_id) === keeper), tombstones);

const staleCandidate = await rows('SELECT status, review_note FROM dedupe_review_candidates WHERE q2_id=$1', [twin]);
check('pending review pair closed when its question is merged',
  staleCandidate.length === 0 || staleCandidate[0].status === 'rejected', staleCandidate);

// -- guards -------------------------------------------------------------------
async function expectError(label, promise, fragment) {
  try {
    await promise;
    check(label, false, 'no error raised');
  } catch (error) {
    check(label, String(error.message).includes(fragment), error.message);
  }
}

const otherChapter = await insertQuestion({ fileRef: 2, number: 7 });
await expectError('refuses a cross-chapter merge',
  db.query('SELECT abhyas_merge_questions($1,$2)', [keeper, otherChapter]), 'same chapter');
await expectError('refuses to merge an unanswered copy',
  db.query('SELECT abhyas_merge_questions($1,$2)', [keeper, blankA]), 'valid, consistent answer');
await expectError('refuses to merge conflicting answers',
  db.query('SELECT abhyas_merge_questions($1,$2)', [keeper, conflict]), 'answers conflict');
await expectError('refuses a missing question',
  db.query('SELECT abhyas_merge_questions($1,$2)', [keeper, 999999]), 'no longer exists');
await expectError('refuses an empty identity group merge',
  db.query('SELECT abhyas_merge_exact_group(2, $1)', ['nope']), 'no longer has multiple');

// -- delete -------------------------------------------------------------------
const deleted = await rows('SELECT abhyas_delete_question($1, $2) AS result', [blankB, 'test delete']);
check('delete reports the audit id', deleted[0].result.ok && deleted[0].result.audit_id, deleted[0].result);
const gone = await rows('SELECT count(*)::int AS n FROM questions WHERE id=$1', [blankB]);
check('delete removes the row', gone[0].n === 0, gone[0]);
const deletionAudit = await rows('SELECT question_id, snapshot, reason FROM review_deletions WHERE question_id=$1', [blankB]);
check('delete keeps a restorable snapshot',
  deletionAudit.length === 1 && deletionAudit[0].snapshot.question.id === blankB, deletionAudit);

const withChildren = await insertQuestion({ number: 9 });
await db.exec(`INSERT INTO question_subtopics (question_id, subtopic_id, confidence) VALUES (${withChildren}, 1, 0.5);
  INSERT INTO question_images (question_id, mime_type, data_base64, position) VALUES (${withChildren}, 'image/png', 'CCCC', 0);`);
await db.query('SELECT abhyas_delete_question($1)', [withChildren]);
const orphanTopics = await rows('SELECT count(*)::int AS n FROM question_subtopics WHERE question_id=$1', [withChildren]);
const orphanImages = await rows('SELECT count(*)::int AS n FROM question_images WHERE question_id=$1', [withChildren]);
check('delete removes child rows', orphanTopics[0].n === 0 && orphanImages[0].n === 0,
  { topics: orphanTopics[0].n, images: orphanImages[0].n });
await expectError('delete refuses a missing question',
  db.query('SELECT abhyas_delete_question($1)', [blankB]), 'no longer exists');

// -----------------------------------------------------------------------------
// The queries index.html runs. They are copied verbatim from the page so a
// renamed column or a broken join fails here instead of in the browser.
// -----------------------------------------------------------------------------
await db.exec(`
INSERT INTO levels (id, level_key) VALUES (1, 'level5'), (2, 'level7');
UPDATE chapters SET level_id = 1 WHERE id = 1;
UPDATE chapters SET level_id = 2 WHERE id = 2;
INSERT INTO questions (question_number, question_text, options, correct_index, correct_text,
    explanation, quality_score, identity_key, source_identity, source_question_key,
    identity_version, file_ref_id)
VALUES (21, 'Console smoke question?', '["Newton","Joule","Watt","Pascal"]'::jsonb, 0, 'Newton',
        'because', 30, 'ccc', 'ddd', 'n:21', 2, 1),
       (22, 'Console smoke question?', '["Newton","Joule","Watt","Pascal"]'::jsonb, 0, 'Newton',
        '', 0, 'ccc', 'ddd', 'n:22', 2, 1),
       (23, 'Second console question', '["A"]'::jsonb, NULL, NULL, '', 0, 'eee', NULL, 'n:23', 2, 1);
INSERT INTO question_subtopics (question_id, subtopic_id, confidence, classified_by)
SELECT id, 1, 0.8, 'keyword' FROM questions WHERE identity_key IN ('ccc', 'eee');
`);

const OPTION_COUNT = "(CASE WHEN jsonb_typeof(options) = 'array' THEN jsonb_array_length(options) ELSE 0 END)";
const UNANSWERED = "correct_index IS NULL AND coalesce(btrim(correct_text), '') = ''";

const consoleQueries = [
  ['overview stats', `
    SELECT count(*)::int AS questions,
           count(DISTINCT identity_key)::int AS identities,
           count(*) FILTER (WHERE ${UNANSWERED})::int AS unanswered,
           count(*) FILTER (WHERE correct_index IS NOT NULL AND ${OPTION_COUNT} > 0
                 AND (correct_index < 0 OR correct_index >= ${OPTION_COUNT}))::int AS out_of_range,
           count(*) FILTER (WHERE ${OPTION_COUNT} = 0)::int AS empty_options,
           count(*) FILTER (WHERE coalesce(btrim(explanation), '') = '')::int AS no_explanation,
           count(*) FILTER (WHERE corrected_at IS NOT NULL)::int AS corrected
    FROM questions WHERE canonical_id IS NULL`, []],
  ['overview counts', `
    SELECT (SELECT count(*) FROM chapters)::int AS chapters,
           (SELECT count(*) FROM subtopics)::int AS subtopics,
           (SELECT count(*) FROM files)::int AS files,
           (SELECT count(*) FROM question_images)::int AS images,
           (SELECT count(*) FROM applied_corrections)::int AS corrections,
           (SELECT count(*) FROM dedup_deletions)::int AS merges`, []],
  ['level breakdown', `
    SELECT l.level_key AS name, count(DISTINCT q.id)::int AS total
    FROM levels l
    JOIN chapters c ON c.level_id = l.id
    JOIN books b ON b.chapter_id = c.id
    JOIN files f ON b.id = f.book_id
    JOIN questions q ON q.file_ref_id = f.id
    WHERE q.canonical_id IS NULL
    GROUP BY 1 ORDER BY 1`, []],
  ['classification breakdown', `
    SELECT coalesce(classified_by, '(none)') AS source, count(*)::int AS total
    FROM question_subtopics GROUP BY 1 ORDER BY 2 DESC LIMIT 8`, []],
  ['merge reasons', `
    SELECT reason, count(*)::int AS total FROM dedup_deletions GROUP BY 1 ORDER BY 2 DESC LIMIT 8`, []],
  ['duplicate group count', `
    SELECT count(*)::int AS n FROM (
      SELECT c.id, q.identity_key FROM questions q
      JOIN files f ON f.id = q.file_ref_id
      JOIN books b ON b.id = f.book_id
      JOIN chapters c ON c.id = b.chapter_id
      WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
      GROUP BY 1, 2 HAVING count(*) > 1) grouped`, []],
  ['duplicate group list', `
    SELECT q.identity_key, c.id AS chapter_id, c.chapter_name,
           count(*)::int AS copies, min(q.id) AS sample_id, min(q.question_text) AS sample
    FROM questions q
    JOIN files f ON f.id = q.file_ref_id
    JOIN books b ON b.id = f.book_id
    JOIN chapters c ON c.id = b.chapter_id
    WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
    GROUP BY 1, 2, 3 HAVING count(*) > 1
    ORDER BY copies DESC, sample_id
    LIMIT 50 OFFSET $1`, [0]],
  ['duplicate group detail', `
    SELECT q.id, q.question_number, q.question_text, q.options, q.correct_index,
           q.correct_text, q.explanation, q.quality_score::float AS quality_score,
           q.corrected_at, b.book_name, f.subtopic
    FROM questions q
    LEFT JOIN files f ON f.id = q.file_ref_id
    LEFT JOIN books b ON b.id = f.book_id
    LEFT JOIN chapters c ON c.id = b.chapter_id
    WHERE q.identity_key = $1 AND c.id = $2 AND q.canonical_id IS NULL
    ORDER BY q.id`, ['ccc', 1]],
  ['near-duplicate queue', `
    SELECT d.id, d.chapter_id, d.q1_id, d.q2_id,
           d.stem_score::float AS stem_score, d.options_score::float AS options_score,
           d.status, c.chapter_name,
           q1.question_text AS q1_text, q1.options AS q1_options,
           q1.correct_index AS q1_correct_index, q1.correct_text AS q1_correct_text,
           q1.explanation AS q1_explanation,
           q2.question_text AS q2_text, q2.options AS q2_options,
           q2.correct_index AS q2_correct_index, q2.correct_text AS q2_correct_text,
           q2.explanation AS q2_explanation
    FROM dedupe_review_candidates d
    JOIN questions q1 ON q1.id = d.q1_id
    JOIN questions q2 ON q2.id = d.q2_id
    LEFT JOIN chapters c ON c.id = d.chapter_id
    WHERE d.status = $1
    ORDER BY d.stem_score DESC, d.options_score DESC, d.id
    LIMIT 20 OFFSET $2`, ['pending', 0]],
  ['near-duplicate count', `SELECT count(*)::int AS n FROM dedupe_review_candidates WHERE status = $1`, ['pending']],
  ['question search count', `SELECT count(*)::int AS n FROM questions q
      LEFT JOIN files f ON f.id = q.file_ref_id
      LEFT JOIN books b ON b.id = f.book_id
      LEFT JOIN chapters c ON c.id = b.chapter_id
      LEFT JOIN levels l ON l.id = c.level_id WHERE q.canonical_id IS NULL AND q.question_text ILIKE $1
        AND l.level_key = $2 AND q.correct_index IS NULL AND coalesce(btrim(q.correct_text), '') = ''`, ['%sms%', 'level5']],
  ['question search page', `
    SELECT q.id, q.question_number, q.question_text, q.options, q.correct_index,
           q.correct_text, q.explanation, q.quality_score::float AS quality_score,
           q.corrected_at, b.book_name, c.chapter_name, l.level_key, f.subtopic,
           (SELECT count(*) FROM questions x
            JOIN files xf ON xf.id = x.file_ref_id
            JOIN books xb ON xb.id = xf.book_id
            JOIN chapters xc ON xc.id = xb.chapter_id
            WHERE x.identity_key = q.identity_key AND xc.id = c.id
              AND x.canonical_id IS NULL)::int AS copies
    FROM questions q
      LEFT JOIN files f ON f.id = q.file_ref_id
      LEFT JOIN books b ON b.id = f.book_id
      LEFT JOIN chapters c ON c.id = b.chapter_id
      LEFT JOIN levels l ON l.id = c.level_id WHERE q.canonical_id IS NULL AND q.question_text ILIKE $1
        AND l.level_key = $2
    ORDER BY q.id DESC
    LIMIT 25 OFFSET $3`, ['%console%', 'level5', 0]],
  ['editor lookup', `
    SELECT q.id, q.question_number, q.question_text, q.options, q.correct_index,
           q.correct_text, q.explanation, q.identity_key, q.source_question_key,
           q.raw, b.book_name, f.subtopic, c.id AS chapter_id, c.chapter_name
    FROM questions q
    LEFT JOIN files f ON f.id = q.file_ref_id
    LEFT JOIN books b ON b.id = f.book_id
    LEFT JOIN chapters c ON c.id = b.chapter_id
    WHERE q.id = $1`, [1]],
  ['level filter list', 'SELECT level_key FROM levels ORDER BY 1', []],
  ['answer conflict count', `
    SELECT count(*)::int AS conflicting FROM (
      SELECT c.id AS chapter, q.identity_key
      FROM questions q
      JOIN files f ON f.id = q.file_ref_id
      JOIN books b ON b.id = f.book_id
      JOIN chapters c ON c.id = b.chapter_id
      WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
      GROUP BY 1, 2 HAVING count(*) > 1
        AND count(DISTINCT abhyas_answer_key(q.correct_text, q.correct_index, q.options)) > 1) groups`, []],
  ['reject candidate', `UPDATE dedupe_review_candidates SET status = 'rejected', reviewed_at = now(), review_note = coalesce(review_note, 'reviewer: not duplicate') WHERE id = $1`, [1]],
  ['candidate merge lookup', `SELECT q1_id, q2_id, q1_identity_key, q2_identity_key
        FROM dedupe_review_candidates WHERE id = $1`, [1]],
  ['record candidate merge', `UPDATE dedupe_review_candidates SET status = 'merged', reviewed_at = now(),
        review_note = 'merged into #' || $2 WHERE id = $1`, [1, 2]],
];

for (const [label, query, params] of consoleQueries) {
  try {
    const result = await db.query(query, params);
    check(`console query: ${label}`, true, result.rows.length);
  } catch (error) {
    check(`console query: ${label}`, false, error.message);
  }
}

// The editor's save path, including the source-correction alias.
const edited = await rows('SELECT id, identity_key FROM questions WHERE identity_key = $1', ['ccc']);
const editedId = edited[0].id;
const newIdentity = await Identity.identity('Console smoke question, corrected?', ['Newton', 'Joule']);
const location = await Identity.sourceLocationKey('Sunil Sah', 'Chain survey');
try {
  await db.query(`
        UPDATE questions
           SET question_text = $1, options = $2::jsonb, correct_index = $3,
               correct_text = $4, explanation = $5, identity_key = $6,
               source_identity = coalesce(source_identity, $7),
               identity_version = 2, corrected_at = now(), quality_score = $8
         WHERE id = $9`,
  ['Console smoke question, corrected?', JSON.stringify(['Newton', 'Joule']), 0, 'Newton',
    'because', newIdentity, 'ccc', 30, editedId]);
  await db.query(`
            INSERT INTO correction_source_aliases_v2
              (chapter_id, source_identity, source_location, source_number, question_text,
               options, correct_index, correct_text, explanation, identity_key,
               quality_score, corrected_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, now())`,
  [1, 'ccc', location, '21', 'Console smoke question, corrected?',
    JSON.stringify(['Newton', 'Joule']), 0, 'Newton', 'because', newIdentity, 30]);
  const alias = await rows('SELECT identity_key, source_location FROM correction_source_aliases_v2');
  check('editor save writes the correction alias',
    alias.length === 1 && alias[0].identity_key === newIdentity && alias[0].source_location === location,
    alias);
  const updated = await rows('SELECT identity_key, identity_version, corrected_at FROM questions WHERE id = $1', [editedId]);
  check('editor save rewrites the identity and marks the question corrected',
    updated[0].identity_key === newIdentity && Number(updated[0].identity_version) === 2
    && updated[0].corrected_at !== null, updated[0]);
} catch (error) {
  check('editor save path', false, error.message);
}

if (failures.length) {
  console.error(`admin SQL FAILED (${failures.length}/${checks})`);
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}
console.log(`admin SQL OK: ${checks} checks passed against Postgres (PGlite)`);
