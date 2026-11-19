"""One client for the Google Apps Script backend (reports, corrections)."""
import json
import os

import requests


class BackendError(RuntimeError):
    pass


class UnknownAction(BackendError):
    """The deployed Apps Script does not implement this action yet."""


def call(action, timeout=90, **fields):
    url = os.getenv("ABHYAS_APPS_SCRIPT_URL")
    key = os.getenv("ABHYAS_SYNC_KEY")
    if not url:
        raise BackendError("ABHYAS_APPS_SCRIPT_URL not set")
    if not key:
        raise BackendError("ABHYAS_SYNC_KEY not set")
    body = json.dumps({"action": action, "key": key, **fields})
    # text/plain keeps the request CORS-simple and is what Apps Script expects
    resp = requests.post(url, data=body, headers={"Content-Type": "text/plain"},
                         timeout=timeout)
    if resp.status_code in (401, 403):
        raise BackendError(f"HTTP {resp.status_code}: key rejected by the backend")
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError:
        raise BackendError("backend did not return JSON (is the web app deployed "
                           "as 'Anyone' and the URL the /exec one?)")
    if not payload.get("success"):
        message = str(payload.get("error") or "backend returned success=false")
        if "unknown action" in message.lower():
            raise UnknownAction(message)
        raise BackendError(message)
    return payload
