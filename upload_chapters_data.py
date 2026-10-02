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
import json
import os
import pickle
import random
import sys
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

OAUTH_CREDENTIALS_FILE = "oauth-credentials.json"
TOKEN_FILE             = "oauth-token.pickle"
SCOPES                 = ["https://www.googleapis.com/auth/drive"]

DRIVE_PARENT = "1_gAGILhdleeR6gBUfEFSKOVpa8x5wkCE"
LOCAL_FILE   = "abhyas_export/chapters-data.js"
FNAME        = "chapters-data.js"
MIME         = "application/javascript"

HASH_FILE = LOCAL_FILE + ".uploaded_sha256"

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


# ============================================================================
# OAUTH
# ============================================================================

def _load_creds():
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_creds(creds):
    with open(TOKEN_FILE, "wb") as f:
        pickle.dump(creds, f)


def client():
    # 1. env var (GitHub Actions / cloud)
    raw = os.environ.get("GDRIVE_OAUTH_TOKEN")
    if raw:
        try:
            info = json.loads(raw)
            creds = Credentials.from_authorized_user_info(info, SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            if creds.valid:
                print("  [auth] using GDRIVE_OAUTH_TOKEN")
                return build("drive", "v3", credentials=creds,
                             cache_discovery=False)
        except Exception as e:
            print(f"  [auth] GDRIVE_OAUTH_TOKEN invalid: {e}")

    # 2. pickle (local, after first interactive auth)
    creds = _load_creds()
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_creds(creds)
        except Exception as e:
            print(f"  token refresh failed: {e}")
            creds = None
    if creds and creds.valid:
        print("  [auth] using oauth-token.pickle")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    # 3. interactive (local only)
    if not sys.stdin.isatty():
        raise RuntimeError(
            "OAuth token missing or invalid and stdin is not a terminal. "
            "Set GDRIVE_OAUTH_TOKEN (cloud) or run this script "
            "interactively once (local)."
        )
    print("  interactive OAuth required — opening browser ...")
    flow = InstalledAppFlow.from_client_secrets_file(
        OAUTH_CREDENTIALS_FILE, SCOPES
    )
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_creds(creds)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


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