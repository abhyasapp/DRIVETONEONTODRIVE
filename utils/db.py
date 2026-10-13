"""Shared Postgres connection helper (Neon-friendly).

Serverless Postgres can take 10-30 s to wake up, so every script connects
through here: generous timeout, a few retries, keepalives for long jobs.

    from utils.db import connect
    conn = connect()                  # uses DATABASE_URL
    conn = connect(some_dsn)          # explicit DSN
"""

import os
import time

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def connect(dsn=None, retries=4, timeout=30, **kwargs):
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set (.env or environment).")
    kwargs.setdefault("connect_timeout", timeout)
    kwargs.setdefault("keepalives", 1)
    kwargs.setdefault("keepalives_idle", 30)
    kwargs.setdefault("keepalives_interval", 10)
    kwargs.setdefault("keepalives_count", 5)

    last = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg2.connect(dsn, **kwargs)
        except psycopg2.OperationalError as e:
            last = e
            if attempt < retries:
                wait = 5 * attempt
                print(f"  [db] connect failed ({attempt}/{retries}): "
                      f"{str(e).strip()[:120]} - retrying in {wait}s")
                time.sleep(wait)
    raise last
