/* Runs the SQL the Reported tab sends against a real Postgres (PGlite, WASM).
 *
 *   PGLITE_MODULE=.tmp/pglite/node_modules/@electric-sql/pglite/dist/index.cjs \
 *     node tests/test_reported_sql.mjs
 *
 * The console has no build step, so this is the closest offline equivalent of
 * clicking through the tab: same aggregates, same filter builder, same paging.
 */
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

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
  throw new Error(`PGlite is not installed (${lastError && lastError.message}).`);
}

const { PGlite } = await loadPglite();
const db = new PGlite();

const failures = [];
let checks = 0;
function check(label, condition, detail) {
  checks += 1;
  if (!condition) failures.push(`${label}${detail === undefined ? '' : `\n  got: ${JSON.stringify(detail)}`}`);
}
const rows = async (sql, params = []) => (await db.query(sql, params)).rows;

await db.exec(`
CREATE TABLE questions (
    id bigserial PRIMARY KEY,
    question_text text,
    options jsonb,
    correct_index integer,
    corrected_at timestamptz
);
CREATE TABLE reported_questions (
    id                TEXT PRIMARY KEY,
    uid               TEXT,
    qid               BIGINT,
    file_id           TEXT,
    question_snapshot TEXT,
    options_snapshot  TEXT,
    correct_index     INTEGER,
    chosen_index      INTEGER,
    reason            TEXT,
    note              TEXT,
    reported_by       TEXT,
    reported_at       TIMESTAMPTZ,
    status            TEXT,
    resolved_at       TIMESTAMPTZ,
    resolved_by       TEXT,
    corrected         BOOLEAN DEFAULT FALSE,
    corrected_snapshot TEXT,
    section           TEXT,
    app_version       TEXT,
    migrated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO questions (id, question_text, options, correct_index) VALUES
  (101, 'Which is the capital of India?', '["Mumbai","New Delhi","Kolkata"]'::jsonb, 1),
  (102, 'Explain the water cycle.', '[]'::jsonb, NULL);
INSERT INTO reported_questions
  (id, uid, qid, file_id, question_snapshot, options_snapshot, correct_index, chosen_index,
   reason, note, reported_by, reported_at, status, corrected, section, app_version)
VALUES
  ('rep-a', 'u1', 101, 'f-1', 'Which is the capital of India?',
   '["Mumbai","New Delhi","Kolkata"]', 1, 0, 'wrong_answer', 'shows Mumbai', 'app',
   now() - interval '1 day', 'open', false, 'practice', '2.3.1'),
  ('rep-b', 'u2', NULL, NULL, 'Explain the water cycle.', NULL, NULL, NULL,
   NULL, 'no options at all', NULL, now() - interval '2 days', 'open', false, NULL, '2.2.0'),
  ('rep-c', 'u3', 102, 'f-2', 'Explain the water cycle.', '[]', NULL, 1, 'typo', NULL, 'app',
   now() - interval '30 days', 'resolved', true, 'practice', '2.1.0');
`);

/* ---- the same statements index.html sends ---- */

const summary = (await rows(`
  SELECT count(*)::int AS total,
         count(*) FILTER (WHERE status = 'resolved')::int AS resolved,
         count(*) FILTER (WHERE status IS DISTINCT FROM 'resolved')::int AS open,
         count(*) FILTER (WHERE qid IS NULL)::int AS unlinked,
         count(*) FILTER (WHERE corrected)::int AS corrected,
         count(DISTINCT uid)::int AS reporters,
         count(*) FILTER (WHERE reported_at >= now() - interval '7 days')::int AS week
  FROM reported_questions`))[0];
check('summary total', summary.total === 3, summary.total);
check('summary open counts NULL status as open', summary.open === 2, summary.open);
check('summary resolved', summary.resolved === 1, summary.resolved);
check('summary unlinked', summary.unlinked === 1, summary.unlinked);
check('summary corrected', summary.corrected === 1, summary.corrected);
check('summary distinct reporters', summary.reporters === 3, summary.reporters);
check('summary last 7 days', summary.week === 2, summary.week);

const reasons = await rows(`SELECT coalesce(reason, '') AS value, count(*)::int AS n
                            FROM reported_questions GROUP BY 1 ORDER BY n DESC`);
check('reason facet includes the empty bucket', reasons.some(r => r.value === '' && r.n === 1), reasons);
check('reason facet orders by count', reasons[0].n === 1 && reasons.length === 3, reasons);

const sections = await rows(`SELECT coalesce(section, '') AS value, count(*)::int AS n
                             FROM reported_questions GROUP BY 1 ORDER BY 1`);
check('section facet buckets', sections.length === 2, sections);

const LIST = `
  SELECT rq.id AS report_id, rq.uid, rq.qid, rq.file_id, rq.question_snapshot,
         rq.options_snapshot, rq.correct_index, rq.chosen_index, rq.reason,
         rq.note, rq.reported_by, rq.reported_at, rq.status, rq.resolved_at,
         rq.resolved_by, rq.corrected, rq.section, rq.app_version,
         q.id AS live_id, q.question_text, q.options,
         q.correct_index AS q_correct_index, q.corrected_at AS q_corrected_at
  FROM reported_questions rq
  LEFT JOIN questions q ON q.id = rq.qid`;

const all = await rows(`${LIST} ORDER BY rq.reported_at DESC NULLS LAST, rq.id DESC LIMIT 25 OFFSET $1`, [0]);
check('list returns every report', all.length === 3, all.length);
check('newest first', all[0].report_id === 'rep-a', all.map(r => r.report_id));
check('linked report joins the live question', all[0].live_id === 101 && all[0].question_text === 'Which is the capital of India?', all[0]);
check('unlinked report keeps a NULL live_id', all[1].live_id === null && all[1].question_text === null, all[1]);

const ascending = await rows(`${LIST} ORDER BY rq.reported_at ASC NULLS LAST, rq.id ASC LIMIT 25 OFFSET $1`, [0]);
check('oldest first flips the order', ascending[0].report_id === 'rep-c', ascending.map(r => r.report_id));

const openOnly = await rows(`${LIST} WHERE rq.status = $1 ORDER BY rq.reported_at DESC LIMIT 25 OFFSET $2`, ['open', 0]);
check('status filter', openOnly.length === 2, openOnly.length);

const unlinked = await rows(`${LIST} WHERE rq.qid IS NULL ORDER BY rq.reported_at DESC LIMIT 25`, []);
check('unlinked filter', unlinked.length === 1 && unlinked[0].report_id === 'rep-b', unlinked.length);

const search = await rows(`${LIST}
  WHERE (rq.question_snapshot ILIKE $1 OR coalesce(rq.note, '') ILIKE $1
         OR rq.uid ILIKE $1 OR rq.id ILIKE $1)
  ORDER BY rq.reported_at DESC LIMIT 25`, ['%mumbai%']);
check('search matches the snapshot text', search.length === 1 && search[0].report_id === 'rep-a', search.length);

const searchNote = await rows(`${LIST}
  WHERE (rq.question_snapshot ILIKE $1 OR coalesce(rq.note, '') ILIKE $1
         OR rq.uid ILIKE $1 OR rq.id ILIKE $1)
  ORDER BY rq.reported_at DESC LIMIT 25`, ['%rep-b%']);
check('search matches a report id', searchNote.length === 1 && searchNote[0].report_id === 'rep-b', searchNote.length);

const noReason = await rows(`${LIST} WHERE coalesce(rq.reason, '') = '' ORDER BY rq.reported_at DESC LIMIT 25`, []);
check('"no reason" filter', noReason.length === 1 && noReason[0].report_id === 'rep-b', noReason.length);

const paged = await rows(`${LIST} ORDER BY rq.reported_at DESC NULLS LAST, rq.id DESC LIMIT 2 OFFSET $1`, [2]);
check('paging offset', paged.length === 1 && paged[0].report_id === 'rep-c', paged.map(r => r.report_id));

const resolved = await rows(`UPDATE reported_questions
  SET status = $1, resolved_at = now(), resolved_by = $2
  WHERE id = $3 RETURNING id`, ['resolved', 'console', 'rep-a']);
check('resolve updates the row', resolved.length === 1 && resolved[0].id === 'rep-a', resolved);
const afterResolve = (await rows(`SELECT status, resolved_at, resolved_by FROM reported_questions WHERE id = 'rep-a'`))[0];
check('resolve stamps time and actor', afterResolve.status === 'resolved'
  && afterResolve.resolved_at !== null && afterResolve.resolved_by === 'console', afterResolve);

const reopened = await rows(`UPDATE reported_questions
  SET status = $1, resolved_at = NULL, resolved_by = NULL
  WHERE id = $2 RETURNING id`, ['open', 'rep-a']);
check('reopen updates the row', reopened.length === 1, reopened);
const afterReopen = (await rows(`SELECT status, resolved_at, resolved_by FROM reported_questions WHERE id = 'rep-a'`))[0];
check('reopen clears the resolution', afterReopen.status === 'open'
  && afterReopen.resolved_at === null && afterReopen.resolved_by === null, afterReopen);

if (failures.length) {
  console.error(`reported SQL FAILED (${failures.length} of ${checks} checks)`);
  failures.forEach(failure => console.error(`  - ${failure}`));
  process.exit(1);
}
console.log(`reported SQL OK: ${checks} checks passed against Postgres (PGlite)`);
