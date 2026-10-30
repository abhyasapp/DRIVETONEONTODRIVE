# Abhyas daily pipeline

Drive -> Neon (sync) -> corrections -> dedupe -> keyword + Gemini classify ->
export JSON -> upload to Drive -> chapters-data.js -> upload.

```
.github/workflows/daily_pipeline.yml   CI schedule + manual run
pipeline/run_pipeline.py               orchestrator (retries, timeouts, exit codes)
sync/        sync_any_level, apply_corrections
dedupe/      dedupe_exact (pipeline), dedupe_review (dry-run report)
             scope: duplicates are grouped per CHAPTER (same identity_key
             inside one chapter), never across chapters/levels.
             policy: every copy that agrees with a sibling is merged, even
             inside a group where other copies disagree on the answer;
             only copies with no agreeing sibling are kept.
             near-duplicates (fuzzy): tools/near_duplicates.sql
classify/    classify_any_level (keywords), classify_with_gemini (LLM)
export/      export_any_level, generate_chapters_data_all
upload/      upload_any_level, upload_chapters_data
             safety: the prune pass is skipped for a level whenever any
             local file cannot be mapped to a DB subtopic, so good Drive
             files are never trashed on a partial/late classification.
             failures: any level or file error makes the script exit non-zero.
utils/       db.py (Neon retry), drive_auth.py (OAuth), extract_token.py
config/      settings.py (Drive parent, paths) + keyword JSONs
review/      dashboard.py + duplicate_reviews.py  (local admin reviewer)
             index.html + admin_functions.sql + identity.js  (hosted console)
tools/       cleanup_drive, boost_keywords   (manual helpers)
tests/       smoke.py                        (offline, runs in CI)
archive/     one-off scripts, not used by the pipeline
```

## Run

```bash
pip install -r requirements.txt
python tests/smoke.py                          # offline sanity check
python -m pipeline.run_pipeline --list         # step names
python -m pipeline.run_pipeline --dry-run
python -m pipeline.run_pipeline --only export,upload
python -m pipeline.run_pipeline --skip-gemini
```

Exit codes: `0` ok, `1` a step failed, `2` pre-flight failed, `3` dedupe aborted.
FATAL steps (turn the whole run red): `dedupe`, `export`, `upload`.
Export splits parts stably: each question keeps its file across runs
(hash of its DB id) so unchanged questions are not re-uploaded;
`--sequential-parts` restores the old 1..50 slicing.

## Hosted admin console (`index.html`)

`https://abhyasapp.github.io/DRIVETONEONTODRIVE/` is the pipeline homepage and the
duplicate-review console. Pages serves it as a static file, so there is no server:
the page talks to Neon's HTTPS SQL endpoint (`https://<project-host>/sql`) straight
from your browser.

- **Overview** — questions / files / chapters per level, reducible duplicates, last run.
- **Duplicates** — pending review groups, side-by-side compare, edit, merge, delete.
- **Health** — unanswered, thin-option, duplicate-content and tombstone counts.
- **Runs** — the six most recent workflow runs with their result and duration, plus a
  link to the full run log and the `pipeline-logs-<run_id>` artifact (7-day retention).

One-time setup per database: paste the Neon connection string, then **Install merge
functions**. That creates the audit tables (`dedup_deletions`, `dedupe_source_tombstones`,
`dedupe_review_candidates`) plus the `*_admin` SQL helpers. The string is kept in
`sessionStorage` only — never in git, localStorage or a URL — and is wiped by
**Disconnect** or closing the tab.

Safety built into `review/admin_functions.sql` (enforced in Postgres, not the page):

- a merge refuses pairs from different chapters, pairs where either answer is missing,
  and pairs whose correct answers disagree;
- the deleted row is snapshotted into `dedup_deletions` and the source location into
  `dedupe_source_tombstones`, so a later Drive sync of the same file resolves the old
  question to the keeper instead of resurrecting it;
- editing recomputes `identity_key` / `source_identity` with `review/identity.js`,
  a line-by-line port of `dedupe/content_identity.py`, so a corrected question is not
  imported as "new" on the next cron run.

Console checks (Node, no database server needed — PGlite is a WASM Postgres):

```bash
node tests/test_console_identity.mjs   # JS identity == Python identity (58 vectors)
node tests/test_admin_sql.mjs          # SQL helpers + every console query on real Postgres
```

Notes: use a Neon role scoped to this one database — anyone holding the string can read
and write. Non-Neon hosts (`localhost`, other providers) are only supported by the local
reviewer (`python review/dashboard.py`), which connects with `DATABASE_URL`.

Secrets: see `SECRETS.md`. Optional env: `DRIVE_PARENT_ID`, `GEMINI_STEP_TIMEOUT`.
