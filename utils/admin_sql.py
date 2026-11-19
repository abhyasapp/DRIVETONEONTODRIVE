"""Install review/admin_functions.sql into Neon when it is missing.

The console installs these functions from the browser; headless paths (the
admin-corrections workflow, the daily reports step) call ensure_installed so
nothing depends on someone having clicked the button first.
"""
import os

SQL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "review", "admin_functions.sql")
REQUIRED = ("abhyas_apply_correction", "abhyas_revert_correction",
            "abhyas_reconcile_reports", "abhyas_resolve_question")


def is_installed(cur):
    cur.execute("SELECT " + " AND ".join(
        f"to_regproc('{name}') IS NOT NULL" for name in REQUIRED))
    return bool(cur.fetchone()[0])


def ensure_installed(conn):
    cur = conn.cursor()
    try:
        if is_installed(cur):
            return False
        with open(SQL_PATH, "r", encoding="utf-8") as fh:
            cur.execute(fh.read())     # no params: '%' in RAISE text is literal
        conn.commit()
        return True
    finally:
        cur.close()
