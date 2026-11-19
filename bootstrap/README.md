# bootstrap/ - one-time scripts for building a database from scratch

Not used by the daily pipeline. Run them (in this order) only when you set up a
**new** database; an existing one already has everything they create.

| Order | File | Purpose |
|---|---|---|
| 1 | `l7_schema_v4.py` | Level 7 schema |
| 2 | `levels_data.py`, `setup_new_levels.py` | Seed the Level 5 and GK structure |
| 3 | `l7_load_hierarchy.py` | Load the Level 7 hierarchy (needs `pip install chompjs` and the source JS) |

Run from the repo root with `PYTHONPATH=.`, e.g.
`PYTHONPATH=. python bootstrap/setup_new_levels.py`.

Schema changes after that are versioned SQL files in `migrations/`, applied by
`python -m utils.migrate` (the daily pipeline runs it first).

The earlier one-off data fixes and report migrations (book renames, the L5
chapter-2 file move, the three GAS/Supabase report imports, the v4 CSV dedupe
exporter) were applied long ago and have been removed; they remain in git
history if you ever need them.
