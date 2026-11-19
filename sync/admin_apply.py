#!/usr/bin/env python3
"""
admin_apply.py
--------------
Apply admin corrections to Neon directly, addressed by question id - no
round trip through the corrections backend. Accepts the numeric questions.id
or the public qn id the export publishes (e.g. 50011.110001).

It calls the same SQL function the console uses (abhyas_apply_correction), so a
correction made here behaves exactly like one made there: the question row is
rewritten, a source alias is stored so the next Drive sync cannot restore the
old text, matching reports are closed, and a before/after audit row is kept so
the change can be undone from the console. Ids that were merged away are
followed to the surviving question.

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

Open reports about the question (and about questions merged into it) are closed
in the same transaction and queued for push-back to the backend
(sync.sync_reports --push-only). An identical resubmission is a no-op.

Exit code 0 = every item applied (or was a no-op), 1 = any item rejected.
"""
import argparse
import json
import os
import sys

from dedupe.content_identity import (
    compute_quality_score, identity, source_location_key,
)
from sync.apply_corrections import validate_after_answer
from utils.admin_sql import ensure_installed, is_installed
from utils.db import connect as db_connect


class NoMatch(LookupError):
    pass


def _clean(text):
    return " ".join(str(text or "").split())


def apply_item(cur, item):
    """Apply one correction through abhyas_apply_correction (same function the
    console calls). Returns (ok, note, report_ids)."""
    ref = str(item.get("qn") or item.get("qid") or "").strip()
    cur.execute("SELECT question_id, hops FROM abhyas_resolve_question(%s)", (ref,))
    hit = cur.fetchone()
    if not hit:
        raise NoMatch(f"no live question matches {ref!r}")
    qid, hops = hit
    cur.execute("""
        SELECT q.question_text, q.options, q.correct_index, q.explanation,
               q.question_number, q.source_identity, b.book_name, f.subtopic
          FROM questions q
          LEFT JOIN files f ON f.id = q.file_ref_id
          LEFT JOIN books b ON b.id = f.book_id
         WHERE q.id = %s""", (qid,))
    (old_text, old_opts, old_idx, old_expl, qnum, src_ident, book, subtopic) = cur.fetchone()
    old_opts = old_opts or []

    question = str(item["question"]).strip()
    options = [str(o).strip() for o in item["options"]]
    explanation = item.get("explanation") or ""
    index = item.get("correctIndex")

    if (_clean(old_text) == _clean(question) and old_opts == options
            and old_idx == index and _clean(old_expl) == _clean(explanation)):
        return True, f"no change qid={qid}", []

    source_identity = src_ident or identity(old_text, old_opts)
    location = source_location_key(book, subtopic) if book else None
    cur.execute(
        "SELECT abhyas_apply_correction(%s, %s, %s::jsonb, %s, %s, %s, %s, %s::real,"
        " %s, %s, %s, %s, %s)",
        (str(qid), question, json.dumps(options), index, explanation,
         identity(question, options), source_identity,
         compute_quality_score(explanation, options, len(question)),
         location, str(qnum or ""), item.get("note") or "admin_apply",
         "admin-cli", True))
    result = cur.fetchone()[0]
    merged = f" (via merged id, {hops} hop{'s' if hops != 1 else ''})" if hops else ""
    return (True, f"applied qid={qid} audit={result.get('audit_id')}{merged}",
            list(result.get("reports_resolved") or []))


def run(items, apply=False, conn=None, info=None):
    own = conn is None
    conn = conn or db_connect()
    if apply:
        ensure_installed(conn)       # also creates the audit / alias tables
    cur = conn.cursor()
    ready = is_installed(cur)
    results, closed = [], []
    for n, item in enumerate(items, 1):
        ref = str(item.get("qn") or item.get("qid") or "").strip()
        problem = None
        if not ref:
            problem = "missing qn"
        elif not str(item.get("question") or "").strip():
            problem = "missing question text"
        else:
            problem = validate_after_answer(item.get("options"), item.get("correctIndex"))
        if problem:
            results.append((ref or f"#{n}", False, problem))
            continue
        if not ready:
            results.append((ref, True, "validated (SQL functions install on --apply)"))
            continue
        cur.execute("SAVEPOINT admin_item")
        try:
            ok, note, reports = apply_item(cur, item)
            cur.execute("RELEASE SAVEPOINT admin_item")
            results.append((ref, ok, note))
            closed.extend(reports)
        except Exception as exc:       # one bad item must not sink the batch
            cur.execute("ROLLBACK TO SAVEPOINT admin_item")
            results.append((ref, False, (str(exc).strip().splitlines() or [type(exc).__name__])[0]))
    if info is not None:
        info["reports_resolved"] = closed
    if apply:
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
