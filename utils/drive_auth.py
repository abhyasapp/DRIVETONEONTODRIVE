"""Shared Google Drive OAuth client (used by upload + tools).

Order of preference:
  1. GDRIVE_OAUTH_TOKEN env var (JSON)  - GitHub Actions / cloud
  2. oauth-token.pickle                 - local, after first login
  3. interactive browser login          - local only, first-time setup

A JSON token carries no access token, so we refresh whenever the
credentials are not valid (never rely on `expired`, which is False when
there is no expiry timestamp).
"""

import json
import os
import pickle
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

OAUTH_CREDENTIALS_FILE = "oauth-credentials.json"
TOKEN_FILE = "oauth-token.pickle"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _from_env():
    raw = os.environ.get("GDRIVE_OAUTH_TOKEN")
    if not raw:
        return None
    try:
        return Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    except Exception as e:
        print(f"  [auth] GDRIVE_OAUTH_TOKEN unusable: {e}")
        return None


def _from_pickle():
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        print(f"  [auth] {TOKEN_FILE} unreadable ({type(e).__name__}); "
              f"regenerate it or use GDRIVE_OAUTH_TOKEN")
        return None


def _save_pickle(creds):
    try:
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    except Exception:
        pass


def _refreshed(creds, save=False):
    if creds is None:
        return None
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
            if save:
                _save_pickle(creds)
        except Exception as e:
            print(f"  [auth] token refresh failed: {e}")
            return None
    return creds if creds.valid else None


def get_credentials():
    creds = _refreshed(_from_env())
    if creds:
        print("  [auth] using GDRIVE_OAUTH_TOKEN")
        return creds

    creds = _refreshed(_from_pickle(), save=True)
    if creds:
        print(f"  [auth] using {TOKEN_FILE}")
        return creds

    if not sys.stdin.isatty():
        raise RuntimeError(
            "No valid Google OAuth credentials and stdin is not a terminal. "
            "Set the GDRIVE_OAUTH_TOKEN secret (python -m utils.extract_token) "
            "or log in once locally to create oauth-token.pickle.")
    print("  [auth] interactive login - opening browser ...")
    flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_pickle(creds)
    return creds


def drive_client():
    return build("drive", "v3", credentials=get_credentials(),
                 cache_discovery=False)
