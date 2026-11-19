/**
 * AdminReports.gs - drop-in module for the Abhyas Apps Script web app.
 *
 * Adds two POST actions next to the existing syncreportssince /
 * syncCorrectionsSince ones:
 *
 *   resolvereports   (sync key)   The pipeline reports which reports it closed
 *                                 or reopened in Neon; this writes that status
 *                                 into the reports table so the app stops
 *                                 showing them as open.
 *   admincorrect     (admin key)  An admin fixes a question by its qn id from
 *                                 anywhere that can reach this script. The
 *                                 script validates the fix and starts the
 *                                 "Apply admin corrections" GitHub workflow,
 *                                 which writes it to Neon, closes the matching
 *                                 reports, republishes the JSON to Drive and
 *                                 calls resolvereports back.
 *
 * Wire it up in your existing doPost, before your own action routing:
 *
 *     var req = JSON.parse(e.postData.contents);
 *     var handled = adminReportsRoute_(req);
 *     if (handled) return handled;
 *
 * Script Properties (Project settings -> Script properties):
 *     ABHYAS_SYNC_KEY       same value as the pipeline's ABHYAS_SYNC_KEY secret
 *     ABHYAS_ADMIN_KEY      NEW secret, only for admincorrect (not the sync key)
 *     SUPABASE_URL          https://<project>.supabase.co
 *     SUPABASE_SERVICE_KEY  service-role key (server side only)
 *     GITHUB_TOKEN          fine-grained token, this repo only, Actions: read+write
 *
 * Adjust ADMIN_REPORTS_CONFIG_.columns if your table uses other column names.
 */

var ADMIN_REPORTS_CONFIG_ = {
  reportsTable: 'question_reports',
  columns: {
    status: 'status',
    resolvedAt: 'resolved_at',
    resolvedBy: 'resolved_by',
    corrected: 'corrected',
    correctedSnapshot: 'corrected_snapshot'
  },
  githubRepo: 'abhyasapp/DRIVETONEONTODRIVE',
  workflow: 'apply_admin_corrections.yml',
  ref: 'main',
  maxResolveBatch: 200,
  maxCorrections: 50,
  maxDispatchChars: 60000   // GitHub caps all workflow inputs at 65,535 chars
};

/* ------------------------------------------------------------- routing */

function adminReportsRoute_(req) {
  if (!req || (req.action !== 'resolvereports' && req.action !== 'admincorrect')) {
    return null;
  }
  try {
    var result = req.action === 'resolvereports' ? resolveReports_(req) : adminCorrect_(req);
    return adminJsonOut_(result);
  } catch (err) {
    return adminJsonOut_({ success: false, error: String(err && err.message || err) });
  }
}

function adminJsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function adminProp_(name) {
  return PropertiesService.getScriptProperties().getProperty(name) || '';
}

function adminSafeEqual_(a, b) {
  a = String(a || ''); b = String(b || '');
  if (!a || a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/* ---------------------------------------------------- resolvereports */

function validateResolveItems_(items) {
  var cfg = ADMIN_REPORTS_CONFIG_;
  if (!Array.isArray(items) || !items.length) return { error: 'items must be a non-empty list' };
  if (items.length > cfg.maxResolveBatch) return { error: 'too many items in one call' };
  var out = [];
  for (var i = 0; i < items.length; i++) {
    var it = items[i] || {};
    var id = String(it.id == null ? '' : it.id);
    if (!/^[A-Za-z0-9_.:-]{1,64}$/.test(id)) return { error: 'item ' + i + ': bad id' };
    if (it.status !== 'open' && it.status !== 'resolved') return { error: 'item ' + i + ': bad status' };
    var snap = it.correctedSnapshot == null ? null : String(it.correctedSnapshot);
    if (snap && snap.length > 20000) return { error: 'item ' + i + ': snapshot too large' };
    var when = it.resolvedAt ? new Date(it.resolvedAt) : null;
    if (when && isNaN(when.getTime())) return { error: 'item ' + i + ': bad resolvedAt' };
    out.push({
      id: id,
      status: it.status,
      resolvedAt: when ? when.toISOString() : null,
      resolvedBy: it.resolvedBy ? String(it.resolvedBy).slice(0, 100) : null,
      corrected: !!it.corrected,
      correctedSnapshot: snap
    });
  }
  return { items: out };
}

function buildReportPatch_(item) {
  var c = ADMIN_REPORTS_CONFIG_.columns, body = {};
  body[c.status] = item.status;
  body[c.resolvedAt] = item.status === 'resolved' ? item.resolvedAt : null;
  body[c.resolvedBy] = item.status === 'resolved' ? item.resolvedBy : null;
  body[c.corrected] = item.corrected;
  body[c.correctedSnapshot] = item.corrected ? item.correctedSnapshot : null;
  return body;
}

function resolveReports_(req) {
  if (!adminSafeEqual_(req.key, adminProp_('ABHYAS_SYNC_KEY'))) {
    return { success: false, error: 'unauthorized' };
  }
  var checked = validateResolveItems_(req.items);
  if (checked.error) return { success: false, error: checked.error };

  var base = adminProp_('SUPABASE_URL'), svc = adminProp_('SUPABASE_SERVICE_KEY');
  if (!base || !svc) return { success: false, error: 'SUPABASE_URL / SUPABASE_SERVICE_KEY not set' };

  var table = ADMIN_REPORTS_CONFIG_.reportsTable;
  var requests = checked.items.map(function (item) {
    return {
      url: base.replace(/\/$/, '') + '/rest/v1/' + table + '?id=eq.' + encodeURIComponent(item.id),
      method: 'patch',
      contentType: 'application/json',
      headers: { apikey: svc, Authorization: 'Bearer ' + svc, Prefer: 'return=representation' },
      payload: JSON.stringify(buildReportPatch_(item)),
      muteHttpExceptions: true
    };
  });
  var responses = UrlFetchApp.fetchAll(requests);

  var updated = [], missing = [], failed = [];
  responses.forEach(function (resp, i) {
    var id = checked.items[i].id, code = resp.getResponseCode();
    if (code >= 200 && code < 300) {
      var rows = [];
      try { rows = JSON.parse(resp.getContentText() || '[]'); } catch (e) { rows = []; }
      (rows.length ? updated : missing).push(id);
    } else {
      failed.push(id);
    }
  });
  // the pipeline only clears what is acknowledged in updated/missing
  return { success: true, updated: updated, missing: missing, failed: failed };
}

/* -------------------------------------------------------- admincorrect */

function validateAdminCorrections_(items) {
  var cfg = ADMIN_REPORTS_CONFIG_;
  if (!Array.isArray(items) || !items.length) return { error: 'items must be a non-empty list' };
  if (items.length > cfg.maxCorrections) return { error: 'at most ' + cfg.maxCorrections + ' corrections per call' };
  var seen = {}, out = [];
  for (var i = 0; i < items.length; i++) {
    var it = items[i] || {}, tag = 'item ' + i + ': ';
    var qn = String(it.qn == null ? '' : it.qn).trim();
    if (!/^[A-Za-z0-9_.-]{1,40}$/.test(qn)) return { error: tag + 'bad qn id' };
    if (seen[qn]) return { error: tag + 'duplicate qn id ' + qn };
    seen[qn] = true;
    var question = String(it.question == null ? '' : it.question).trim();
    if (!question || question.length > 2000) return { error: tag + 'question must be 1-2000 chars' };
    var options = it.options;
    if (!Array.isArray(options) || options.length < 2 || options.length > 10) return { error: tag + 'need 2-10 options' };
    var cleaned = [];
    for (var j = 0; j < options.length; j++) {
      var o = String(options[j] == null ? '' : options[j]).trim();
      if (!o || o.length > 1000) return { error: tag + 'option ' + j + ' must be 1-1000 chars' };
      cleaned.push(o);
    }
    var ci = it.correctIndex;
    if (typeof ci !== 'number' || ci % 1 !== 0 || ci < 0 || ci >= cleaned.length) {
      return { error: tag + 'correctIndex must be an integer inside the options' };
    }
    var explanation = it.explanation == null ? '' : String(it.explanation);
    if (explanation.length > 4000) return { error: tag + 'explanation too long' };
    out.push({
      qn: qn, question: question, options: cleaned, correctIndex: ci,
      explanation: explanation, note: it.note ? String(it.note).slice(0, 200) : ''
    });
  }
  return { items: out };
}

function buildDispatchBody_(items, publish) {
  var json = JSON.stringify(items);
  if (json.length > ADMIN_REPORTS_CONFIG_.maxDispatchChars) {
    return { error: 'payload too large for one workflow run; send fewer corrections' };
  }
  return {
    body: {
      ref: ADMIN_REPORTS_CONFIG_.ref,
      inputs: { corrections_json: json, publish: publish === false ? 'false' : 'true' }
    }
  };
}

function adminCorrect_(req) {
  if (!adminSafeEqual_(req.adminKey, adminProp_('ABHYAS_ADMIN_KEY'))) {
    return { success: false, error: 'unauthorized' };
  }
  var checked = validateAdminCorrections_(req.items);
  if (checked.error) return { success: false, error: checked.error };
  var built = buildDispatchBody_(checked.items, req.publish);
  if (built.error) return { success: false, error: built.error };

  var token = adminProp_('GITHUB_TOKEN');
  if (!token) return { success: false, error: 'GITHUB_TOKEN not set' };
  var cfg = ADMIN_REPORTS_CONFIG_;
  var resp = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + cfg.githubRepo + '/actions/workflows/' +
      cfg.workflow + '/dispatches',
    {
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
      },
      payload: JSON.stringify(built.body),
      muteHttpExceptions: true
    });
  if (resp.getResponseCode() !== 204) {
    return { success: false, error: 'GitHub dispatch failed (HTTP ' + resp.getResponseCode() + ')' };
  }
  return { success: true, queued: checked.items.length };
}

// Lets tests/test_apps_script.mjs load this file in Node; Apps Script ignores it.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    adminReportsRoute_: adminReportsRoute_, validateResolveItems_: validateResolveItems_,
    validateAdminCorrections_: validateAdminCorrections_, buildDispatchBody_: buildDispatchBody_,
    buildReportPatch_: buildReportPatch_, resolveReports_: resolveReports_,
    adminCorrect_: adminCorrect_, adminSafeEqual_: adminSafeEqual_
  };
}
