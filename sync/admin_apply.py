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

Exit code 0 = every item applied (or was a no-op), 1 = any item rejected.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

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


def run(items, apply=False, conn=None):
    own = conn is None
    conn = conn or db_connect()
    cur = conn.cursor()
    ensure_schema(cur, conn)
    results = []
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
            results.append((ref, str(why2).startswith("applied"), f"{why2} qid={qid}"))
    if apply:
        conn.commit()
    else:
        conn.rollback()
    cur.close()
    if own:
        conn.close()
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="JSON file with a list of corrections")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args(argv)
    with open(args.file, "r", encoding="utf-8") as fh:
        items = json.load(fh)
    if not isinstance(items, list):
        sys.exit("input must be a JSON list")
    results = run(items, apply=args.apply)
    bad = 0
    for ref, ok, note in results:
        print(f"  {'OK ' if ok else 'ERR'} {ref}: {note}")
        bad += 0 if ok else 1
    print(f"{len(results) - bad} ok, {bad} rejected"
          f"{'' if args.apply else '  (dry run - nothing written)'}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
