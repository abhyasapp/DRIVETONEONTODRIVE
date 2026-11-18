// Offline checks for apps_script/AdminReports.gs (validation, routing, auth,
// the Supabase patch and the GitHub dispatch) using stubbed Apps Script globals.
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const failures = [];
let checks = 0;
const check = (label, ok, detail) => { checks++; if (!ok) failures.push(`- ${label}${detail !== undefined ? `\n  got: ${JSON.stringify(detail)}` : ''}`); };

let props = {}, fetched = [], fetchAllReply = () => [], fetchReply = () => ({ getResponseCode: () => 204 });
globalThis.PropertiesService = { getScriptProperties: () => ({ getProperty: k => props[k] ?? null }) };
globalThis.ContentService = { MimeType: { JSON: 'json' },
  createTextOutput: text => ({ text, setMimeType() { return this; }, parsed: () => JSON.parse(text) }) };
globalThis.UrlFetchApp = {
  fetchAll: reqs => { fetched.push(...reqs); return fetchAllReply(reqs); },
  fetch: (url, opts) => { fetched.push({ url, ...opts }); return fetchReply(url, opts); } };

const gs = require(path.join(root, 'apps_script', 'AdminReports.gs'));
const resp = (code, body = '[]') => ({ getResponseCode: () => code, getContentText: () => body });

// ---- routing
check('unrelated actions are left to the existing doPost', gs.adminReportsRoute_({ action: 'syncCorrectionsSince' }) === null);

// ---- resolvereports
props = { ABHYAS_SYNC_KEY: 'k1', SUPABASE_URL: 'https://x.supabase.co/', SUPABASE_SERVICE_KEY: 'svc' };
const item = { id: 'rep-1', status: 'resolved', resolvedAt: '2026-11-18T10:00:00Z', resolvedBy: 'console', corrected: true, correctedSnapshot: '{"q":1}' };
let out = gs.adminReportsRoute_({ action: 'resolvereports', key: 'wrong', items: [item] }).parsed();
check('resolvereports rejects a wrong key', out.success === false && out.error === 'unauthorized', out);
check('resolvereports rejects a missing key', gs.adminReportsRoute_({ action: 'resolvereports', items: [item] }).parsed().success === false);
check('bad status is rejected', gs.validateResolveItems_([{ ...item, status: 'weird' }]).error !== undefined);
check('hostile ids are rejected', gs.validateResolveItems_([{ ...item, id: "x' or 1=1" }]).error !== undefined);
check('oversized batches are rejected', gs.validateResolveItems_(Array(201).fill(item)).error !== undefined);

fetched = [];
fetchAllReply = reqs => reqs.map((_, i) => i === 0 ? resp(200, '[{"id":"rep-1"}]') : i === 1 ? resp(200, '[]') : resp(500, 'boom'));
out = gs.adminReportsRoute_({ action: 'resolvereports', key: 'k1',
  items: [item, { ...item, id: 'rep-gone' }, { ...item, id: 'rep-bad' }] }).parsed();
check('updated / missing / failed are reported separately',
  JSON.stringify([out.updated, out.missing, out.failed]) === JSON.stringify([['rep-1'], ['rep-gone'], ['rep-bad']]), out);
check('PATCH targets one report with an encoded id filter and a service key',
  fetched[0].method === 'patch' && fetched[0].url === 'https://x.supabase.co/rest/v1/question_reports?id=eq.rep-1'
  && fetched[0].headers.apikey === 'svc', fetched[0]);
const patch = JSON.parse(fetched[0].payload);
check('resolved patch carries snapshot and timestamps', patch.status === 'resolved' && patch.corrected === true && patch.resolved_by === 'console' && patch.corrected_snapshot === '{"q":1}', patch);
const reopen = gs.buildReportPatch_({ id: 'a', status: 'open', corrected: false });
check('reopen clears resolution fields', reopen.resolved_at === null && reopen.resolved_by === null && reopen.corrected_snapshot === null, reopen);

// ---- admincorrect
const good = { qn: '50011.110001', question: 'Fixed?', options: ['A', 'B', 'C'], correctIndex: 2, explanation: 'why' };
props = { ABHYAS_ADMIN_KEY: 'adm', ABHYAS_SYNC_KEY: 'k1', GITHUB_TOKEN: 'ghp_x' };
check('admincorrect refuses the sync key (separate admin secret)',
  gs.adminReportsRoute_({ action: 'admincorrect', adminKey: 'k1', items: [good] }).parsed().error === 'unauthorized');
for (const [label, bad] of [
  ['empty question', { ...good, question: ' ' }], ['one option', { ...good, options: ['A'] }],
  ['blank option', { ...good, options: ['A', ' '] }], ['out-of-range answer', { ...good, correctIndex: 3 }],
  ['fractional answer', { ...good, correctIndex: 1.5 }], ['string answer', { ...good, correctIndex: '1' }],
  ['bad qn', { ...good, qn: 'a b' }]]) {
  check(`validation rejects ${label}`, gs.validateAdminCorrections_([bad]).error !== undefined);
}
check('duplicate qn in one batch is rejected', gs.validateAdminCorrections_([good, good]).error !== undefined);
check('more than 50 corrections are rejected', gs.validateAdminCorrections_(Array.from({ length: 51 }, (_, i) => ({ ...good, qn: 'q' + i }))).error !== undefined);
check('oversized payload is refused before calling GitHub',
  gs.buildDispatchBody_([{ ...good, explanation: 'x'.repeat(61000) }]).error !== undefined);

fetched = [];
out = gs.adminReportsRoute_({ action: 'admincorrect', adminKey: 'adm', items: [good], publish: false }).parsed();
check('admincorrect dispatches the workflow and reports how many were queued', out.success === true && out.queued === 1, out);
check('dispatch goes to the workflow endpoint with a bearer token',
  fetched[0].url === 'https://api.github.com/repos/abhyasapp/DRIVETONEONTODRIVE/actions/workflows/apply_admin_corrections.yml/dispatches'
  && fetched[0].headers.Authorization === 'Bearer ghp_x', fetched[0]);
const sent = JSON.parse(fetched[0].payload);
check('inputs carry the cleaned corrections and the publish flag',
  sent.ref === 'main' && sent.inputs.publish === 'false' && JSON.parse(sent.inputs.corrections_json)[0].qn === '50011.110001', sent);
fetchReply = () => resp(422, 'nope');
check('a GitHub failure is surfaced', gs.adminReportsRoute_({ action: 'admincorrect', adminKey: 'adm', items: [good] }).parsed().success === false);

check('safeEqual is length-safe', gs.adminSafeEqual_('abc', 'abcd') === false && gs.adminSafeEqual_('', '') === false && gs.adminSafeEqual_('abc', 'abc') === true);

if (failures.length) { console.error(`apps script FAILED (${failures.length}/${checks})\n${failures.join('\n')}`); process.exit(1); }
console.log(`apps script OK: ${checks} checks passed`);
