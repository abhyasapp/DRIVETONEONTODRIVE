"""
classify_with_gemini.py  (v22 — persistent, non-looping)
--------------------------------------------------------
Changes over v20.1 that end the daily-loop problem:

  * Keyword fallback only returns codes that exist in the CURRENT
    chapter. Mismatched hint codes are logged once per chapter so
    you can fix l5_keywords.json / l7_keywords.json / gk_keywords.json.
    (This was the root cause of "Ch 2: 107 to classify" every run:
    hint codes were looked up against a different chapter's subtopics,
    failed, and every row was re-written as mixedqn.)

  * Rows that fail BOTH the LLM and keyword fallback are written with
    classified_by='deferred' and excluded from future runs unless you
    pass --retry-deferred. This guarantees each run makes forward
    progress and the pending count only ever goes down.

  * DB reconnect with TCP keepalives. The old script called
    conn.rollback() on a socket that had already died, which raised
    InterfaceError and killed the whole step. Now every DB operation
    retries on OperationalError/InterfaceError via a fresh connection.

  * Providers are only killed on PERMANENT errors (auth, 404). Rate
    and server errors use exponential backoff up to 15 minutes, so
    a key that's temporarily rate-limited comes back on its own.

  * Faster defaults so a full pass fits in hours, not days:
        SLEEP_BETWEEN         5s   (was 30)
        CALL_TIMEOUT          90s  (was 180)
        MAX_WAIT_PER_BATCH    60s  (was 180)
        MAX_WAIT_PER_CHAPTER  120s (was 600)
        BATCH_SIZE            8    (10 is fine but 8 is safer
                                    when only flash-lite is alive)

  * Exits 0 on partial completion — run_pipeline.py moves on to
    export/upload instead of retrying the whole gemini step.

Usage:
  python classify_with_gemini.py --selftest
  python classify_with_gemini.py all
  python classify_with_gemini.py level7 --retry-deferred
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
import warnings
from collections import defaultdict
from datetime import datetime

import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values
from dotenv import load_dotenv

warnings.filterwarnings(
    "ignore",
    message=r".*automatic function calling.*",
    category=UserWarning,
)

try:
    from google import genai
    from google.genai import types as gtypes
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_SDK = True
except ImportError:
    OPENAI_SDK = False

load_dotenv()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ════════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════════

def _env_int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


BATCH_SIZE           = _env_int("CLASSIFY_BATCH_SIZE", 8)
SLEEP_BETWEEN        = _env_float("CLASSIFY_SLEEP_BETWEEN", 5.0)
TEMPERATURE          = _env_float("CLASSIFY_TEMPERATURE", 0.1)
CALL_TIMEOUT         = _env_int("CLASSIFY_CALL_TIMEOUT", 90)
MAX_WAIT_PER_BATCH   = _env_int("CLASSIFY_MAX_WAIT_PER_BATCH", 60)
MAX_WAIT_PER_CHAPTER = _env_int("CLASSIFY_MAX_WAIT_PER_CHAPTER", 120)
QUIET                = _env_int("CLASSIFY_QUIET", 1) == 1

MAX_COOLDOWN         = 900.0     # hard cap 15 min for rate/server backoff
DEFAULT_COOLDOWN     = 45.0
SERVER_COOLDOWN      = 30.0
PARSE_COOLDOWN       = 20.0
KEYWORD_MIN_HITS     = 2

KEY_BREAKER_WINDOW   = 20.0
KEY_BREAKER_THRESHOLD = 5
KEY_BREAKER_COOLDOWN = 60.0

# Only auth and 404 are permanent. Rate/server errors get long backoff but
# the provider is not removed from the pool.
GEMINI_TIERS = [
    (100, "gemini-3.6-flash"),
    (110, "gemini-3.5-flash"),
    (120, "gemini-3.5-flash-lite"),
    (130, "gemini-3.1-flash-lite"),
]

GROQ_TIERS = [
    (900, "groq", "https://api.groq.com/openai/v1",
     ["llama-3.3-70b-versatile", "qwen/qwen3-32b",
      "openai/gpt-oss-120b", "llama-3.1-8b-instant"]),
]
CEREBRAS_TIERS = [
    (910, "cerebras", "https://api.cerebras.ai/v1",
     ["gpt-oss-120b", "qwen-3.8-27b", "llama-3.3-70b"]),
]
OPENROUTER_TIERS = [
    (920, "openrouter", "https://api.openrouter.ai/v1",
     ["openrouter/free",
      "meta-llama/llama-3.3-70b-instruct:free",
      "qwen/qwen3-32b:free"]),
]

KEYWORD_FILE_FOR = {
    "level5": "l5_keywords.json",
    "gk":     "gk_keywords.json",
    "level7": "l7_keywords.json",
}

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

ANSWERS_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "q":    {"type": "integer"},
                    "code": {"type": "string"},
                    "conf": {"type": "number"},
                },
                "required": ["q", "code"],
            },
        },
    },
    "required": ["answers"],
}


def _log(msg, *, force=False):
    if not QUIET or force:
        print(msg, flush=True)


# ════════════════════════════════════════════════════════════════════════
# DB — reconnect-safe, keepalive-enabled
# ════════════════════════════════════════════════════════════════════════

class DB:
    def __init__(self, url):
        self.url = url
        self.conn = None
        self.cur = None
        self._open()

    def _open(self):
        kwargs = dict(
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
            connect_timeout=15,
        )
        try:
            self.conn = psycopg2.connect(self.url, **kwargs)
        except TypeError:
            self.conn = psycopg2.connect(self.url)
        self.cur = self.conn.cursor()

    def _reconnect(self):
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        time.sleep(2.0)
        self._open()

    def execute(self, sql, params=(), *, retries=2):
        last = None
        for attempt in range(retries + 1):
            try:
                self.cur.execute(sql, params)
                return self.cur
            except (psycopg2.OperationalError,
                    psycopg2.InterfaceError) as e:
                last = e
                if attempt < retries:
                    _log(f"    [DB-RECONNECT] {type(e).__name__}: "
                         f"{str(e)[:100]} (attempt {attempt+1})", force=True)
                    self._reconnect()
                else:
                    raise
        raise last  # pragma: no cover

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def commit(self):
        self.cur.connection.commit()

    def close(self):
        try:
            self.cur.close()
            self.conn.close()
        except Exception:
            pass


INSERT_SQL = """
INSERT INTO question_subtopics
    (question_id, subtopic_id, confidence, matched_keywords, classified_by)
VALUES %s
"""


def _apply_batch(db, qids, rows):
    """DELETE + INSERT + COMMIT with reconnect-retry on socket death."""
    def _run():
        db.execute(
            "DELETE FROM question_subtopics WHERE question_id = ANY(%s)",
            (qids,),
        )
        if rows:
            execute_values(
                db.cur, INSERT_SQL, rows,
                template="(%s, %s, %s, %s, %s)",
                page_size=len(rows),
            )
        db.commit()

    try:
        _run()
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        _log(f"    [DB-RECONNECT] batch write: {str(e)[:100]}", force=True)
        db._reconnect()
        _run()


# ════════════════════════════════════════════════════════════════════════
# Timeout + error classification
# ════════════════════════════════════════════════════════════════════════

class CallTimeout(Exception):
    pass


def call_with_timeout(fn, timeout, *args, **kwargs):
    result = {"value": None, "error": None, "done": False}

    def worker():
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as e:
            result["error"] = e
        finally:
            result["done"] = True

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if not result["done"]:
        raise CallTimeout(f"no response in {timeout}s")
    if result["error"]:
        raise result["error"]
    return result["value"]


def classify_error(err_text):
    """Return (kind, base_cooldown_secs).

    Kinds:
      'missing'  — permanent, kill provider
      'auth'     — permanent, kill provider
      'rate'     — long backoff, keep provider
      'server'   — medium backoff, keep provider
      'other'    — short backoff
    """
    lower = (err_text or "").lower()

    if "404" in lower or "not found" in lower or "no such model" in lower:
        return "missing", 0.0
    if ("401" in lower or "403" in lower
            or "api key not valid" in lower
            or "unauthenticated" in lower
            or "permission denied" in lower
            or "invalid api key" in lower):
        return "auth", 0.0

    m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", lower)
    if m:
        return "rate", max(30.0, min(MAX_COOLDOWN, float(m.group(1)) * 2))

    if "503" in lower or "unavailable" in lower or "high demand" in lower:
        return "server", SERVER_COOLDOWN
    if ("429" in lower or "resource_exhausted" in lower
            or "rate_limit" in lower or "quota" in lower):
        return "rate", DEFAULT_COOLDOWN
    return "other", PARSE_COOLDOWN


def _jitter(seconds):
    return seconds * (0.8 + 0.4 * random.random())


# ════════════════════════════════════════════════════════════════════════
# Providers
# ════════════════════════════════════════════════════════════════════════

class KeyBreaker:
    def __init__(self, window=KEY_BREAKER_WINDOW,
                 threshold=KEY_BREAKER_THRESHOLD,
                 cooldown=KEY_BREAKER_COOLDOWN):
        self.window = window
        self.threshold = threshold
        self.cooldown_secs = cooldown
        self._hits = []
        self.available_at = 0.0

    def record_failure(self):
        now = time.time()
        self._hits = [t for t in self._hits if now - t < self.window]
        self._hits.append(now)
        if len(self._hits) >= self.threshold:
            self.available_at = max(self.available_at, now + self.cooldown_secs)
            self._hits.clear()
            return True
        return False

    def is_available(self):
        return time.time() >= self.available_at

    def remaining(self):
        return max(0.0, self.available_at - time.time())


class Provider:
    def __init__(self, name, model, strength, breaker=None):
        self.name = name
        self.model = model
        self.strength = strength
        self.breaker = breaker
        self.available_at = 0.0
        self.dead = False
        self.consecutive_failures = 0
        self.parse_fails = 0
        self.stats = defaultdict(int)
        self.total_latency = 0.0

    def is_available(self, now=None):
        if self.dead:
            return False
        if self.breaker is not None and not self.breaker.is_available():
            return False
        now = now if now is not None else time.time()
        return now >= self.available_at

    def cooldown(self, seconds):
        self.available_at = max(self.available_at,
                                time.time() + max(1.0, seconds))

    def record_failure(self, kind, base_secs):
        self.consecutive_failures += 1
        self.stats[f"fail_{kind}"] += 1
        # exponential backoff up to MAX_COOLDOWN
        factor = min(2 ** (self.consecutive_failures - 1), 16)
        secs = min(MAX_COOLDOWN, base_secs * factor)
        secs = _jitter(secs)
        self.cooldown(secs)
        return secs

    def record_success(self, latency):
        self.stats["ok"] += 1
        self.total_latency += latency
        self.consecutive_failures = 0
        self.parse_fails = 0

    def kill(self, reason=""):
        if not self.dead:
            self.dead = True
            tag = f" ({reason})" if reason else ""
            _log(f"    [KILL]{tag} {self.name}:{self.model} disabled for this run",
                 force=True)

    def call(self, prompt):
        raise NotImplementedError

    def metrics_line(self):
        ok = self.stats.get("ok", 0)
        avg = (self.total_latency / ok) if ok else 0.0
        parts = [f"{k}={v}" for k, v in sorted(self.stats.items())]
        return f"{self.name}:{self.model} ok={ok} avg={avg:.2f}s " + " ".join(parts)

    def __repr__(self):
        return f"{self.name}:{self.model}"

    def __hash__(self):
        return hash((self.name, self.model))

    def __eq__(self, other):
        return (isinstance(other, Provider)
                and other.name == self.name
                and other.model == self.model)


def _gemini_config():
    kwargs = dict(
        temperature=TEMPERATURE,
        response_mime_type="application/json",
        response_schema=ANSWERS_SCHEMA,
    )
    try:
        kwargs["safety_settings"] = [
            gtypes.SafetySetting(category="HARM_CATEGORY_HARASSMENT",
                                 threshold="BLOCK_NONE"),
            gtypes.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH",
                                 threshold="BLOCK_NONE"),
            gtypes.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                                 threshold="BLOCK_NONE"),
            gtypes.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT",
                                 threshold="BLOCK_NONE"),
        ]
        return gtypes.GenerateContentConfig(**kwargs)
    except Exception:
        kwargs.pop("safety_settings", None)
        return gtypes.GenerateContentConfig(**kwargs)


class GeminiProvider(Provider):
    def __init__(self, client, model, key_label, strength, breaker=None):
        super().__init__(key_label, model, strength, breaker)
        self.client = client

    def call(self, prompt):
        resp = call_with_timeout(
            self.client.models.generate_content,
            CALL_TIMEOUT,
            model=self.model,
            contents=prompt,
            config=_gemini_config(),
        )
        if resp is None or not getattr(resp, "candidates", None):
            raise RuntimeError("gemini: empty candidates (safety-blocked?)")
        text = getattr(resp, "text", None)
        if not text:
            raise RuntimeError("gemini: empty text in response")
        return text


class OpenAICompatProvider(Provider):
    def __init__(self, name, base_url, api_key, model, strength, breaker=None):
        super().__init__(name, model, strength, breaker)
        kwargs = dict(api_key=api_key, base_url=base_url)
        try:
            kwargs["timeout"] = CALL_TIMEOUT
            kwargs["max_retries"] = 0
            self.client = OpenAI(**kwargs)
        except TypeError:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        self._supports_json_mode = True

    def _call(self, prompt, json_mode):
        kwargs = dict(
            model=self.model,
            messages=[
                {"role": "system",
                 "content": "You classify exam questions. Reply ONLY with JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=TEMPERATURE,
            max_tokens=BATCH_SIZE * 80,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return self.client.chat.completions.create(**kwargs)

    def call(self, prompt):
        try:
            resp = call_with_timeout(
                self._call, CALL_TIMEOUT, prompt, self._supports_json_mode
            )
        except Exception as e:
            msg = str(e).lower()
            if self._supports_json_mode and "response_format" in msg:
                self._supports_json_mode = False
                self.stats["json_mode_off"] += 1
                resp = call_with_timeout(self._call, CALL_TIMEOUT, prompt, False)
            else:
                raise
        if not resp or not resp.choices:
            raise RuntimeError(f"{self.name}: empty response")
        return resp.choices[0].message.content or ""


class ProviderPool:
    def __init__(self, providers):
        self.providers = sorted(providers, key=lambda p: (p.strength, p.name))
        self._key_last_used = defaultdict(float)

    def __len__(self):
        return len(self.providers)

    def mark_key_used(self, provider):
        self._key_last_used[provider.name] = time.time()

    def has_available(self):
        now = time.time()
        return any(p.is_available(now) for p in self.providers)

    def best_available(self, exclude=None):
        exclude = exclude or set()
        now = time.time()
        candidates = [p for p in self.providers
                      if p not in exclude and p.is_available(now)]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (p.strength,
                                       self._key_last_used.get(p.name, 0.0)))
        return candidates[0]

    def next_available_in(self):
        now = time.time()
        waits = []
        for p in self.providers:
            if p.dead:
                continue
            wait = max(0.0, p.available_at - now)
            if p.breaker is not None:
                wait = max(wait, p.breaker.remaining())
            waits.append(wait)
        return min(waits) if waits else None

    def status_line(self):
        now = time.time()
        avail = sum(1 for p in self.providers if p.is_available(now))
        cooling = sum(1 for p in self.providers
                      if not p.dead and not p.is_available(now))
        dead = sum(1 for p in self.providers if p.dead)
        return (f"{avail} avail / {cooling} cooling / {dead} dead / "
                f"{len(self.providers)} total")

    def metrics_report(self):
        lines = ["", "Provider metrics:"]
        for p in self.providers:
            lines.append("  " + p.metrics_line())
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
# Discovery
# ════════════════════════════════════════════════════════════════════════

def _collect_gemini_keys():
    keys = []
    primary = os.getenv("GEMINI_API_KEY")
    if primary:
        keys.append(("gemini#1", primary.strip()))
    n = 2
    while True:
        k = os.getenv(f"GEMINI_API_KEY_{n}")
        if not k:
            break
        keys.append((f"gemini#{n}", k.strip()))
        n += 1
    seen, out = set(), []
    for label, k in keys:
        if k in seen:
            continue
        seen.add(k)
        out.append((label, k))
    return out


def _fuzzy_match(tier_model, available):
    if tier_model in available:
        return tier_model
    for a in available:
        if a.startswith(tier_model):
            return a
    return None


def _make_gemini_client(api_key):
    try:
        return genai.Client(
            api_key=api_key,
            http_options=gtypes.HttpOptions(timeout=CALL_TIMEOUT * 1000),
        )
    except Exception:
        return genai.Client(api_key=api_key)


def _probe_gemini_key(label, api_key):
    try:
        client = _make_gemini_client(api_key)
        available = set()
        for m in client.models.list():
            raw = getattr(m, "name", "") or ""
            name = raw.replace("models/", "").strip()
            if name:
                available.add(name)
    except Exception as e:
        print(f"  [{label}] could not list models: {str(e)[:110]}")
        return []

    if not available:
        print(f"  [{label}] no models visible to this key")
        return []

    breaker = KeyBreaker()
    providers = []
    matched = []
    for strength, tier_model in GEMINI_TIERS:
        actual = _fuzzy_match(tier_model, available)
        if actual:
            providers.append(
                GeminiProvider(client, actual, label, strength, breaker))
            matched.append(actual)

    if not providers:
        print(f"  [{label}] none of the tiered models exist on this key")
    else:
        print(f"  [{label}] {len(providers)} tier(s): {', '.join(matched)}")
    return providers


def _probe_external(tier_list):
    providers = []
    for strength, name, base_url, models in tier_list:
        env_key = f"{name.upper()}_API_KEY"
        api_key = os.getenv(env_key)
        if not api_key:
            print(f"  {env_key} not set; skipping {name}")
            continue
        chosen = None
        for model in models:
            try:
                p = OpenAICompatProvider(name, base_url, api_key.strip(),
                                         model, strength)
                call_with_timeout(
                    lambda: p.client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": "Reply OK."}],
                        max_tokens=5,
                    ),
                    20,
                )
                chosen = p
                print(f"  [OK] {name}:{model}")
                break
            except Exception as e:
                print(f"  [skip] {name}:{model}: {str(e)[:90]}")
        if chosen:
            providers.append(chosen)
    return providers


def build_provider_pool():
    providers = []
    gemini_keys = _collect_gemini_keys()

    if not GEMINI_AVAILABLE and gemini_keys:
        print("  google-genai SDK not installed; skipping Gemini.")
    elif not gemini_keys:
        print("  no GEMINI_API_KEY* set; skipping Gemini.")
    else:
        print(f"Discovered {len(gemini_keys)} Gemini API key(s).")
        for label, key in gemini_keys:
            providers.extend(_probe_gemini_key(label, key))

    if OPENAI_SDK:
        providers.extend(_probe_external(GROQ_TIERS))
        providers.extend(_probe_external(CEREBRAS_TIERS))
        providers.extend(_probe_external(OPENROUTER_TIERS))

    return ProviderPool(providers)


# ════════════════════════════════════════════════════════════════════════
# Prompt
# ════════════════════════════════════════════════════════════════════════

def format_question(num, q_text, options):
    q_text = (q_text or "").strip()
    lines = [f"Q{num}. {q_text}"]
    if options:
        for j, opt in enumerate(options):
            letter = chr(ord("A") + j)
            lines.append(f"    {letter}) {(str(opt) or '').strip()}")
    return "\n".join(lines)


def build_prompt(chapter_name, subtopics, questions, keyword_hints=None):
    sub_lines = []
    for code, title in subtopics:
        hint = ""
        if keyword_hints and code in keyword_hints:
            kws = keyword_hints[code][:8]
            hint = f"   [keywords: {', '.join(kws)}]"
        sub_lines.append(f'  - "{code}" : {title}{hint}')
    sub_block = "\n".join(sub_lines)

    q_blocks = "\n\n".join(
        format_question(num, txt, opts) for num, txt, opts in questions
    )

    return f"""You classify Nepal PSC engineering exam questions into syllabus subtopics.

Chapter: {chapter_name}

Available subtopics (use the exact quoted code, nothing else):
{sub_block}
  - "mixedqn" : last resort only

Questions to classify:

{q_blocks}

Rules:
1. Use the question text AND its options.
2. Judge by MEANING, not exact keyword match.
3. If you are 60%+ confident, choose that subtopic. Do NOT pick mixedqn
   just because you are unsure — pick the closest subtopic you see.
4. Use "mixedqn" ONLY if the text is blank/unreadable, or the question
   clearly belongs to a different chapter.
5. Return the subtopic CODE only (never the title).
6. "conf" is a 0.0–1.0 confidence score.

Output a JSON object with key "answers": a list of
{{"q": <question number>, "code": "<code>", "conf": <0-1>}}.

Reply with ONLY the JSON object."""


# ════════════════════════════════════════════════════════════════════════
# Parsing
# ════════════════════════════════════════════════════════════════════════

def parse_response(text):
    if text is None:
        return None, "empty response"
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s).strip()
    try:
        return json.loads(s), None
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(s)):
            if s[i] == opener:
                depth += 1
            elif s[i] == closer:
                depth -= 1
                if depth == 0:
                    cand = s[start:i + 1]
                    try:
                        return json.loads(cand), None
                    except json.JSONDecodeError as e:
                        return None, f"json error: {e}"
    return None, "no JSON found"


def normalize_mapping(parsed):
    if isinstance(parsed, dict):
        for wrapper in ("answers", "result", "results",
                        "questions", "mapping", "response"):
            if wrapper in parsed and isinstance(parsed[wrapper], (dict, list)):
                parsed = parsed[wrapper]
                break

    out = {}

    if isinstance(parsed, dict):
        for k, v in parsed.items():
            code = None
            conf = None
            if isinstance(v, dict):
                code = (v.get("code") or v.get("subtopic")
                        or v.get("answer") or v.get("label"))
                c = v.get("conf", v.get("confidence"))
                if isinstance(c, (int, float)):
                    conf = float(c)
            elif isinstance(v, str):
                code = v
            out[str(k)] = (code if code is not None else "mixedqn", conf)
        return out, None

    if isinstance(parsed, list):
        if all(isinstance(x, str) for x in parsed):
            for i, v in enumerate(parsed, 1):
                out[str(i)] = (v, None)
            return out, None
        for i, v in enumerate(parsed, 1):
            if not isinstance(v, dict):
                continue
            num = (v.get("q") or v.get("question") or v.get("number")
                   or v.get("id") or i)
            code = (v.get("code") or v.get("subtopic")
                    or v.get("answer") or v.get("label"))
            c = v.get("conf", v.get("confidence"))
            conf = float(c) if isinstance(c, (int, float)) else None
            if code is not None:
                out[str(num)] = (str(code), conf)
        return out, None

    return {}, f"unexpected parsed type: {type(parsed).__name__}"


def build_title_to_code(subtopics):
    out = {}
    for code, title in subtopics:
        t = re.sub(r"\s+", " ", (title or "").strip()).lower()
        if t:
            out[t] = code
    return out


def normalize_code(raw_code, codes_valid, title_to_code):
    if raw_code is None:
        return "mixedqn"
    if not isinstance(raw_code, str):
        raw_code = str(raw_code)
    s = raw_code.strip()
    if not s:
        return "mixedqn"
    if s in codes_valid:
        return s
    lower_map = {c.lower(): c for c in codes_valid}
    if s.lower() in lower_map:
        return lower_map[s.lower()]
    first = re.split(r"[\s,;:]+", s, maxsplit=1)[0].strip(" .,;:-")
    if first in codes_valid:
        return first
    if first.lower() in lower_map:
        return lower_map[first.lower()]
    m = re.match(r"^(\d+(?:\.\d+)*)", s)
    if m:
        parts = m.group(1).split(".")
        while parts:
            cand = ".".join(parts)
            if cand in codes_valid:
                return cand
            parts = parts[:-1]
    norm = re.sub(r"\s+", " ", s).lower()
    if norm in title_to_code:
        return title_to_code[norm]
    if norm:
        hits = [c for t, c in title_to_code.items()
                if t and (norm in t or t in norm)]
        if len(hits) == 1:
            return hits[0]
    return "mixedqn"


# ════════════════════════════════════════════════════════════════════════
# Keyword fallback — only returns codes valid for THIS chapter
# ════════════════════════════════════════════════════════════════════════

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text):
    return set(_TOKEN_RE.findall(text.lower()))


def keyword_fallback(question_text, options, keyword_hints,
                     valid_codes=None, min_hits=KEYWORD_MIN_HITS):
    if not keyword_hints:
        return None, 0.0
    blob = (question_text or "") + " " + " ".join(str(o) for o in (options or []))
    tokens = _tokenize(blob)
    if not tokens:
        return None, 0.0

    best_code = None
    best_score = 0
    for code, kws in keyword_hints.items():
        if valid_codes is not None and code not in valid_codes:
            continue
        kw_tokens = set()
        for k in kws:
            kw_tokens |= _tokenize(str(k))
        if not kw_tokens:
            continue
        score = len(tokens & kw_tokens)
        if score > best_score:
            best_code, best_score = code, score

    if best_code is None or best_score < min_hits:
        return None, 0.0
    conf = min(0.6, 0.25 + 0.10 * best_score)
    return best_code, conf


# ════════════════════════════════════════════════════════════════════════
# Failure dump
# ════════════════════════════════════════════════════════════════════════

_RUN_TAG = f"{os.getpid()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
_RUN_DIR = os.path.join(LOG_DIR, f"run_{_RUN_TAG}")
os.makedirs(_RUN_DIR, exist_ok=True)


def _dump_failure(chapter, batch_idx, provider, prompt, raw, parsed, err):
    ts = datetime.now().strftime("%H%M%S_%f")
    path = os.path.join(_RUN_DIR, f"fail_{chapter}_{batch_idx}_{ts}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": ts, "chapter": chapter,
                "batch_index": batch_idx,
                "provider": repr(provider) if provider else None,
                "error": err,
                "prompt_head": (prompt or "")[:4000],
                "raw_response": raw, "parsed": parsed,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════
# Batch classification
# ════════════════════════════════════════════════════════════════════════

def classify_batch(pool, prompt, chapter, batch_idx):
    tried = set()
    while True:
        p = pool.best_available(exclude=tried)
        if p is None:
            return None, None

        tried.add(p)
        pool.mark_key_used(p)

        t0 = time.time()
        try:
            text = p.call(prompt)
        except CallTimeout:
            _log(f"    [TIMEOUT] {p.name}:{p.model}")
            p.record_failure("timeout", DEFAULT_COOLDOWN)
            if p.breaker is not None and p.breaker.record_failure():
                _log(f"    [KEY-BREAKER] {p.name} tripped", force=True)
            continue
        except Exception as e:
            kind, base = classify_error(str(e))
            if kind == "missing":
                p.kill("404 / model not found")
                continue
            if kind == "auth":
                p.kill("auth error")
                continue
            secs = p.record_failure(kind, base)
            tripped = False
            if kind in ("server", "rate") and p.breaker is not None:
                tripped = p.breaker.record_failure()
            tag = {"rate": "RATE", "server": "BUSY"}.get(kind, "ERR")
            _log(f"    [{tag}] {p.name}:{p.model} cooldown {secs:.0f}s")
            if tripped:
                _log(f"    [KEY-BREAKER] {p.name} key cooling "
                     f"{KEY_BREAKER_COOLDOWN:.0f}s", force=True)
            continue

        latency = time.time() - t0
        parsed, perr = parse_response(text)
        if perr is None:
            mapping, merr = normalize_mapping(parsed)
            if merr is None and mapping:
                p.record_success(latency)
                return mapping, p
            perr = merr or "empty mapping"

        p.parse_fails += 1
        _log(f"    [PARSE-FAIL] {p.name}:{p.model}: {perr}", force=True)
        _dump_failure(chapter, batch_idx, p, prompt, text, parsed, perr)
        if p.parse_fails >= 5:
            p.kill(f"{p.parse_fails} parse failures")
        else:
            secs = p.record_failure("parse", PARSE_COOLDOWN)
            _log(f"      -> cooldown {secs:.0f}s")


# ════════════════════════════════════════════════════════════════════════
# Chapter classification
# ════════════════════════════════════════════════════════════════════════

def classify_chapter(pool, db, chapter_name, subtopics, questions,
                     code_to_id, keyword_hints, chapter_key=""):
    codes_valid = {code for code, _ in subtopics} | {"mixedqn"}
    title_to_code = build_title_to_code(subtopics)

    # One-time diagnostic: which hint codes don't exist in this chapter?
    hint_codes = set(keyword_hints.keys())
    unknown = sorted(hint_codes - codes_valid)
    if unknown:
        _log(f"    [HINT-MISMATCH] {len(unknown)} keyword-hint codes are "
             f"not subtopics of this chapter: {unknown[:8]}", force=True)

    total = len(questions)
    classified = 0
    deferred = 0
    fallback_success = 0
    start = 0
    total_wait = 0.0

    while start < total:
        batch = questions[start:start + BATCH_SIZE]
        batch_idx = start // BATCH_SIZE
        numbered = [(start + i + 1, txt, opts)
                    for i, (_, txt, opts) in enumerate(batch)]

        # If NO provider is available and the wait is short, sleep and retry.
        if not pool.has_available() and total_wait < MAX_WAIT_PER_CHAPTER:
            wait = pool.next_available_in()
            if wait is not None and 0 < wait <= MAX_WAIT_PER_BATCH:
                _log(f"    [WAIT] pool empty; sleeping {wait:.0f}s")
                time.sleep(wait + 1.0)
                total_wait += wait + 1.0
                continue

        mapping = None
        provider_used = None
        t_batch_start = time.time()

        if pool.has_available():
            prompt = build_prompt(chapter_name, subtopics, numbered,
                                  keyword_hints)
            mapping, provider_used = classify_batch(pool, prompt,
                                                     chapter_name, batch_idx)
            if mapping is None:
                _log("    [SKIP] no parseable reply; keyword fallback only")

        qids = [qid for qid, _, _ in batch]
        rows = []
        batch_ok = 0
        batch_fb = 0
        batch_def = 0

        for i, (qid, qtext, opts) in enumerate(batch):
            gnum = start + i + 1
            raw = (mapping or {}).get(str(gnum))
            raw_code = raw[0] if raw else None
            raw_conf = raw[1] if raw else None
            source = "gemini"

            code = (normalize_code(raw_code, codes_valid, title_to_code)
                    if raw_code else "mixedqn")
            conf = raw_conf

            if code == "mixedqn":
                fb_code, fb_conf = keyword_fallback(
                    qtext, opts, keyword_hints, codes_valid
                )
                if fb_code:
                    code, conf = fb_code, fb_conf
                    source = "keyword_fallback"

            sub_id = code_to_id.get(code)
            if not sub_id:
                sub_id = code_to_id.get("mixedqn")
                code = "mixedqn"

            if code == "mixedqn":
                source = "deferred"
                batch_def += 1
                deferred += 1
            elif source == "keyword_fallback":
                batch_fb += 1
                fallback_success += 1
                batch_ok += 1
                classified += 1
            else:
                batch_ok += 1
                classified += 1

            matched = json.dumps({"llm": raw_code, "conf": raw_conf})
            rows.append((qid, sub_id, conf, matched, source))

        try:
            _apply_batch(db, qids, rows)
        except Exception as e:
            _log(f"    [DB-ERR] batch write failed: {str(e)[:130]}", force=True)
            start += BATCH_SIZE
            continue

        done = min(start + BATCH_SIZE, total)
        prov = (f"{provider_used.name}:{provider_used.model}"
                if provider_used else "—")
        _log(f"    batch {done}/{total}  ok={batch_ok} "
             f"def={batch_def} fb={batch_fb} via={prov}  "
             f"[{pool.status_line()}]", force=True)

        if provider_used is not None:
            elapsed = time.time() - t_batch_start
            gap = max(0.0, SLEEP_BETWEEN - elapsed)
            if gap:
                time.sleep(gap)

        start += BATCH_SIZE

    _log(f"    (chapter summary: {classified} classified, "
         f"{fallback_success} via keyword, {deferred} deferred)", force=True)

    return classified, deferred


# ════════════════════════════════════════════════════════════════════════
# Keyword hints
# ════════════════════════════════════════════════════════════════════════

def load_keyword_hints(level_key):
    path = KEYWORD_FILE_FOR.get(level_key)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  could not load {path}: {e}")
        return {}
    flat = {}
    for _, subs in data.items():
        for key, kws in subs.items():
            code = key.split()[0]
            flat[code] = kws
    return flat


# ════════════════════════════════════════════════════════════════════════
# Level processor
# ════════════════════════════════════════════════════════════════════════

def process_level(level_key, pool, db, retry_deferred):
    print(f"\n=== {level_key} ===")

    db.execute("SELECT id FROM levels WHERE level_key = %s", (level_key,))
    row = db.fetchone()
    if not row:
        print(f"  ERROR: level '{level_key}' not found.")
        return 0, 0
    level_id = row[0]

    keyword_hints = load_keyword_hints(level_key)
    print(f"  loaded {len(keyword_hints)} subtopic keyword hints")

    db.execute("""
        SELECT id, chapter_key, chapter_name
        FROM chapters WHERE level_id = %s
        ORDER BY chapter_key::numeric
    """, (level_id,))
    chapters = db.fetchall()

    total_classified = 0
    total_deferred = 0

    for ch_id, ch_key, ch_name in chapters:
        db.execute("""
            SELECT s.code, s.title FROM subtopics s
            WHERE s.chapter_id = %s AND s.code <> 'mixedqn'
            ORDER BY s.code
        """, (ch_id,))
        subtopics = db.fetchall()

        # Fetch rows that need classification. Deferred rows are skipped
        # unless --retry-deferred is set.
        if retry_deferred:
            sql = """
                SELECT q.id, q.question_text, q.options
                FROM questions q
                JOIN files f    ON f.id = q.file_ref_id
                JOIN books b    ON b.id = f.book_id
                JOIN chapters c ON c.id = b.chapter_id
                JOIN question_subtopics qs ON qs.question_id = q.id
                JOIN subtopics s ON s.id = qs.subtopic_id
                WHERE c.id = %s AND s.code = 'mixedqn'
                  AND q.canonical_id IS NULL
            """
        else:
            sql = """
                SELECT q.id, q.question_text, q.options
                FROM questions q
                JOIN files f    ON f.id = q.file_ref_id
                JOIN books b    ON b.id = f.book_id
                JOIN chapters c ON c.id = b.chapter_id
                JOIN question_subtopics qs ON qs.question_id = q.id
                JOIN subtopics s ON s.id = qs.subtopic_id
                WHERE c.id = %s AND s.code = 'mixedqn'
                  AND q.canonical_id IS NULL
                  AND qs.classified_by IS DISTINCT FROM 'deferred'
            """
        db.execute(sql, (ch_id,))
        rows = db.fetchall()
        if not rows:
            continue

        db.execute(
            "SELECT code, id FROM subtopics WHERE chapter_id = %s",
            (ch_id,),
        )
        code_to_id = dict(db.fetchall())

        print(f"  Ch {ch_key} ({ch_name}): {len(rows)} to classify")

        q_list = []
        for qid, qtext, opts in rows:
            if isinstance(opts, str):
                try:
                    opts = json.loads(opts)
                except Exception:
                    opts = []
            if not isinstance(opts, list):
                opts = []
            q_list.append((qid, qtext or "", opts))

        c, d = classify_chapter(
            pool, db, ch_name, subtopics,
            q_list, code_to_id, keyword_hints, chapter_key=ch_key,
        )
        total_classified += c
        total_deferred += d

    print(f"\n  {level_key}: {total_classified} classified, "
          f"{total_deferred} deferred this run")
    return total_classified, total_deferred


# ════════════════════════════════════════════════════════════════════════
# Self-test
# ════════════════════════════════════════════════════════════════════════

def _selftest():
    failures = 0

    def check(cond, msg):
        nonlocal failures
        if not cond:
            print(f"  FAIL: {msg}")
            failures += 1
        else:
            print(f"  ok:   {msg}")

    v, e = parse_response('{"1": "2.2"}')
    check(e is None and v == {"1": "2.2"}, "parse plain object")
    v, e = parse_response('```json\n{"1":"2.2"}\n```')
    check(e is None and v == {"1": "2.2"}, "parse fenced json")
    v, e = parse_response("not json")
    check(v is None and e is not None, "reject non-json")

    m, e = normalize_mapping({"answers": [{"q": 1, "code": "2.2", "conf": 0.9}]})
    check(e is None and m["1"] == ("2.2", 0.9), "mapping from answers list")

    codes = {"2.1", "2.2", "2.2.1"}
    t2c = {"cement": "2.2", "stone": "2.1"}
    check(normalize_code("2.2", codes, t2c) == "2.2", "code exact")
    check(normalize_code("2.2 Cement", codes, t2c) == "2.2", "code + title")
    check(normalize_code("nonsense", codes, t2c) == "mixedqn", "unknown->mixedqn")

    kind, _ = classify_error("429 retry in 30s")
    check(kind == "rate", "rate detected")
    kind, _ = classify_error("Invalid API Key")
    check(kind == "auth", "auth detected")
    kind, _ = classify_error("503 Service Unavailable")
    check(kind == "server", "server detected")

    hints = {"2.2": ["cement", "opc"], "9.9": ["unrelated"]}
    # With valid_codes filter, 9.9 is skipped
    code, conf = keyword_fallback("What is OPC cement?", ["a"], hints,
                                  {"2.1", "2.2"})
    check(code == "2.2", "keyword respects valid_codes")
    code, conf = keyword_fallback("What is OPC cement?", ["a"], hints,
                                  {"9.9"})
    check(code is None, "keyword abstains when no valid code matches")

    print()
    if failures:
        print(f"SELFTEST FAILED ({failures})")
        return 1
    print("SELFTEST PASSED")
    return 0


# ════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════

def main():
    global BATCH_SIZE, TEMPERATURE, MAX_WAIT_PER_BATCH, QUIET

    ap = argparse.ArgumentParser(
        description="Classify exam questions into subtopics via Gemini."
    )
    ap.add_argument("target", nargs="?",
                    help="level_key (level5|level7|gk) or 'all'")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--temperature", type=float, default=TEMPERATURE)
    ap.add_argument("--max-wait", type=int, default=MAX_WAIT_PER_BATCH)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--retry-deferred", action="store_true",
                    help="re-include rows previously marked deferred")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(_selftest())

    if not args.target:
        ap.print_help()
        sys.exit(1)

    BATCH_SIZE = args.batch_size
    TEMPERATURE = args.temperature
    MAX_WAIT_PER_BATCH = args.max_wait
    if args.verbose:
        QUIET = False
    elif args.quiet:
        QUIET = True

    levels = (["level5", "level7", "gk"]
              if args.target == "all" else [args.target])

    print(f"Run log dir: {_RUN_DIR}")
    print(f"Quiet mode: {'ON' if QUIET else 'OFF'}")
    print(f"Retry deferred: {args.retry_deferred}")
    print("Building provider pool ...")
    pool = build_provider_pool()
    if len(pool) == 0:
        print("\n[EXIT] No usable providers this run.")
        sys.exit(0)

    print(f"\nProvider pool ({len(pool)} entries, strongest first):")
    for i, p in enumerate(pool.providers, 1):
        print(f"  {i:>3}. [{p.strength}] {p.name}:{p.model}")
    print(f"Temperature: {TEMPERATURE}   Batch size: {BATCH_SIZE}   "
          f"Sleep: {SLEEP_BETWEEN}s   Max wait/batch: {MAX_WAIT_PER_BATCH}s")

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("\n[EXIT] DATABASE_URL not set.")
        sys.exit(1)

    db = DB(db_url)
    start = time.time()
    try:
        for lv in levels:
            try:
                process_level(lv, pool, db, args.retry_deferred)
            except KeyboardInterrupt:
                print("\n\nCtrl+C received. Progress saved.")
                break
            except Exception as e:
                print(f"\n[WARN] {lv} raised {type(e).__name__}: "
                      f"{str(e)[:180]}")
                # Continue to next level instead of killing the run
    finally:
        db.close()
        print(f"\nTotal time: {time.time() - start:.1f}s")
        print(f"Final pool status: {pool.status_line()}")
        print(pool.metrics_report())

    # Exit 0 so run_pipeline.py moves on to export/upload.
    sys.exit(0)


if __name__ == "__main__":
    main()