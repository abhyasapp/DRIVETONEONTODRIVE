#!/usr/bin/env python3
"""
dedupe_exact.py  (v6.1 — full model, lock-safe schema)
======================================================
Same dedup model as v6. Lock-safe schema handling:

  * DDL runs with a patient lock_timeout (5 min).
  * Indexes built CREATE INDEX CONCURRENTLY.
  * Leftover invalid indexes are dropped and rebuilt.
  * release_lock() rolls back an aborted tx first.
  * Ordinary work still uses a 5s lock_timeout.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter

import psycopg2
from psycopg2.extras import Json, execute_values
from dotenv import load_dotenv

load_dotenv()


# ============================================================================
# CONFIG
# ============================================================================

CHUNK_SIZE        = 5000
SIG_PAGE          = 5000
DELETE_CHUNK      = 500
AUDIT_CHUNK       = 5000
ADVISORY_LOCK     = 0x64656475706571      # "dedupeq"

WORK_LOCK_TIMEOUT = "5s"
DDL_LOCK_TIMEOUT  = "5min"
STATEMENT_TIMEOUT = "30min"


# ============================================================================
# SIGNATURE
# ============================================================================

_WS_RE        = re.compile(r"\s+", re.UNICODE)
_LEAD_NUM_RE  = re.compile(r"^\s*\d+\s*[\.\)]\s+")
_INVISIBLE_RE = re.compile("[\u00ad\u200b\u200c\u200d\u200e\u200f\u2060\ufeff]")

_PUNCT_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2032": "'", "\u2035": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2033": '"', "\u2036": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2026": "...",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ",
})

_ASCII_OPERATORS = frozenset("+-*/=<>^%~")


def _keep_char(ch, prev, nxt):
    if ch.isalnum():
        return True
    if ch in _ASCII_OPERATORS:
        return True
    cat = unicodedata.category(ch)
    if cat == "Sm":
        return True
    if cat in ("Mn", "Mc"):
        return True
    if ch == "." and prev.isdigit() and nxt.isdigit():
        return True
    return False


def _canonical(s):
    if s is None:
        return ""
    s = str(s)
    s = unicodedata.normalize("NFC", s)
    s = _INVISIBLE_RE.sub("", s)
    s = s.translate(_PUNCT_MAP).lower()
    n = len(s)
    out = []
    for i, ch in enumerate(s):
        if ch.isspace():
            out.append(" ")
            continue
        prev = s[i - 1] if i > 0 else ""
        nxt  = s[i + 1] if i + 1 < n else ""
        if _keep_char(ch, prev, nxt):
            out.append(ch)
    return _WS_RE.sub(" ", "".join(out)).strip()


def signature(question_text, options):
    text = _LEAD_NUM_RE.sub("", question_text or "")
    parts = [_canonical(text)]
    for opt in (options or []):
        parts.append(_canonical(opt))
    payload = "".join(f"{len(p)}|{p}" for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ============================================================================
# DIFF CLASSIFIER
# ============================================================================

_STRIP_PUNCT_WS = re.compile(r"[^0-9a-z\u0900-\u097F]+", re.IGNORECASE)


def classify_diff(a, b):
    if a == b:
        return "identical"
    if a.lower() == b.lower():
        return "case-only"
    if _WS_RE.sub(" ", a.lower()).strip() == _WS_RE.sub(" ", b.lower()).strip():
        return "whitespace-only"
    if _STRIP_PUNCT_WS.sub("", a.lower()) == _STRIP_PUNCT_WS.sub("", b.lower()):
        return "punct/space-only"
    if _canonical(a) == _canonical(b):
        return "model-mergeable"
    return "bigger difference"


# ============================================================================
# HELPERS
# ============================================================================

def coerce_options(opts):
    if isinstance(opts, str):
        try:
            opts = json.loads(opts)
        except ValueError:
            opts = []
    if not isinstance(opts, list):
        opts = []
    return opts


def chunked(iterable, n):
    buf = []
    for x in iterable:
        buf.append(x)
        if len(buf) == n:
            yield buf
            buf = []
    if buf:
        yield buf


def progress(done, total, suffix=""):
    print(f"      {done}/{total} {suffix}\033[K", end="\r", flush=True)


def set_lock_timeout(cur, value):
    cur.execute(f"SET lock_timeout = '{value}'")


# ============================================================================
# SCHEMA
# ============================================================================

def _index_is_valid(cur, name):
    cur.execute("""
        SELECT i.indisvalid
        FROM pg_class c
        JOIN pg_index i ON i.indexrelid = c.oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = %s
          AND n.nspname = ANY (current_schemas(false))
    """, (name,))
    row = cur.fetchone()
    return bool(row) and bool(row[0])


def _ensure_index_concurrently(cur, conn, name, create_sql):
    if _index_is_valid(cur, name):
        return False
    cur.execute(f'DROP INDEX IF EXISTS "{name}"')
    conn.commit()
    prev_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        cur.execute(create_sql)
    finally:
        conn.autocommit = prev_autocommit
    return True


def ensure_schema(cur, conn):
    set_lock_timeout(cur, DDL_LOCK_TIMEOUT)

    ddl = [
        "ALTER TABLE questions ADD COLUMN IF NOT EXISTS strict_sig TEXT",
        """
        CREATE TABLE IF NOT EXISTS dedup_deletions (
            id             BIGSERIAL PRIMARY KEY,
            signature      TEXT        NOT NULL,
            canonical_id   BIGINT      NOT NULL,
            deleted_id     BIGINT      NOT NULL,
            question_text  TEXT,
            options        JSONB,
            correct_index  INTEGER,
            correct_text   TEXT,
            reason         TEXT        NOT NULL,
            deleted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "ALTER TABLE dedup_deletions ADD COLUMN IF NOT EXISTS correct_index INTEGER",
        "ALTER TABLE dedup_deletions ADD COLUMN IF NOT EXISTS correct_text  TEXT",
    ]
    for s in ddl:
        cur.execute(s)
    conn.commit()

    indexes = [
        ("idx_q_strict_sig",
         "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_q_strict_sig "
         "ON questions(strict_sig)"),
        ("idx_dd_sig",
         "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_dd_sig "
         "ON dedup_deletions(signature)"),
        ("idx_dd_canonical",
         "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_dd_canonical "
         "ON dedup_deletions(canonical_id)"),
        ("idx_dd_deleted",
         "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_dd_deleted "
         "ON dedup_deletions(deleted_id)"),
    ]
    built = []
    for name, sql in indexes:
        try:
            if _ensure_index_concurrently(cur, conn, name, sql):
                built.append(name)
        except psycopg2.Error as e:
            print(f"      (warning) CONCURRENTLY failed for {name}: {e}")
            print(f"      falling back to plain CREATE INDEX for {name}")
            cur.execute(f'DROP INDEX IF EXISTS "{name}"')
            conn.commit()
            cur.execute(sql.replace("CONCURRENTLY ", ""))
            conn.commit()
            built.append(name + " (plain)")

    if built:
        print(f"      indexes built: {', '.join(built)}")

    set_lock_timeout(cur, WORK_LOCK_TIMEOUT)


# ============================================================================
# ADVISORY LOCK
# ============================================================================

def acquire_lock(cur):
    cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK,))
    return cur.fetchone()[0]


def release_lock(cur):
    try:
        cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
    except psycopg2.Error:
        try:
            cur.connection.rollback()
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
        except psycopg2.Error as e:
            print(f"  (warning) advisory unlock failed: {e}", file=sys.stderr)


# ============================================================================
# CHILD FK DISCOVERY
# ============================================================================

def discover_child_fks(cur):
    cur.execute("""
        SELECT
            con.conrelid::regclass::text              AS child_table,
            quote_ident(att.attname)                  AS child_column,
            con.confdeltype                           AS action
        FROM pg_constraint con
        JOIN pg_attribute att
          ON att.attrelid = con.conrelid
         AND att.attnum   = con.conkey[1]
        WHERE con.contype     = 'f'
          AND con.confrelid   = 'questions'::regclass
          AND array_length(con.conkey, 1) = 1
        ORDER BY 1, 2
    """)
    out = []
    for table, col, action in cur.fetchall():
        if table.endswith("questions") and col == '"canonical_id"':
            continue
        if action in ("c", "n", "d"):
            continue
        out.append((table, col))
    return out


# ============================================================================
# PHASE 1 — SIGNATURES
# ============================================================================

def populate_signatures(cur, conn, refresh_all=False, quiet=False):
    where = "" if refresh_all else "WHERE strict_sig IS NULL"
    cur.execute(f"SELECT MIN(id), MAX(id), COUNT(*) FROM questions {where}")
    min_id, max_id, total = cur.fetchone()

    if not total:
        if not quiet:
            print("      no rows need signature computation")
        return 0

    if not quiet:
        print(f"      computing signatures for {total} question(s) ...")

    processed = 0
    started = time.time()
    batch_start = min_id

    while batch_start <= max_id:
        batch_end = batch_start + CHUNK_SIZE
        cur.execute(
            f"""
            SELECT id, question_text, options
            FROM questions
            {where + (" AND " if where else "WHERE ")} id >= %s AND id < %s
            """,
            (batch_start, batch_end),
        )
        rows = cur.fetchall()
        if rows:
            data = [(signature(t, coerce_options(o)), i) for i, t, o in rows]
            execute_values(
                cur,
                """
                UPDATE questions AS q
                SET strict_sig = v.sig
                FROM (VALUES %s) AS v(sig, id)
                WHERE q.id = v.id::bigint
                """,
                data,
                page_size=1000,
            )
            conn.commit()
            processed += len(rows)
            if not quiet:
                progress(processed, total)
        batch_start = batch_end

    if not quiet:
        elapsed = time.time() - started
        print(f"      {processed}/{total} signatures stored "
              f"({elapsed:.1f}s)\033[K")
    return processed


# ============================================================================
# PHASE 2 — GROUPS
# ============================================================================

def count_duplicate_groups(cur):
    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(cnt - 1), 0)
        FROM (
            SELECT COUNT(*) AS cnt
            FROM questions
            WHERE strict_sig IS NOT NULL
            GROUP BY strict_sig
            HAVING COUNT(*) > 1
        ) x
    """)
    n_groups, n_delete = cur.fetchone()
    return int(n_groups), int(n_delete)


def iter_duplicate_signature_pages(cur, page_size=SIG_PAGE):
    last = None
    while True:
        if last is None:
            cur.execute("""
                SELECT strict_sig FROM questions
                WHERE strict_sig IS NOT NULL
                GROUP BY strict_sig HAVING COUNT(*) > 1
                ORDER BY strict_sig LIMIT %s
            """, (page_size,))
        else:
            cur.execute("""
                SELECT strict_sig FROM questions
                WHERE strict_sig IS NOT NULL AND strict_sig > %s
                GROUP BY strict_sig HAVING COUNT(*) > 1
                ORDER BY strict_sig LIMIT %s
            """, (last, page_size))
        rows = cur.fetchall()
        if not rows:
            return
        page = [r[0] for r in rows]
        last = page[-1]
        yield page


# ============================================================================
# PHASE 3 — LOAD / PLAN / APPLY
# ============================================================================

def load_groups(cur, sigs):
    if not sigs:
        return {}
    cur.execute(
        """
        SELECT q.id, q.strict_sig, q.question_text, q.options,
               q.correct_index, q.correct_text,
               EXTRACT(EPOCH FROM f.last_synced_at) AS synced_at
        FROM questions q
        LEFT JOIN files f ON f.id = q.file_ref_id
        WHERE q.strict_sig = ANY(%s)
        ORDER BY q.strict_sig, q.id
        """,
        (sigs,),
    )
    groups = {}
    for qid, sig, txt, opts, ci, ct, synced_at in cur.fetchall():
        groups.setdefault(sig, []).append({
            "id": qid, "text": txt or "",
            "options": coerce_options(opts),
            "ci": ci, "ct": ct,
            "synced_at": float(synced_at) if synced_at is not None else 0.0,
        })
    return groups


def _keeper_key(x):
    return (
        -x["synced_at"],
        0 if (x["ci"] is not None or x["ct"] is not None) else 1,
        -len(x["text"]),
        x["id"],
    )


def pick_keeper(items):
    return min(items, key=_keeper_key)


def plan_deletions(groups):
    plans = []
    for sig, items in groups.items():
        keeper = pick_keeper(items)
        answers = {
            (x["ci"], x["ct"]) for x in items
            if x["ci"] is not None or x["ct"] is not None
        }
        reason = "conflict" if len(answers) > 1 else "exact"
        for it in items:
            if it["id"] == keeper["id"]:
                continue
            plans.append((sig, keeper["id"], it["id"], it["text"],
                          Json(it["options"]), it["ci"], it["ct"], reason))
    return plans


def apply_deletions(cur, conn, plans, child_fks):
    if not plans:
        return 0
    deleted_ids = [p[2] for p in plans]
    total_deleted = 0
    for chunk in chunked(deleted_ids, DELETE_CHUNK):
        cur.execute(
            "UPDATE questions SET canonical_id = NULL "
            "WHERE canonical_id = ANY(%s)", (chunk,))
        for table, col in child_fks:
            cur.execute(f"DELETE FROM {table} WHERE {col} = ANY(%s)", (chunk,))
        cur.execute("DELETE FROM questions WHERE id = ANY(%s)", (chunk,))
        total_deleted += len(chunk)
    execute_values(
        cur,
        """
        INSERT INTO dedup_deletions
            (signature, canonical_id, deleted_id,
             question_text, options, correct_index, correct_text, reason)
        VALUES %s
        """,
        plans,
        template="(%s, %s, %s, %s, %s, %s, %s, %s)",
        page_size=AUDIT_CHUNK,
    )
    conn.commit()
    return total_deleted


def orphan_check(cur, child_fks):
    out = []
    for table, col in child_fks:
        cur.execute(f"""
            SELECT COUNT(*) FROM {table} c
            LEFT JOIN questions q ON q.id = c.{col}
            WHERE c.{col} IS NOT NULL AND q.id IS NULL
        """)
        n = cur.fetchone()[0]
        if n:
            out.append((table, col, n))
    return out


# ============================================================================
# NEAR-DIFF
# ============================================================================

def _ensure_trgm(cur, conn):
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        conn.commit()
        return True
    except psycopg2.Error:
        conn.rollback()
        return False


def near_diff_report(cur, conn, limit=30, min_sim=0.90, show_pairs=True):
    print("      scanning with pg_trgm ...")
    if not _ensure_trgm(cur, conn):
        print("      pg_trgm unavailable — skipping near-diff report")
        return
    cur.execute("""
        SELECT a.id, b.id,
               similarity(a.question_text, b.question_text) AS sim,
               a.question_text, b.question_text,
               a.strict_sig IS NOT DISTINCT FROM b.strict_sig AS same_sig
        FROM questions a
        JOIN questions b
          ON a.id < b.id AND a.question_text %% b.question_text
        WHERE similarity(a.question_text, b.question_text) >= %s
        ORDER BY sim DESC LIMIT %s
    """, (min_sim, limit))
    rows = cur.fetchall()
    if not rows:
        print(f"      no pairs above similarity {min_sim}")
        return
    kinds = Counter()
    for _, _, _, ta, tb, _ in rows:
        kinds[classify_diff(ta or "", tb or "")] += 1
    print(f"\n      {len(rows)} near-diff pair(s) at similarity >= {min_sim}")
    print("      " + "-" * 58)
    for kind, n in kinds.most_common():
        print(f"        {kind:<22} {n}")
    print("      " + "-" * 58)
    if not show_pairs:
        return
    for id_a, id_b, sim, ta, tb, same_sig in rows:
        ta = (ta or "").replace("\n", " ")
        tb = (tb or "").replace("\n", " ")
        kind = classify_diff(ta, tb)
        marker = "MERGED" if same_sig else "kept-apart"
        print(f"\n        pair id={id_a} <-> id={id_b}  "
              f"sim={sim:.3f}  [{kind}]  [{marker}]")
        sm = difflib.SequenceMatcher(None, ta, tb)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            print(f"          A  ...{ta[max(0, i1 - 20):i2 + 20]!r}")
            print(f"          B  ...{tb[max(0, j1 - 20):j2 + 20]!r}")


# ============================================================================
# SAMPLES
# ============================================================================

def show_samples(cur, n=5):
    cur.execute("""
        SELECT strict_sig FROM questions
        WHERE strict_sig IS NOT NULL
        GROUP BY strict_sig HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC LIMIT %s
    """, (n,))
    sigs = [r[0] for r in cur.fetchall()]
    if not sigs:
        print("      (no duplicate groups to sample)")
        return
    groups = load_groups(cur, sigs)
    for i, sig in enumerate(sigs, 1):
        items = groups.get(sig, [])
        keeper = pick_keeper(items)
        answers = {(x["ci"], x["ct"]) for x in items
                   if x["ci"] is not None or x["ct"] is not None}
        flag = "  [CONFLICT]" if len(answers) > 1 else ""
        print(f"\n      -- Sample group {i}/{len(sigs)}  "
              f"— {len(items)} copies  — sig {sig[:12]}...{flag}")
        for it in sorted(items, key=_keeper_key):
            marker = "KEEP" if it["id"] == keeper["id"] else "DEL "
            txt = it["text"].replace("\n", " ")[:56]
            ans = str(it["ct"])[:22] if it["ct"] is not None else "-"
            syn = it["synced_at"]
            syn_s = f"sync={syn:.0f}" if syn else "sync=-"
            print(f"        {marker}  id={it['id']:>8}  {syn_s:<20}  "
                  f"ans={ans!r:<22}  {txt!r}")


# ============================================================================
# MAIN
# ============================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Syntax-lenient dedupe with near-diff preview.")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--preview-only", action="store_true")
    p.add_argument("--rehash-all", action="store_true")
    p.add_argument("--samples", type=int, default=5)
    p.add_argument("--near-limit", type=int, default=30)
    p.add_argument("--min-sim", type=float, default=0.90)
    p.add_argument("--no-near", action="store_true")
    p.add_argument("--quiet", "-q", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    mode = "APPLY (will delete)" if args.apply else "DRY-RUN"

    print("=" * 66)
    print("FULL-MODEL DEDUPE  (v6.1)")
    print("=" * 66)
    print(f"Mode: {mode}\n")

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")

        if not acquire_lock(cur):
            print("ERROR: another dedupe_exact.py is already running.")
            return

        try:
            print("[0/6] Schema")
            ensure_schema(cur, conn)
            print()

            if args.preview_only:
                print("[preview] Near-diff scan")
                near_diff_report(cur, conn, limit=args.near_limit,
                                 min_sim=args.min_sim, show_pairs=True)
                print("\nPreview only — nothing changed.")
                return

            print("[1/6] Signatures")
            populate_signatures(cur, conn, refresh_all=args.rehash_all,
                                quiet=args.quiet)
            print()

            print("[2/6] Exact-match groups")
            n_groups, n_delete = count_duplicate_groups(cur)
            print(f"      Duplicate groups:     {n_groups}")
            print(f"      Rows to delete:       {n_delete}\n")

            if not args.no_near:
                print("[3/6] Near-diff scan")
                near_diff_report(cur, conn, limit=args.near_limit,
                                 min_sim=args.min_sim,
                                 show_pairs=(not args.quiet))
                print()

            print("[4/6] FK map")
            children = discover_child_fks(cur)
            if children:
                for t, c in children:
                    print(f"      restrictive:  {t}.{c}")
            else:
                print("      all child FKs are cascade / set-null — "
                      "nothing to clean")
            print()

            if not args.apply:
                if n_groups == 0:
                    print("Nothing to delete.")
                    return
                print("[5/6] Sample groups")
                show_samples(cur, n=args.samples)
                print()
                print("-" * 66)
                print(f"DRY-RUN: would delete {n_delete} row(s) "
                      f"across {n_groups} group(s).")
                print("Run with --apply to actually delete.")
                print("-" * 66)
                return

            if n_groups == 0:
                print("Nothing to delete.")
                return

            print("[5/6] Applying deletions")
            total_deleted = 0
            total_conflicts = 0
            groups_done = 0
            started = time.time()

            for sig_page in iter_duplicate_signature_pages(cur, SIG_PAGE):
                groups_done += len(sig_page)
                groups = load_groups(cur, sig_page)
                plans = plan_deletions(groups)
                if not plans:
                    continue
                total_conflicts += sum(1 for p in plans if p[7] == "conflict")
                total_deleted += apply_deletions(cur, conn, plans, children)
                if not args.quiet:
                    progress(groups_done, n_groups,
                             f"groups, {total_deleted} row(s) deleted")

            elapsed = time.time() - started
            print(f"      {groups_done}/{n_groups} groups processed "
                  f"in {elapsed:.1f}s\033[K")

            cur.execute("SELECT COUNT(*) FROM questions")
            remaining = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM dedup_deletions")
            audited = cur.fetchone()[0]

            print()
            print("[6/6] Orphan check")
            orphans = orphan_check(cur, children)
            if orphans:
                print("      WARNING: orphan references remain:")
                for t, c, n in orphans:
                    print(f"        {t}.{c}: {n}")
            else:
                print("      no orphan FK references")

            print()
            print("=" * 66)
            print("RESULT")
            print("=" * 66)
            print(f"Groups processed:               {groups_done}")
            print(f"Rows deleted:                   {total_deleted}")
            print(f"Conflicting answers (audited):  {total_conflicts}")
            print(f"Questions remaining in DB:      {remaining}")
            print(f"Audit rows in dedup_deletions:  {audited}")
            print("=" * 66)

            if total_conflicts:
                print()
                print("To review conflicting deletions:")
                print("  SELECT canonical_id, deleted_id, question_text,")
                print("         correct_index, correct_text")
                print("  FROM dedup_deletions WHERE reason = 'conflict'")
                print("  ORDER BY deleted_at DESC;")

        finally:
            release_lock(cur)

    except KeyboardInterrupt:
        print("\n\nInterrupted — rolling back.")
        try:
            conn.rollback()
        except Exception:
            pass
    except psycopg2.Error as e:
        print(f"\nDATABASE ERROR: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        sys.exit(2)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()