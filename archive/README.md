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

Run from the repo root, e.g. `python archive/normalize_books.py`.
Note: they may need `PYTHONPATH=.` (`PYTHONPATH=. python archive/normalize_books.py`).
