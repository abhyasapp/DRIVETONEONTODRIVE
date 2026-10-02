#!/usr/bin/env python3
"""
upload_any_level.py  (v3 — cloud-ready)
---------------------------------------
Uploads abhyas_export/ to Google Drive, preserving the subtopic
sub-folder structure and file IDs across runs.

Mirrors:
    <level label>/<Ch N - Chapter>/<subtopic code>/<file>.json

Authentication, in priority order:
  1. GDRIVE_OAUTH_TOKEN env var (JSON string) — used by GitHub Actions
  2. oauth-token.pickle                        — used locally after first auth
  3. Interactive browser OAuth                 — first-time local setup only

Handles:
  • subtopic subfolders, created once per (chapter, code)
  • legacy flat files still sitting in the chapter folder
  • sidecar .meta.json for code / part / source_label
  • folder-level fallback so a failed DB write never causes duplicates
  • source_label = "" for classified files (Postgres PK safety)
  • prune pass: after scanning the local tree, any export_files row for
    this level whose (subtopic_id, source_label, part_number) is not
    present locally is deleted, and its Drive file is trashed.

Usage:
    python upload_any_level.py all
    python upload_any_level.py level5
    python upload_any_level.py all --dry-run
    python upload_any_level.py all --workers 6
    python upload_any_level.py all --no-prune     # old behaviour
    python upload_any_level.py all --prune-only   # only prune
"""

import argparse
import hashlib
import json
import os
import pickle
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

load_dotenv()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OAUTH_CREDENTIALS_FILE = "oauth-credentials.json"
TOKEN_FILE             = "oauth-token.pickle"
SCOPES                 = ["https://www.googleapis.com/auth/drive"]

LOCAL_ROOT   = "abhyas_export"
DRIVE_PARENT = "1_gAGILhdleeR6gBUfEFSKOVpa8x5wkCE"

DEFAULT_WORKERS = 5
HASH_WORKERS    = 8

# Accepts:
#   "1.1 General.json" / "1.1 General (part 3).json"
#   "mixedqn.json"     / "mixedqn (2).json"
_FILENAME_RE = re.compile(
    r"^(?P<code>[0-9]+(?:\.[0-9]+)*|mixedqn)"
    r"(?:\s+(?P<title>\S.*?))?"
    r"(?:\s*\(part\s+(?P<part>\d+)\))?"
    r"(?:\s*\((?P<part2>\d+)\))?"
    r"(?:\.json)$",
    re.IGNORECASE,
)


# ════════════════════════════════════════════════════════════════════════
# OAuth / Drive client
# ════════════════════════════════════════════════════════════════════════

def _load_creds_from_env():
    """Try GDRIVE_OAUTH_TOKEN env var (JSON string). Returns None if absent/invalid."""
    raw = os.environ.get("GDRIVE_OAUTH_TOKEN")
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except Exception as e:
        print(f"  [auth] GDRIVE_OAUTH_TOKEN is not valid JSON: {e}")
        return None
    try:
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    except Exception as e:
        print(f"  [auth] GDRIVE_OAUTH_TOKEN could not build Credentials: {e}")
        return None
    return creds


def _load_creds_from_pickle():
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_creds(creds):
    try:
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    except Exception:
        # Not fatal — env-var auth doesn't need the pickle
        pass


def _interactive_auth():
    flow = InstalledAppFlow.from_client_secrets_file(
        OAUTH_CREDENTIALS_FILE, SCOPES
    )
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_creds(creds)
    return creds


def _refresh_if_needed(creds, save_to_pickle=False):
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            if save_to_pickle:
                _save_creds(creds)
        except Exception as e:
            print(f"  [auth] token refresh failed: {e}")
            return None
    return creds


def drive_client():
    # 1. env var (GitHub Actions / cloud)
    creds = _load_creds_from_env()
    if creds and not creds.valid:
        creds = _refresh_if_needed(creds, save_to_pickle=False)
    if creds and creds.valid:
        print("  [auth] using GDRIVE_OAUTH_TOKEN")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    # 2. local pickle
    creds = _load_creds_from_pickle()
    if creds and not creds.valid:
        creds = _refresh_if_needed(creds, save_to_pickle=True)
    if creds and creds.valid:
        print("  [auth] using oauth-token.pickle")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    # 3. interactive (local only)
    if not sys.stdin.isatty():
        raise RuntimeError(
            "No valid OAuth credentials and stdin is not a terminal. "
            "Set GDRIVE_OAUTH_TOKEN (cloud) or run this script "
            "interactively once to create oauth-token.pickle (local)."
        )
    print("  [auth] interactive OAuth required — opening browser ...")
    creds = _interactive_auth()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


_drive_cache = {}
_drive_lock  = threading.Lock()


def thread_drive():
    tid = threading.get_ident()
    with _drive_lock:
        if tid not in _drive_cache:
            _drive_cache[tid] = drive_client()
        return _drive_cache[tid]


# ════════════════════════════════════════════════════════════════════════
# Retry
# ════════════════════════════════════════════════════════════════════════

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _retry(fn, max_attempts=5, base=1.0, cap=30.0):
    last = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            last = e
            if status not in RETRYABLE_STATUSES:
                raise
            if attempt == max_attempts - 1:
                raise
            delay = min(base * (2 ** attempt) + random.random(), cap)
            time.sleep(delay)
    if last:
        raise last


# ════════════════════════════════════════════════════════════════════════
# Drive folder helpers
# ════════════════════════════════════════════════════════════════════════

_folder_cache = {}
_folder_cache_lock = threading.Lock()
_key_locks = {}
_key_locks_lock = threading.Lock()


def _get_key_lock(key):
    with _key_locks_lock:
        if key not in _key_locks:
            _key_locks[key] = threading.Lock()
        return _key_locks[key]


def find_or_create_folder(drive, name, parent_id):
    key = (name, parent_id)
    with _folder_cache_lock:
        if key in _folder_cache:
            return _folder_cache[key]
    with _get_key_lock(key):
        with _folder_cache_lock:
            if key in _folder_cache:
                return _folder_cache[key]
        escaped = name.replace("'", "\\'")
        q = (f"'{parent_id}' in parents and name = '{escaped}' "
             f"and mimeType = 'application/vnd.google-apps.folder' "
             f"and trashed = false")

        def _search():
            return drive.files().list(
                q=q, fields="files(id, name)", pageSize=10,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
        res = _retry(_search)
        if res.get("files"):
            fid = res["files"][0]["id"]
        else:
            meta = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }

            def _create():
                return drive.files().create(
                    body=meta, fields="id", supportsAllDrives=True
                ).execute()
            fid = _retry(_create)["id"]
        with _folder_cache_lock:
            _folder_cache[key] = fid
        return fid


def list_folder_files(drive, folder_id):
    """{filename: file_id} of every non-folder child of folder_id."""
    out = {}
    token = None
    while True:
        def _call():
            return drive.files().list(
                q=(f"'{folder_id}' in parents and trashed = false "
                   f"and mimeType != 'application/vnd.google-apps.folder'"),
                fields="nextPageToken, files(id, name)",
                pageSize=200,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        res = _retry(_call)
        for f in res.get("files", []):
            out[f["name"]] = f["id"]
        token = res.get("nextPageToken")
        if not token:
            break
    return out


def trash_drive_file(drive, file_id):
    """Move a Drive file to trash. Treats 404 as success."""
    if not file_id:
        return

    def _do():
        return drive.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
        ).execute()

    try:
        _retry(_do)
    except HttpError as e:
        if getattr(e.resp, "status", None) == 404:
            return
        raise


# ════════════════════════════════════════════════════════════════════════
# Local file parsing
# ════════════════════════════════════════════════════════════════════════

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_filename(fname):
    m = _FILENAME_RE.match(fname)
    if not m:
        return None
    code = m.group("code")
    if code.lower() == "mixedqn":
        code = "mixedqn"
    part_str = m.group("part") or m.group("part2") or "1"
    return {
        "code":   code,
        "source": None,
        "part":   int(part_str),
    }


def meta_for(path, fname):
    meta_path = path[:-5] + ".meta.json"
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "code":   data.get("code"),
                "source": data.get("source_label"),
                "part":   int(data.get("part_number") or 1),
            }
        except Exception:
            pass
    return parse_filename(fname)


# ════════════════════════════════════════════════════════════════════════
# Upload
# ════════════════════════════════════════════════════════════════════════

def upload_one(args):
    local_path, fname, parent_id, existing_id = args
    drive = thread_drive()
    media = MediaFileUpload(local_path, mimetype="application/json",
                            resumable=False)
    try:
        if existing_id:
            def _update():
                return drive.files().update(
                    fileId=existing_id,
                    body={"name": fname},
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
            _retry(_update)
            return fname, existing_id, "updated", None

        meta = {"name": fname, "parents": [parent_id]}

        def _create():
            return drive.files().create(
                body=meta, media_body=media, fields="id",
                supportsAllDrives=True,
            ).execute()
        new = _retry(_create)
        return fname, new["id"], "created", None
    except HttpError as e:
        return fname, None, "error", f"HTTP {getattr(e.resp, 'status', '?')}: {e}"
    except Exception as e:
        return fname, None, "error", str(e)


# ════════════════════════════════════════════════════════════════════════
# Prune
# ════════════════════════════════════════════════════════════════════════

def prune_stale(cur, conn, level_id, level_key, want_keys,
                drive_main, dry_run):
    """
    Delete export_files rows for this level whose
    (subtopic_id, source_label, part_number) is not in want_keys,
    and trash the corresponding Drive files.

    export_files has no surrogate 'id' column; its primary key is the
    composite (subtopic_id, source_label, part_number). We delete
    row-by-row using that key.
    """
    cur.execute("""
        SELECT ef.subtopic_id, ef.source_label, ef.part_number,
               ef.drive_file_id
        FROM export_files ef
        JOIN subtopics s ON s.id = ef.subtopic_id
        JOIN chapters  c ON c.id = s.chapter_id
        WHERE c.level_id = %s
    """, (level_id,))

    stale = []
    for sid, src, part, fid in cur.fetchall():
        src = src or ""
        if (sid, src, part) not in want_keys:
            stale.append((sid, src, part, fid))

    stats = {"stale": len(stale), "trashed": 0, "failed": 0}
    if not stale:
        print(f"  {level_key}: prune: nothing stale")
        return stats

    print(f"  {level_key}: prune: {len(stale)} stale row(s)")
    if dry_run:
        for sid, src, part, fid in stale[:10]:
            print(f"    [prune] subtopic={sid} source={src!r} "
                  f"part={part} drive={fid or '(none)'}")
        if len(stale) > 10:
            print(f"    ... and {len(stale) - 10} more")
        stats["trashed"] = len(stale)
        return stats

    deleted = 0
    for sid, src, part, fid in stale:
        if fid:
            try:
                trash_drive_file(drive_main, fid)
            except Exception as e:
                print(f"    prune: trash failed for {fid}: {e} "
                      f"— keeping row (subtopic={sid}, source={src!r}, part={part})")
                stats["failed"] += 1
                continue

        cur.execute("""
            DELETE FROM export_files
            WHERE subtopic_id  = %s
              AND source_label = %s
              AND part_number  = %s
        """, (sid, src, part))
        deleted += 1

    if deleted:
        conn.commit()
        stats["trashed"] = deleted

    if stats["failed"]:
        print(f"  {level_key}: prune: {stats['failed']} row(s) kept "
              f"due to Drive errors (will retry next run)")

    return stats


# ════════════════════════════════════════════════════════════════════════
# Per-level orchestration
# ════════════════════════════════════════════════════════════════════════

def upload_level(cur, conn, level_key, level_label, workers, dry_run,
                 prune=True, prune_only=False):
    print(f"  {level_key}: starting")
    level_path = os.path.join(LOCAL_ROOT, level_label)
    if not os.path.isdir(level_path):
        print(f"  {level_key}: no local folder ({level_path}), skipping")
        return

    cur.execute("SELECT id FROM levels WHERE level_key = %s", (level_key,))
    row = cur.fetchone()
    if not row:
        print(f"  {level_key}: not in DB, skipping")
        return
    level_id = row[0]

    # ── chapter folders on disk ───────────────────────────────────────
    chapter_folders = {}
    for folder in sorted(os.listdir(level_path)):
        full = os.path.join(level_path, folder)
        if not os.path.isdir(full):
            continue
        m = re.match(r"^([0-9.]+)\s", folder)
        if m:
            chapter_folders[m.group(1)] = (folder, full)

    # ── subtopic code -> DB subtopic_id ───────────────────────────────
    cur.execute("""
        SELECT c.chapter_key, s.code, s.id
        FROM subtopics s JOIN chapters c ON c.id = s.chapter_id
        WHERE c.level_id = %s
    """, (level_id,))
    code_to_id = {(ck, code): sid for ck, code, sid in cur.fetchall()}

    # ── existing rows from export_files ───────────────────────────────
    # Normalize source_label to "" so classified files line up.
    cur.execute("""
        SELECT ef.subtopic_id, ef.source_label, ef.part_number,
               ef.drive_file_id, ef.content_hash
        FROM export_files ef
        JOIN subtopics s ON s.id = ef.subtopic_id
        JOIN chapters c  ON c.id = s.chapter_id
        WHERE c.level_id = %s
    """, (level_id,))
    existing = {}
    for sid, src, part, fid, chash in cur.fetchall():
        existing[(sid, src or "", part)] = (fid, chash)

    # ── Drive folder hierarchy ────────────────────────────────────────
    drive_main = thread_drive()
    level_drive_id = find_or_create_folder(drive_main, level_label, DRIVE_PARENT)

    ch_parent_ids = {}
    for ch_key, (ch_folder_name, _ch_path) in chapter_folders.items():
        ch_parent_ids[ch_key] = find_or_create_folder(
            drive_main, ch_folder_name, level_drive_id
        )

    # subtopic subfolders — discovered from local disk
    sub_parent_ids = {}     # (ch_key, sub_code) -> drive folder id
    for ch_key, (ch_folder_name, ch_path) in chapter_folders.items():
        for entry in sorted(os.listdir(ch_path)):
            full = os.path.join(ch_path, entry)
            if not os.path.isdir(full):
                continue
            sub_parent_ids[(ch_key, entry)] = find_or_create_folder(
                drive_main, entry, ch_parent_ids[ch_key]
            )

    # folder file cache so a failed DB write doesn't cause duplicates
    folder_cache = {}

    def files_in(folder_id):
        if folder_id not in folder_cache:
            folder_cache[folder_id] = list_folder_files(drive_main, folder_id)
        return folder_cache[folder_id]

    # ── local file discovery ──────────────────────────────────────────
    local_files = []
    for ch_key, (ch_folder_name, ch_path) in chapter_folders.items():
        for entry in sorted(os.listdir(ch_path)):
            sub_path = os.path.join(ch_path, entry)

            if os.path.isdir(sub_path):
                # new shape: <chapter>/<sub_code>/<file>.json
                sub_code = entry
                for fname in sorted(os.listdir(sub_path)):
                    if not fname.lower().endswith(".json"):
                        continue
                    if fname.endswith(".meta.json"):
                        continue
                    fpath = os.path.join(sub_path, fname)
                    parsed = meta_for(fpath, fname)
                    if not parsed or not parsed["code"]:
                        continue
                    subtopic_id = code_to_id.get((ch_key, parsed["code"]))
                    if not subtopic_id:
                        continue
                    local_files.append({
                        "path": fpath, "fname": fname, "ch_key": ch_key,
                        "sub_code": sub_code,
                        "subtopic_id": subtopic_id,
                        "source": parsed["source"],
                        "part": parsed["part"],
                    })

            elif (entry.lower().endswith(".json")
                  and not entry.endswith(".meta.json")):
                # legacy: file directly in chapter folder
                fpath = os.path.join(ch_path, entry)
                parsed = meta_for(fpath, entry)
                if not parsed or not parsed["code"]:
                    continue
                subtopic_id = code_to_id.get((ch_key, parsed["code"]))
                if not subtopic_id:
                    continue
                local_files.append({
                    "path": fpath, "fname": entry, "ch_key": ch_key,
                    "sub_code": None,
                    "subtopic_id": subtopic_id,
                    "source": parsed["source"],
                    "part": parsed["part"],
                })

    # ── set of keys that *should* exist for this level ────────────────
    want_keys = {
        (lf["subtopic_id"], lf["source"] or "", lf["part"])
        for lf in local_files
    }

    # ── prune pass ────────────────────────────────────────────────────
    prune_stats = {"stale": 0, "trashed": 0, "failed": 0}
    if prune:
        prune_stats = prune_stale(
            cur, conn, level_id, level_key, want_keys, drive_main, dry_run
        )

    if prune_only:
        return

    # ── parallel hashing ──────────────────────────────────────────────
    hashes = {}
    with ThreadPoolExecutor(max_workers=HASH_WORKERS) as pool:
        futs = {pool.submit(sha256_of, lf["path"]): lf["path"]
                for lf in local_files}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                hashes[p] = fut.result()
            except Exception as e:
                print(f"    hash error {p}: {e}")
                hashes[p] = None

    # ── build tasks ───────────────────────────────────────────────────
    tasks = []
    db_keys = []
    skipped = 0
    for lf in local_files:
        new_hash = hashes.get(lf["path"])
        if new_hash is None:
            continue

        if lf["sub_code"] is not None:
            target_folder_id = sub_parent_ids.get((lf["ch_key"], lf["sub_code"]))
        else:
            target_folder_id = ch_parent_ids[lf["ch_key"]]
        if not target_folder_id:
            continue

        key = (lf["subtopic_id"], lf["source"] or "", lf["part"])
        existing_id, old_hash = existing.get(key, (None, None))

        if not existing_id:
            existing_id = files_in(target_folder_id).get(lf["fname"])

        if existing_id and old_hash == new_hash:
            skipped += 1
            continue

        tasks.append((lf["path"], lf["fname"], target_folder_id, existing_id))
        db_keys.append({
            "subtopic_id": lf["subtopic_id"],
            "source_label": lf["source"] or "",
            "part": lf["part"],
            "parent_id": target_folder_id,
            "new_hash": new_hash,
        })

    if not tasks:
        parts = [f"0 to upload", f"{skipped} unchanged"]
        if prune:
            parts.append(f"{prune_stats['trashed']} pruned")
            if prune_stats["failed"]:
                parts.append(f"{prune_stats['failed']} prune-errors")
        print(f"  {level_key}: " + ", ".join(parts))
        return

    print(f"  {level_key}: {len(tasks)} to upload, {skipped} unchanged")
    if dry_run:
        for t in tasks[:10]:
            action = "update" if t[3] else "create"
            print(f"    [{action}] {t[1]}")
        if len(tasks) > 10:
            print(f"    ... and {len(tasks) - 10} more")
        return

    results = [None] * len(tasks)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(upload_one, t): i for i, t in enumerate(tasks)}
        for fut in as_completed(futs):
            i = futs[fut]
            results[i] = fut.result()
            done += 1
            if done % 20 == 0 or done == len(tasks):
                print(f"    uploaded {done}/{len(tasks)}")

    created = updated = errors = 0
    for i, (fname, file_id, action, err) in enumerate(results):
        if err:
            print(f"    ERROR {fname}: {err}")
            errors += 1
            continue
        dk = db_keys[i]
        cur.execute("""
            INSERT INTO export_files
                (subtopic_id, source_label, part_number,
                 drive_file_id, drive_parent_id, content_hash, last_uploaded_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (subtopic_id, source_label, part_number) DO UPDATE SET
                drive_file_id    = EXCLUDED.drive_file_id,
                drive_parent_id  = EXCLUDED.drive_parent_id,
                content_hash     = EXCLUDED.content_hash,
                last_uploaded_at = NOW()
        """, (
            dk["subtopic_id"],
            dk["source_label"],
            dk["part"],
            file_id,
            dk["parent_id"],
            dk["new_hash"],
        ))
        if action == "created":
            created += 1
        else:
            updated += 1

    conn.commit()

    parts = [f"{created} created", f"{updated} updated",
             f"{skipped} unchanged", f"{errors} errors"]
    if prune:
        parts.append(f"{prune_stats['trashed']} pruned")
        if prune_stats["failed"]:
            parts.append(f"{prune_stats['failed']} prune-errors")
    print(f"  {level_key}: " + ", ".join(parts))


# ════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="Upload export to Google Drive.")
    p.add_argument("target", help="level_key or 'all'")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--dry-run", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--no-prune", action="store_true",
                   help="Do not delete stale rows / trash stale Drive files")
    g.add_argument("--prune-only", action="store_true",
                   help="Only prune, do not upload anything")
    return p.parse_args()


def main():
    args = parse_args()
    levels = (["level5", "level7", "gk"]
              if args.target == "all" else [args.target])

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)

    print("Authenticating with Google ...")
    try:
        drive_client()
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    prune = not args.no_prune
    prune_only = args.prune_only

    start = time.time()
    for lv in levels:
        try:
            cur.execute("SELECT label FROM levels WHERE level_key = %s", (lv,))
            row = cur.fetchone()
            if not row:
                print(f"  {lv}: not found in DB, skipping")
                continue
            upload_level(cur, conn, lv, row[0], args.workers, args.dry_run,
                         prune=prune, prune_only=prune_only)
        except Exception as e:
            print(f"  {lv}: FAILED — {type(e).__name__}: {e}")
            conn.rollback()

    cur.close(); conn.close()
    print(f"\nDone in {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()