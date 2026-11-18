# Runbook

For whoever is on call for the pipeline. Everything here is something the code
actually does; the step names come from `python -m pipeline.run_pipeline --list`.

## The daily run in one paragraph

`migrate -> sync -> corrections -> dedupe -> reports -> kw_l5/kw_l7/kw_gk ->
gemini -> qn_ids -> export -> upload -> chapters -> chapters_up`.
**Fatal** steps stop the run so stale data is never published as fresh:
migrate, sync, corrections, dedupe, export, upload, chapters, chapters_up.
**Non-fatal** steps (reports, kw_*, gemini, qn_ids) do not turn the run red, but a
failure there opens a "Pipeline warnings" issue and a run-page annotation, so
it cannot go unnoticed.

## A run failed: where to look

Open the run, read the **step summary** table at the top, then the log of the
first FAILED step. A failed scheduled run also opens a "Daily pipeline failed"
issue with the link.

| Symptom (exit code / message) | Meaning | What to do |
|---|---|---|
| exit 2, `Pre-flight failed` | a secret, key or credential file is missing | the message names it; fix the secret and re-run |
| `migrate` failed | a migration file errored and was rolled back | fix the SQL by **adding a new** migration (never edit an applied one); `python -m utils.migrate --status` shows state |
| `sync` -> `ABORT ... missing from Drive` | more than 25 files / 10% look deleted: almost always a renamed, moved or unshared Drive folder | restore the folder; only if the removal is intended, run locally `SYNC_ALLOW_MASS_DELETE=1 python -m pipeline.run_pipeline --only sync` |
| `corrections` exit 1, `Backend error` | backend unreachable or key rejected | check `ABHYAS_APPS_SCRIPT_URL` / `ABHYAS_SYNC_KEY`; the watermark is **not** advanced, nothing is lost |
| `corrections` warns `N failed ... stay queued` | individual corrections could not apply | they wait in `correction_backlog` and are retried every run; `CORRECTIONS_STRICT=1` makes them fatal |
| `dedupe` exit 3, `ABORT ... exceed the safety cap` | an unusually large merge was planned | inspect with `python -m dedupe.dedupe_exact --samples 20` (a dry run unless you pass `--apply`); if intended, once: `DEDUPE_ALLOW_LARGE=1` |
| `export` -> `no stable public qn id` | new questions have no public id yet | run `python -m pipeline.run_pipeline --only qn_ids` then re-run from `export` |
| `upload` / `chapters_up` auth errors | Drive OAuth token expired or revoked | regenerate with `python -m utils.extract_token`, update `GDRIVE_OAUTH_TOKEN`; make sure the OAuth consent screen is **In production** (testing mode expires tokens after 7 days) |
| `chapters_up` log says `created` again | the folder lookup found nothing | check the Drive parent is the right folder and the OAuth account can see the file; expected only on the very first upload |
| `WARNING: N copies of chapters-data.js` | duplicate files from earlier runs | the newest is updated in place; point the app at that id, then `python -m tools.cleanup_drive` |
| `reports` warns `no 'resolvereports' action yet` | backend not updated | deploy `apps_script/AdminReports.gs` (see `apps_script/README.md`); pending changes just wait |
| `gemini` skipped `(budget)` | the run was too long for Gemini to start | it runs next time; or run it alone: `--only gemini` |

## Re-running part of the pipeline

- Actions tab -> *Daily pipeline* -> **Run workflow**: `only` = comma-separated
  steps, or `from_step` = resume from a step onward (e.g. `export` after a
  failure there), `skip_gemini` to skip the long step.
- Locally: `python -m pipeline.run_pipeline --only export,upload`,
  `--from-step export`, `--skip gemini`, `--dry-run` (prints the plan), `--list`.

## Fixing a question by its qn id

Console -> Questions -> *Correct by qn id* (or Reported -> *Fix question*). The
same atomic SQL function is behind the CLI
(`python -m sync.admin_apply fixes.json --apply`) and the backend `admincorrect`
action. Every correction is audited and can be undone from *Recent corrections*.
It reaches students at the next export; to publish sooner run the workflow with
`only` = `qn_ids,export,upload,chapters,chapters_up`.

## Before anything risky

1. Take a Neon branch or snapshot.
2. Try it on a branch with a manual workflow run first.
3. Rollback: revert the commit; the SQL and migrations are additive
   (`IF NOT EXISTS`); restore the Neon snapshot for data.

## Routine care

- **Keep the repo active.** GitHub pauses scheduled workflows after 60 days
  without a commit; merging the weekly Dependabot PRs does that for you.
- Rotate `GDRIVE_OAUTH_TOKEN`, the sync key and (if used) `ABHYAS_ADMIN_KEY`
  and the backend's `GITHUB_TOKEN` on a schedule you can keep.
- Review Dependabot PRs when CI is green; they update Python packages and the
  pinned action SHAs.
- Log artifacts in a **public** repo are readable by any signed-in GitHub
  user. Make the repo private if the logs are sensitive.

## Limits you can tune (all environment variables)

See the "Safety limits" table in `README.md`.
