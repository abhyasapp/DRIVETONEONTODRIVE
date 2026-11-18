#!/usr/bin/env python3
"""
sync_reports.py
---------------
Keeps Neon's reported_questions in step with the Apps Script backend, in both
directions, and closes reports whose question has been corrected.

    1. PULL   syncreportssince -> upsert into reported_questions
              (incremental by watermark; a full re-read every 7 days catches
              status changes if the backend filters on reportedAt only).
              Rows with a change still waiting to be pushed are never
              overwritten by the pull.
    2. RECONCILE  abhyas_reconcile_reports(): any open report about a question
              (or a duplicate merged into it) corrected after the report was
              filed becomes resolved + corrected. This covers every correction
              path: the app, the console, admin_apply.
    3. PUSH   resolvereports -> tells the backend about every report resolved
              or reopened here (backend_dirty rows), so the app stops showing
              it as open. If the Apps Script has not been updated with
              apps_script/AdminReports.gs yet, the rows simply stay pending.

    python -m sync.sync_reports                # pull + reconcile + push
    python -m sync.sync_reports --push-only
    python -m sync.sync_reports --full         # ignore the watermark
    python -m sync.sync_reports --dry-run      # fetch + report, write nothing

Exit 0 = ok (a not-yet-deployed push endpoint is only a warning), 1 = backend
unreachable / misconfigured.
"""
import argparse
import json
import sys
import time

from dotenv import load_dotenv

from utils import backend
from utils.admin_sql import ensure_installed
from utils.common import to_ms
from utils.db import connect as db_connect

load_dotenv()

LIMIT = 1000
MAX_PAGES = 50
PUSH_BATCH = 200
FULL_REFRESH_MS = 7 * 24 * 3600 * 1000
K_SINCE = "last_read_reports_at"
K_FULL = "last_full_reports_at"


# ----------------------------------------------------------------- settings

def get_setting(cur, key):
    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
    row = cur.fetchone()
    return int(row[0]) if row and row[0] else 0


def set_setting(cur, key, value):
    cur.execute("""INSERT INTO settings (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                (key, str(value)))


# --------------------------------------------------------------------- pull

def _stamp(report):
    return max(to_ms(report.get("updatedAt")), to_ms(report.get("resolvedAt")),
               to_ms(report.get("reportedAt")))


def fetch_all(since_ms, limit=LIMIT, max_pages=MAX_PAGES):
    """Every report changed since since_ms. Returns (reports, watermark, complete)."""
    out, seen, cursor, server_now = [], set(), since_ms, 0
    for _ in range(max_pages):
        payload = backend.call("syncreportssince", since=cursor, limit=limit)
        batch = payload.get("reports") or []
        server_now = payload.get("now") or server_now
        fresh = 0
        for r in batch:
            key = (str(r.get("id")), _stamp(r))
            if key not in seen:
                seen.add(key)
                out.append(r)
                fresh += 1
        if len(batch) < limit:
            return out, int(server_now or 0), True
        newest = max((_stamp(r) for r in batch), default=0)
        if fresh == 0 or newest <= cursor:
            break
        cursor = max(newest - 1, cursor)
    newest_seen = max((_stamp(r) for r in out), default=since_ms)
    return out, max(newest_seen, since_ms), False


def _int(v):
    try:
        return None if v in (None, "") else int(v)
    except (TypeError, ValueError):
        return None


def _text(v):
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def to_row(r):
    return (
        str(r["id"]), r.get("uid"), _int(r.get("qid")), r.get("fileId"),
        _text(r.get("questionSnapshot")), _text(r.get("optionsSnapshot")),
        _int(r.get("correctIndex")), _int(r.get("chosenIndex")), r.get("reason"),
        r.get("note"), r.get("reportedBy"), r.get("reportedAt") or None,
        r.get("status") or "open", r.get("resolvedAt") or None, r.get("resolvedBy"),
        bool(r.get("corrected")), _text(r.get("correctedSnapshot")),
        r.get("section"), r.get("appVersion"),
    )


UPSERT = """
INSERT INTO reported_questions
  (id, uid, qid, file_id, question_snapshot, options_snapshot, correct_index,
   chosen_index, reason, note, reported_by, reported_at, status, resolved_at,
   resolved_by, corrected, corrected_snapshot, section, app_version,
   backend_dirty, backend_synced_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, false, now())
ON CONFLICT (id) DO UPDATE SET
  uid = EXCLUDED.uid, qid = EXCLUDED.qid, file_id = EXCLUDED.file_id,
  question_snapshot = EXCLUDED.question_snapshot,
  options_snapshot = EXCLUDED.options_snapshot,
  correct_index = EXCLUDED.correct_index, chosen_index = EXCLUDED.chosen_index,
  reason = EXCLUDED.reason, note = EXCLUDED.note,
  reported_by = EXCLUDED.reported_by, reported_at = EXCLUDED.reported_at,
  section = EXCLUDED.section, app_version = EXCLUDED.app_version,
  -- a change made here and not yet pushed outranks what the backend says
  status = CASE WHEN reported_questions.backend_dirty
                THEN reported_questions.status ELSE EXCLUDED.status END,
  resolved_at = CASE WHEN reported_questions.backend_dirty
                THEN reported_questions.resolved_at ELSE EXCLUDED.resolved_at END,
  resolved_by = CASE WHEN reported_questions.backend_dirty
                THEN reported_questions.resolved_by ELSE EXCLUDED.resolved_by END,
  corrected = CASE WHEN reported_questions.backend_dirty
                THEN reported_questions.corrected ELSE EXCLUDED.corrected END,
  corrected_snapshot = CASE WHEN reported_questions.backend_dirty
                THEN reported_questions.corrected_snapshot
                ELSE EXCLUDED.corrected_snapshot END,
  backend_synced_at = CASE WHEN reported_questions.backend_dirty
                THEN reported_questions.backend_synced_at ELSE now() END,
  migrated_at = now()
"""


def pull(cur, full=False):
    now_ms = int(time.time() * 1000)
    last_full = get_setting(cur, K_FULL)
    do_full = full or now_ms - last_full > FULL_REFRESH_MS
    since = 0 if do_full else get_setting(cur, K_SINCE)
    reports, mark, complete = fetch_all(since)
    return reports, mark, complete, do_full


def store(cur, reports, mark, complete, was_full):
    for r in reports:
        if r.get("id") is None:
            continue
        cur.execute(UPSERT, to_row(r))
    if mark:
        set_setting(cur, K_SINCE, mark)
    if was_full and complete:
        set_setting(cur, K_FULL, int(time.time() * 1000))


# --------------------------------------------------------------------- push

def pending(cur):
    cur.execute("""SELECT id, status, resolved_at, resolved_by, corrected,
                          corrected_snapshot
                     FROM reported_questions WHERE backend_dirty ORDER BY id""")
    items = []
    for rid, status, resolved_at, resolved_by, corrected, snapshot in cur.fetchall():
        items.append({
            "id": rid, "status": status or "open",
            "resolvedAt": resolved_at.isoformat() if resolved_at else None,
            "resolvedBy": resolved_by, "corrected": bool(corrected),
            "correctedSnapshot": snapshot,
        })
    return items


def push(cur, items):
    """Send pending changes. Returns (pushed_ids, status) where status is
    'ok' | 'nothing' | 'endpoint-missing'."""
    if not items:
        return [], "nothing"
    done = []
    for i in range(0, len(items), PUSH_BATCH):
        batch = items[i:i + PUSH_BATCH]
        try:
            payload = backend.call("resolvereports", items=batch)
        except backend.UnknownAction:
            return done, "endpoint-missing"
        acked = set(map(str, payload.get("updated") or [])) \
            | set(map(str, payload.get("missing") or []))
        ids = [b["id"] for b in batch if str(b["id"]) in acked]
        if ids:
            cur.execute("""UPDATE reported_questions
                              SET backend_dirty = false, backend_synced_at = now()
                            WHERE id = ANY(%s)""", (ids,))
            done.extend(ids)
        for m in payload.get("missing") or []:
            print(f"  [WARN] report {m} does not exist in the backend; "
                  f"dropping its pending change")
    return done, "ok"


# --------------------------------------------------------------------- main

def run(conn, do_pull=True, do_push=True, full=False, dry_run=False):
    stats = {"pulled": 0, "reconciled": 0, "pushed": 0, "pending": 0,
             "push_status": "skipped"}
    ensure_installed(conn)
    cur = conn.cursor()
    try:
        if do_pull:
            reports, mark, complete, was_full = pull(cur, full)
            stats["pulled"] = len(reports)
            print(f"Pulled {len(reports)} report(s)"
                  f"{' (full refresh)' if was_full else ''}"
                  f"{'' if complete else ' - page cap hit, watermark stops at newest read'}")
            if not dry_run:
                store(cur, reports, mark, complete, was_full)
                conn.commit()
        if not dry_run:
            cur.execute("SELECT abhyas_reconcile_reports(NULL, 'pipeline', NULL)")
            ids = cur.fetchone()[0] or []
            stats["reconciled"] = len(ids)
            conn.commit()
            print(f"Closed {len(ids)} report(s) whose question was corrected")
        if do_push:
            items = pending(cur)
            stats["pending"] = len(items)
            if dry_run:
                print(f"{len(items)} change(s) would be pushed")
            else:
                done, status = push(cur, items)
                conn.commit()
                stats["pushed"], stats["push_status"] = len(done), status
                if status == "endpoint-missing":
                    print(f"WARNING: backend has no 'resolvereports' action yet - "
                          f"{len(items)} change(s) stay pending. Deploy "
                          f"apps_script/AdminReports.gs.")
                else:
                    print(f"Pushed {len(done)} of {len(items)} pending change(s)")
    finally:
        cur.close()
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--push-only", action="store_true")
    ap.add_argument("--pull-only", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    conn = db_connect()
    try:
        run(conn, do_pull=not args.push_only, do_push=not args.pull_only,
            full=args.full, dry_run=args.dry_run)
    except backend.BackendError as e:
        print(f"Backend error: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
