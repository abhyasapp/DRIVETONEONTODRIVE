#!/usr/bin/env python3
"""
admin_apply.py
--------------
Apply admin corrections to Neon directly, addressed by question id - no
round trip through the corrections backend. Accepts the numeric questions.id
or the public qn id the export publishes (e.g. 50011.110001).

It reuses sync.apply_corrections.process_correction, so a correction made here
behaves exactly like one from the app: the question row is rewritten, a source
alias is stored so the next Drive sync cannot restore the old text, and the
event lands in applied_corrections. Ids that were merged away are followed to
the surviving question.

Input (JSON list):
    [{"qn": "50011.110001",
      "question": "Corrected question text?",
      "options": ["A", "B", "C", "D"],
      "correctIndex": 2,
      "explanation": "optional",
      "note": "optional"}]

    python -m sync.admin_apply fixes.json            # dry run (rolls back)
    python -m sync.admin_apply fixes.json --apply    # write
    python -m sync.admin_apply --env ADMIN_CORRECTIONS_JSON --apply --out r.json

After a correction is applied, open reports about that question (and about
questions merged into it) are closed in the same transaction and queued for
push-back to the Apps Script backend (sync.sync_reports --push-only).

Exit code 0 = every item applied (or was a no-op), 1 = any item rejected.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from utils.admin_sql import ensure_installed
from utils.db import connect as db_connect
from sync.apply_corrections import (
    ensure_schema, find_by_qid, process_correction, validate_after_answer,
)


def build_correction(row, item, stamp):
    qid, old_text, old_opts, _file_ref, _skey, _corr_at, file_id, _chapter = row
    return {
        "uid": f"admin-cli:{qid}",
        "correctedAt": stamp,
        "qid": str(qid),
        "fileId": file_id,
        "before": {"question": old_text, "options": old_opts or []},
        "after": {
            "question": item["question"],
            "options": item["options"],
            "correctIndex": item.get("correctIndex"),
            "explanation": item.get("explanation") or "",
        },
        "note": item.get("note") or "admin_apply",
    }


def run(items, apply=False, conn=None, info=None):
    own = conn is None
    conn = conn or db_connect()
    if apply:
        ensure_installed(conn)       # reconcile function must exist
    cur = conn.cursor()
    ensure_schema(cur, conn)
    results = []
    applied_qids = []
    for n, item in enumerate(items, 1):
        ref = str(item.get("qn") or item.get("qid") or "").strip()
        problem = None
        if not ref:
            problem = "missing qn"
        elif not str(item.get("question") or "").strip():
            problem = "missing question text"
        elif validate_after_answer(item.get("options"), item.get("correctIndex")):
            problem = validate_after_answer(item.get("options"), item.get("correctIndex"))
        if problem:
            results.append((ref or f"#{n}", False, problem))
            continue
        row, why = find_by_qid(cur, ref)
        if row is None:
            results.append((ref, False, why))
            continue
        # unique per call so two fixes of the same question both apply
        stamp = datetime.now(timezone.utc).isoformat()
        qid, why2, err = process_correction(cur, build_correction(row, item, stamp))
        if err:
            results.append((ref, False, err))
        else:
            ok = str(why2).startswith("applied")
            results.append((ref, ok, f"{why2} qid={qid}"))
            if ok:
                applied_qids.append(int(qid))
    if apply:
        resolved = []
        if applied_qids:
            cur.execute("SELECT abhyas_reconcile_reports(%s::bigint[], 'admin-cli', NULL)",
                        (applied_qids,))
            resolved = cur.fetchone()[0] or []
        if info is not None:
            info["reports_resolved"] = list(resolved)
        conn.commit()
    else:
        conn.rollback()
    cur.close()
    if own:
        conn.close()
    return results


def load_items(args):
    if args.env:
        raw = os.environ.get(args.env, "")
        if not raw.strip():
            sys.exit(f"environment variable {args.env} is empty")
        data = json.loads(raw)
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        sys.exit("give a JSON file or --env VARNAME")
    if not isinstance(data, list):
        sys.exit("input must be a JSON list")
    if len(data) > 200:
        sys.exit("refusing more than 200 corrections in one batch")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="JSON file with a list of corrections")
    ap.add_argument("--env", help="read the JSON list from this environment variable")
    ap.add_argument("--out", help="write a JSON result summary here")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args(argv)
    items = load_items(args)
    info = {}
    results = run(items, apply=args.apply, info=info)
    bad = 0
    for ref, ok, note in results:
        print(f"  {'OK ' if ok else 'ERR'} {ref}: {note}")
        bad += 0 if ok else 1
    print(f"{len(results) - bad} ok, {bad} rejected"
          f"{'' if args.apply else '  (dry run - nothing written)'}")
    if info.get("reports_resolved"):
        print(f"reports closed: {', '.join(map(str, info['reports_resolved']))}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"applied": args.apply, "ok": len(results) - bad, "rejected": bad,
                       "results": [{"qn": r, "ok": o, "note": n} for r, o, n in results],
                       "reportsResolved": info.get("reports_resolved", [])}, fh, indent=2)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
