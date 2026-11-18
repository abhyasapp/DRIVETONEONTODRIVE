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
import os
import sys

from googleapiclient.http import MediaIoBaseUpload

OAUTH_CREDENTIALS_FILE = "oauth-credentials.json"
TOKEN_FILE             = "oauth-token.pickle"
SCOPES                 = ["https://www.googleapis.com/auth/drive"]

from config.settings import DRIVE_PARENT
LOCAL_FILE   = "abhyas_export/chapters-data.js"
FNAME        = "chapters-data.js"
MIME         = "application/javascript"



# ============================================================================
# OAUTH
# ============================================================================

from utils.common import retry as _retry, sha256_of, md5_of
from utils.drive_auth import drive_client as client  # shared OAuth


# ============================================================================
# RETRY
# ============================================================================


# ============================================================================
# HASH  (compared against Drive's own md5, so it works on a fresh CI runner)
# ============================================================================

def validate_local(path):
    """Refuse to publish a truncated or implausible chapters-data.js."""
    size = os.path.getsize(path)
    if size < 2000:
        raise ValueError(f"{path} is only {size} bytes - looks truncated")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    for marker in ("const DRIVE", "const CH_NAMES", "ChapterData"):
        if marker not in text:
            raise ValueError(f"{path} is missing '{marker}' - not a complete export")
    if text.count("{") != text.count("}") or text.count("[") != text.count("]"):
        # string contents can unbalance this legitimately, so only flag big gaps
        if abs(text.count("{") - text.count("}")) > 5:
            raise ValueError(f"{path} has unbalanced braces - looks truncated")


# ============================================================================
# MAIN
# ============================================================================

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true",
                   help="Upload even if the local content hash is unchanged.")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def _upload(drive, file_id=None):
    """Upload LOCAL_FILE, replacing file_id in place (or creating it).

    The file is opened and closed here. MediaFileUpload given a path keeps the
    handle open for the life of the object, which on Windows stops anything
    else from replacing or deleting that file (and leaks the descriptor).
    """
    with open(LOCAL_FILE, "rb") as fh:
        media = MediaIoBaseUpload(fh, mimetype=MIME, resumable=False)
        if file_id:
            return _retry(lambda: drive.files().update(
                fileId=file_id, body={"name": FNAME}, media_body=media,
                fields="id", supportsAllDrives=True,
            ).execute())
        return _retry(lambda: drive.files().create(
            body={"name": FNAME, "parents": [DRIVE_PARENT]},
            media_body=media, fields="id", supportsAllDrives=True,
        ).execute())


def main():
    args = parse_args()

    if not os.path.isfile(LOCAL_FILE):
        print(f"ERROR: {LOCAL_FILE} not found. "
              f"Run generate_chapters_data_all.py first.")
        sys.exit(1)

    try:
        validate_local(LOCAL_FILE)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    local_md5 = md5_of(LOCAL_FILE)
    local_sha = sha256_of(LOCAL_FILE)

    if args.dry_run:
        print(f"DRY-RUN: would upload {LOCAL_FILE} "
              f"(sha256 {local_sha[:12]}...)")
        return

    try:
        drive = client()
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Same flags as upload_any_level: without them a parent that lives in a
    # Shared Drive lists as empty, so every run created a brand-new file with
    # a brand-new id.
    safe_name = FNAME.replace("\\", "\\\\").replace("'", "\\'")
    q = (f"'{DRIVE_PARENT}' in parents and name = '{safe_name}' "
         f"and trashed = false")

    def _search():
        return drive.files().list(
            q=q, fields="files(id, name, md5Checksum, modifiedTime)",
            pageSize=20, orderBy="modifiedTime desc",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()

    found = _retry(_search).get("files", [])

    if found:
        keep = found[0]            # newest copy keeps serving; its id is stable
        fid = keep["id"]
        if len(found) > 1:
            print(f"WARNING: {len(found)} copies of {FNAME} in the folder; "
                  f"updating the newest ({fid}). Older ids: "
                  f"{[f['id'] for f in found[1:]]} - run tools/cleanup_drive "
                  f"after pointing the app at {fid}.")
        if not args.force and keep.get("md5Checksum") == local_md5:
            print(f"{FNAME} unchanged on Drive (md5 {local_md5[:12]}...) "
                  f"- id stays {fid}.")
            return

        _upload(drive, fid)
        print(f"updated {FNAME} -> {fid}")
    else:
        new = _upload(drive)
        print(f"created {FNAME} -> {new['id']}  "
              f"(first upload; later runs update this id in place)")


if __name__ == "__main__":
    main()