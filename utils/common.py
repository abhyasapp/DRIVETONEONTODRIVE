"""Small helpers that used to be copy-pasted across scripts."""
import hashlib
import random
import time

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def retry(fn, max_attempts=5, base=1.0, cap=30.0):
    """Run a Google API call, retrying throttling/5xx with jittered backoff."""
    from googleapiclient.errors import HttpError
    last = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            last = e
            if status not in RETRYABLE_STATUSES or attempt == max_attempts - 1:
                raise
            time.sleep(min(base * (2 ** attempt) + random.random(), cap))
    if last:
        raise last


def file_digest(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of(path):
    return file_digest(path, "sha256")


def md5_of(path):
    return file_digest(path, "md5")


def to_ms(value):
    """Epoch ms from epoch (int/float/str) or an ISO timestamp; 0 if unknown."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
