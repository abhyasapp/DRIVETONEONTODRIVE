"""
extract_token.py
----------------
Converts the local oauth-token.pickle into the JSON string that the GitHub
secret GDRIVE_OAUTH_TOKEN expects.

    python -m utils.extract_token            # prints JSON
    python -m utils.extract_token > token.json

The short-lived access token is deliberately left out: the upload scripts
refresh it from the refresh_token on every run, so a stale token can never
cause a 401.
"""

import json
import pickle
import sys

try:
    with open("oauth-token.pickle", "rb") as f:
        creds = pickle.load(f)
except FileNotFoundError:
    sys.exit("oauth-token.pickle not found. Run upload_any_level.py once "
             "locally (interactive login) to create it.")

if not creds.refresh_token:
    sys.exit("This token has no refresh_token - delete oauth-token.pickle "
             "and re-run the interactive login.")

print(json.dumps({
    "refresh_token": creds.refresh_token,
    "token_uri": creds.token_uri,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "scopes": list(creds.scopes or ["https://www.googleapis.com/auth/drive"]),
}))
