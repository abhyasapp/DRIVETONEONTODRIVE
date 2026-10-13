"""
upload_chapters_data.py  (v3 — cloud-ready)
-------------------------------------------
Uploads abhyas_export/chapters-data.js to the root of ABHYAS_AUTO.
Preserves file ID. Skips upload if content hash is unchanged.

Authentication, in priority order:
  1. GDRIVE_OAUTH_TOKEN env var (JSON string) — used by GitHub Actions
  2. oauth-token.pickle                        — used locally after first auth
  3. Interactive browser OAuth                 — first-time local setup only

Improvements:
  • Non-interactive OAuth — fails fast instead of hanging.
  • Content-hash check → no needless uploads.
  • Retry with exponential backoff on Drive 429/5xx.
  • --force to upload even when the hash matches.
  • --dry-run.

Usage:
    python upload_chapters_data.py
    python upload_chapters_data.py --force
    python upload_chapters_data.py --dry-run
"""

import argparse
import hashlib
import os
import random
import sys
import time

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

OAUTH_CREDENTIALS_FILE = "oauth-credentials.json"
TOKEN_FILE             = "oauth-token.pickle"
SCOPES                 = ["https://www.googleapis.com/auth/drive"]

from config.settings import DRIVE_PARENT
LOCAL_FILE   = "abhyas_export/chapters-data.js"
FNAME        = "chapters-data.js"
MIME         = "application/javascript"

HASH_FILE = LOCAL_FILE + ".uploaded_sha256"

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


# ============================================================================
# OAUTH
# ============================================================================

from utils.drive_auth import drive_client as client  # shared OAuth


# ============================================================================
# RETRY
# ============================================================================

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


# ============================================================================
# HASH
# ============================================================================

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def last_uploaded_hash():
    if not os.path.exists(HASH_FILE):
        return None
    try:
        with open(HASH_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:
        return None


def save_uploaded_hash(h):
    try:
        with open(HASH_FILE, "w", encoding="utf-8") as f:
            f.write(h)
    except Exception:
        pass


# ============================================================================
# MAIN
# ============================================================================

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true",
                   help="Upload even if the local content hash is unchanged.")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    if not os.path.isfile(LOCAL_FILE):
        print(f"ERROR: {LOCAL_FILE} not found. "
              f"Run generate_chapters_data_all.py first.")
        sys.exit(1)

    local_hash = sha256_of(LOCAL_FILE)
    prev = last_uploaded_hash()

    if not args.force and prev == local_hash:
        print(f"chapters-data.js unchanged (sha256 {local_hash[:12]}...) — "
              f"nothing to do.")
        return

    if args.dry_run:
        print(f"DRY-RUN: would upload {LOCAL_FILE} "
              f"(sha256 {local_hash[:12]}...)")
        return

    try:
        drive = client()
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    q = (f"'{DRIVE_PARENT}' in parents and name = '{FNAME}' "
         f"and trashed = false")

    def _search():
        return drive.files().list(
            q=q, fields="files(id, name)", pageSize=5
        ).execute()

    res = _retry(_search)
    media = MediaFileUpload(LOCAL_FILE, mimetype=MIME, resumable=False)

    if res.get("files"):
        fid = res["files"][0]["id"]

        def _update():
            return drive.files().update(
                fileId=fid,
                body={"name": FNAME},
                media_body=media,
                fields="id",
            ).execute()

        _retry(_update)
        print(f"updated {FNAME} -> {fid}")
    else:
        meta = {"name": FNAME, "parents": [DRIVE_PARENT]}

        def _create():
            return drive.files().create(
                body=meta, media_body=media, fields="id"
            ).execute()

        new = _retry(_create)
        print(f"created {FNAME} -> {new['id']}")

    save_uploaded_hash(local_hash)


if __name__ == "__main__":
    main()