# archive/ - one-off scripts (NOT used by the daily pipeline)

Kept only for reference / rebuilding the database from scratch.
They are not imported by anything and are safe to delete once you are sure
they have been applied to your database.

| File | Purpose |
|---|---|
| `l7_schema_v4.py` | Level 7 schema migration |
| `l7_load_hierarchy.py` | Load Level 7 hierarchy (needs `pip install chompjs` and the source JS) |
| `setup_new_levels.py`, `levels_data.py` | Seed Level 5 and GK structure |
| `normalize_books.py` | One-off book renaming |
| `fix_l5_ch2_books.py` | One-off file move (hardcoded Drive file IDs) |
| `dedupe_review.py` | v4 read-only CSV export of identity groups; superseded by
  `dedupe/dedupe_exact.py` (v9) and `review/duplicate_reviews.py` |
| `migrate_reports_to_neon.py` | One-time Supabase `question_report` -> `reported_questions` (applied) |
| `migrate_gas_reports_to_neon.py` | One-time GAS Q-reports sheet -> `reported_questions` (applied) |
| `migrate_gas_paste_to_neon.py` | One-time import of pasted GAS report rows (applied) |

Run from the repo root, e.g. `python archive/normalize_books.py`.
Note: they may need `PYTHONPATH=.` (`PYTHONPATH=. python archive/normalize_books.py`).
