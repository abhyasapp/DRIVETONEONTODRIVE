#!/usr/bin/env python3
"""
audit_reports.py
----------------
Connect Supabase question_reports to Neon: fetch reports (with qid) through
the `syncreportssince` endpoint and join them with the Neon `questions` table
to show dedupe/canonical/correction status per report.

Usage:
    python audit_reports.py            # print audit table
    python audit_reports.py --since 0  # all reports
"""
import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

from utils.db import connect as db_connect

load_dotenv()

APPS_URL = os.getenv("ABHYAS_APPS_SCRIPT_URL")
SYNC_KEY = os.getenv("ABHYAS_SYNC_KEY")
FETCH_TIMEOUT = 90


def fetch_reports(since_ms=0, limit=5000):
    if not APPS_URL or not SYNC_KEY:
        raise RuntimeError("ABHYAS_APPS_SCRIPT_URL / ABHYAS_SYNC_KEY not set")
    body = json.dumps({"action": "syncreportssince", "key": SYNC_KEY,
                       "since": since_ms, "limit": limit})
    r = requests.post(APPS_URL, data=body,
                      headers={"Content-Type": "text/plain"},
                      timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or "fetch failed")
    return payload.get("reports", [])


def neon_lookup(cur, qids):
    if not qids:
        return {}
    cur.execute(
        """SELECT q.id, q.canonical_id, q.corrected_at, q.source_question_key,
                  q.identity_key, f.file_id
           FROM questions q LEFT JOIN files f ON f.id = q.file_ref_id
           WHERE q.id = ANY(%s)""",
        (list(qids),),
    )
    out = {}
    for row in cur.fetchall():
        qid, canon, corrected, src_key, ident, file_id = row
        out[qid] = {
            "canonical_id": canon,
            "corrected_at": corrected,
            "source_question_key": src_key,
            "identity_key": ident,
            "file_id": file_id,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=0)
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()

    reports = fetch_reports(args.since, args.limit)
    print(f"Fetched {len(reports)} reports")

    conn = db_connect()
    cur = conn.cursor()
    qids = [r["qid"] for r in reports if r.get("qid") is not None]
    lookup = neon_lookup(cur, qids)

    print(f"{'report_id':<10}{'qid':<10}{'status':<12}{'neon':<8}{'canonical':<12}{'corrected':<12}{'reason'}")
    print("-" * 80)
    missing = 0
    for r in reports:
        qid = r.get("qid")
        info = lookup.get(qid) if qid is not None else None
        neon = "yes" if info else ("n/a" if qid is None else "MISSING")
        if qid is not None and info is None:
            missing += 1
        canon = str(info["canonical_id"]) if info and info["canonical_id"] is not None else "-"
        corrected = str(info["corrected_at"])[:10] if info and info["corrected_at"] else "-"
        print(f"{str(r['id']):<10}{str(qid):<10}{str(r.get('status')):<12}{neon:<8}{canon:<12}{corrected:<12}{r.get('reason','')}")
    print("-" * 80)
    print(f"Reports with qid not found in Neon: {missing}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
EOF