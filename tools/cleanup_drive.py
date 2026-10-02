#!/usr/bin/env python3
"""
cleanup_drive.py
----------------
Finds orphan files under the Abhyas Drive root and optionally deletes
them. An "orphan" is any file whose Drive file_id is not referenced
in export_files.drive_file_id.

Covers:
  • long-named mixedqn files from earlier runs
  • files at chapter level (flat layout, before subtopic subfolders)
  • files inside bare-code folders (1.1/, 3.1/) that got replaced
  • anything else no longer pointed at by the DB

Usage:
    python cleanup_drive.py                   # report only
    python cleanup_drive.py --apply           # delete them
    python cleanup_drive.py --level level7    # restrict to one level
"""

import argparse
import os
import pickle
import sys

import psycopg2
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

DRIVE_PARENT   = "1_gAGILhdleeR6gBUfEFSKOVpa8x5wkCE"
TOKEN_FILE     = "oauth-token.pickle"
SCOPES         = ["https://www.googleapis.com/auth/drive"]


def load_creds():
    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError(f"{TOKEN_FILE} not found. Run upload_any_level.py once.")
    with open(TOKEN_FILE, "rb") as f:
        creds = pickle.load(f)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    if not creds or not creds.valid:
        raise RuntimeError("OAuth token invalid.")
    return creds


def drive():
    creds = load_creds()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_children(d, folder_id):
    items, token = [], None
    while True:
        r = d.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=200,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items.extend(r.get("files", []))
        token = r.get("nextPageToken")
        if not token:
            break
    return items


def is_folder(item):
    return item["mimeType"] == "application/vnd.google-apps.folder"


def walk(d, folder_id, prefix="", out=None):
    """Recursively collect every file and folder under folder_id."""
    if out is None:
        out = {"files": [], "folders": []}
    for it in list_children(d, folder_id):
        path = f"{prefix}/{it['name']}" if prefix else it["name"]
        if is_folder(it):
            out["folders"].append((path, it["id"]))
            walk(d, it["id"], path, out)
        else:
            out["files"].append((path, it["id"]))
    return out


def live_file_ids(cur, level_keys):
    """Set of Drive file_ids still referenced by export_files."""
    cur.execute("""
        SELECT ef.drive_file_id
        FROM export_files ef
        JOIN subtopics s ON s.id = ef.subtopic_id
        JOIN chapters c  ON c.id = s.chapter_id
        JOIN levels l    ON l.id = c.level_id
        WHERE l.level_key = ANY(%s)
    """, (level_keys,))
    return {r[0] for r in cur.fetchall() if r[0]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually delete. Default: report only.")
    p.add_argument("--level", default=None,
                   help="Restrict to one level (level5/level7/gk).")
    args = p.parse_args()

    d = drive()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    level_keys = [args.level] if args.level else ["level5", "level7", "gk"]
    live = live_file_ids(cur, level_keys)
    print(f"Live file IDs in DB (for {level_keys}): {len(live)}")

    print(f"\nWalking ABHYAS_AUTO root ...")
    tree = walk(d, DRIVE_PARENT)
    print(f"  folders: {len(tree['folders'])}")
    print(f"  files:   {len(tree['files'])}")

    orphans = [(path, fid) for path, fid in tree["files"] if fid not in live]
    print(f"\nOrphan files (not referenced in DB): {len(orphans)}")

    if not orphans:
        print("Nothing to clean.")
        cur.close(); conn.close()
        return

    # group by top-level folder for readability
    by_top = {}
    for path, fid in orphans:
        top = path.split("/")[0]
        by_top.setdefault(top, []).append((path, fid))

    for top in sorted(by_top):
        print(f"\n  ── {top} ({len(by_top[top])} file(s))")
        for path, fid in by_top[top][:20]:
            print(f"       {path}")
        if len(by_top[top]) > 20:
            print(f"       … and {len(by_top[top]) - 20} more")

    # empty folders
    def has_live_child(folder_id):
        for it in list_children(d, folder_id):
            if not is_folder(it):
                return True
            if has_live_child(it["id"]):
                return True
        return False

    empty = []
    for path, fid in tree["folders"]:
        if not has_live_child(fid):
            empty.append((path, fid))

    if empty:
        print(f"\nEmpty folders: {len(empty)}")
        for path, fid in empty[:20]:
            print(f"  {path}")
        if len(empty) > 20:
            print(f"  … and {len(empty) - 20} more")

    if not args.apply:
        print()
        print("=" * 60)
        print(f"DRY-RUN: {len(orphans)} file(s) and {len(empty)} folder(s)")
        print("Re-run with --apply to delete.")
        print("=" * 60)
        cur.close(); conn.close()
        return

    # ── APPLY ────────────────────────────────────────────────────────
    print()
    print(f"Deleting {len(orphans)} file(s) ...")
    deleted = 0
    for path, fid in orphans:
        try:
            d.files().delete(fileId=fid, supportsAllDrives=True).execute()
            deleted += 1
            if deleted % 25 == 0:
                print(f"  {deleted}/{len(orphans)}")
        except Exception as e:
            print(f"  FAILED {path}: {e}")

    print(f"\nDeleting {len(empty)} empty folder(s) ...")
    folders_deleted = 0
    # deepest first so children go before parents
    for path, fid in sorted(empty, key=lambda x: -x[0].count("/")):
        try:
            d.files().delete(fileId=fid, supportsAllDrives=True).execute()
            folders_deleted += 1
        except Exception as e:
            print(f"  FAILED {path}: {e}")

    print()
    print("=" * 60)
    print(f"Deleted: {deleted} file(s), {folders_deleted} folder(s)")
    print("=" * 60)

    cur.close(); conn.close()


if __name__ == "__main__":
    main()