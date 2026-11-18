# Backend actions for reports and admin corrections

The pipeline talks to one backend URL (`ABHYAS_APPS_SCRIPT_URL`) with a JSON
POST `{"action": ..., "key": SYNC_KEY, ...}`. Today it uses `syncreportssince`
and `syncCorrectionsSince`. This version adds two more actions. The backend is
not part of this repo, so `AdminReports.gs` is a ready-made drop-in for the
Google Apps Script deployment; if your live backend is the Supabase Edge
Function (see SECRETS.md), implement the same two actions there - the contract
below is all the pipeline depends on.

## Contract

`resolvereports` - auth: `key` (the sync key). Called by `sync.sync_reports`.

```json
request:  {"action":"resolvereports","key":"...","items":[
            {"id":"rep-1","status":"resolved","resolvedAt":"2026-11-18T10:00:00+00:00",
             "resolvedBy":"console","corrected":true,"correctedSnapshot":"{...json text...}"}]}
response: {"success":true,"updated":["rep-1"],"missing":[],"failed":[]}
```
`status` is `resolved` or `open` (a reopen after an undo). Ids in `updated` and
`missing` are acknowledged and stop being pending; ids in `failed` stay pending
and are retried on the next run. An unknown-action error just leaves everything
pending (the pipeline logs a warning and carries on).

`admincorrect` - auth: `adminKey` (a separate secret from the sync key).
Called by whatever admin UI you build.

```json
request:  {"action":"admincorrect","adminKey":"...","publish":true,"items":[
            {"qn":"50011.110001","question":"...","options":["A","B","C"],
             "correctIndex":2,"explanation":"","note":""}]}
response: {"success":true,"queued":1}
```
The script validates every item, then starts the `Apply admin corrections`
GitHub workflow with the items as its input. That workflow writes to Neon,
closes the matching reports, re-exports and uploads (unless `publish` is
false) and calls `resolvereports` back. `qn` is the public qn id or the numeric
question id.

## Install (Apps Script)

1. Add `AdminReports.gs` to the Apps Script project.
2. In your existing `doPost`, right after parsing the request body:
   ```js
   var handled = adminReportsRoute_(req);
   if (handled) return handled;
   ```
3. Project settings -> Script properties:

   | Property | Value |
   |---|---|
   | `ABHYAS_SYNC_KEY` | same as the pipeline's `ABHYAS_SYNC_KEY` secret |
   | `ABHYAS_ADMIN_KEY` | new random secret, used only for `admincorrect` |
   | `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | where the reports table lives |
   | `GITHUB_TOKEN` | fine-grained token, **only this repo**, permission *Actions: Read and write* |

4. If your reports table is not `question_reports` or uses other column names,
   edit `ADMIN_REPORTS_CONFIG_` at the top of the file.
5. Deploy -> Manage deployments -> edit -> new version.

`node tests/test_apps_script.mjs` checks validation, auth, the Supabase PATCH
and the GitHub dispatch with stubbed Apps Script globals; it cannot replace one
manual check against the real deployment (send a `resolvereports` for a test
report and confirm the row changes).
