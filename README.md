# Abhyas daily pipeline

Drive -> Neon (sync) -> corrections -> dedupe -> keyword + Gemini classify ->
export JSON -> upload to Drive -> chapters-data.js -> upload.

```
.github/workflows/daily_pipeline.yml   CI schedule + manual run
pipeline/run_pipeline.py               orchestrator (retries, timeouts, exit codes)
sync/        sync_any_level, apply_corrections
dedupe/      dedupe_exact (pipeline), dedupe_review (dry-run report)
classify/    classify_any_level (keywords), classify_with_gemini (LLM)
export/      export_any_level, generate_chapters_data_all
upload/      upload_any_level, upload_chapters_data
utils/       db.py (Neon retry), drive_auth.py (OAuth), extract_token.py
config/      settings.py (Drive parent, paths) + keyword JSONs
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

Secrets: see `SECRETS.md`. Optional env: `DRIVE_PARENT_ID`, `GEMINI_STEP_TIMEOUT`.
