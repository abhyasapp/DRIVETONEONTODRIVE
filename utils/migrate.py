#!/usr/bin/env python3
"""
migrate.py
----------
Versioned schema migrations. Every file migrations/NNN_name.sql is applied once,
in order, and recorded in `schema_migrations` (version, checksum, applied_at).

    python -m utils.migrate            # apply what is pending
    python -m utils.migrate --status   # show applied / pending
    python -m utils.migrate --dry-run  # list what would run

Rules for migration files:
  * name them 001_short_name.sql, 002_..., never renumber or reuse a number;
  * write them so running twice is harmless (IF NOT EXISTS ...), because a DB
    that was patched by hand before this runner existed will run them again;
  * do not add BEGIN/COMMIT - each file already runs in its own transaction
    together with its schema_migrations row, so a failure leaves no trace;
  * never edit a file once it has been applied; add a new one. An edited file
    is reported (checksum differs) but not re-run.

The pipeline runs this as its first step. A session-level advisory lock makes
two concurrent runs (daily + manual) apply each file only once.
"""
import argparse
import hashlib
import os
import re
import sys

from dotenv import load_dotenv

from utils.db import connect as db_connect

load_dotenv()

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "migrations")
NAME_RE = re.compile(r"^(\d{3,})_[A-Za-z0-9_]+\.sql$")
LOCK_KEY = 727_001          # arbitrary, unique to this runner


def discover(directory=None):
    directory = directory or DEFAULT_DIR
    found = []
    for name in sorted(os.listdir(directory)):
        m = NAME_RE.match(name)
        if m:
            found.append((m.group(1), os.path.join(directory, name)))
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise SystemExit(f"duplicate migration number in {directory}")
    return found


def _checksum(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version    text PRIMARY KEY,
        name       text NOT NULL,
        checksum   text NOT NULL,
        applied_at timestamptz NOT NULL DEFAULT now())""")


def status(conn, directory=None):
    """Return (applied, pending, changed) where changed lists versions whose
    file no longer matches the checksum recorded when it was applied."""
    cur = conn.cursor()
    _ensure_table(cur)
    cur.execute("SELECT version, checksum FROM schema_migrations")
    done = dict(cur.fetchall())
    conn.commit()
    cur.close()
    found = discover(directory)
    applied = [v for v, _ in found if v in done]
    pending = [(v, p) for v, p in found if v not in done]
    changed = [v for v, p in found if v in done and done[v] != _checksum(p)]
    return applied, pending, changed


def run(conn, directory=None, dry_run=False):
    """Apply pending migrations; returns the list of versions applied."""
    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        applied, pending, changed = status(conn, directory)
        for v in changed:
            print(f"  [WARN] migration {v} was edited after it was applied "
                  f"(not re-run); add a new migration instead")
        if dry_run:
            for v, p in pending:
                print(f"  would apply {os.path.basename(p)}")
            return [v for v, _ in pending]
        done = []
        for version, path in pending:
            with open(path, "r", encoding="utf-8") as fh:
                sql = fh.read()
            try:
                cur.execute(sql)
                cur.execute("""INSERT INTO schema_migrations (version, name, checksum)
                               VALUES (%s, %s, %s)""",
                            (version, os.path.basename(path), _checksum(path)))
                conn.commit()
            except Exception:
                conn.rollback()
                print(f"  migration {os.path.basename(path)} FAILED; rolled back")
                raise
            print(f"  applied {os.path.basename(path)}")
            done.append(version)
        if not done:
            print(f"  schema up to date ({len(applied)} migration(s) recorded)")
        return done
    finally:
        cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        conn.commit()
        cur.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    conn = db_connect()
    try:
        if args.status:
            applied, pending, changed = status(conn)
            print(f"applied: {', '.join(applied) or '-'}")
            print(f"pending: {', '.join(v for v, _ in pending) or '-'}")
            if changed:
                print(f"edited after applying: {', '.join(changed)}")
            return
        run(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
