"""
sync_any_level.py  (v6)
-----------------------
Robust incremental Google Drive → Neon sync.

Changes in v6:
  • New per-level flag `use_file_as_book` (default False).
      - level5 / level7: False → direct files go to `default_book`,
                                 subfolders are books (unchanged).
      - gk:             True  → a direct JSON file becomes its own
                                 book (file stem = book name);
                                 files inside a subfolder use the
                                 subfolder name as the book name.
    This makes GK show meaningful names in the app
    ("Constitution", "Citizen Charter", "INTERNATIONAL AFFAIRS", …)
    instead of a single generic "GATE" book.

  • GK folder_map now points to the new chapter folders:
        "chapter 1: gk"          → "1"
        "chapter 2: management"  → "2"
        "chapter 3: iq"          → "3"
  • GK root_file_map is empty — all files live inside chapter folders.

  • Path-fallback for rotated Drive file_ids: if a Drive file_id is not
    in the DB but its (chapter, book, subtopic) matches an existing row,
    it is treated as a change of that row instead of a new file. This
    prevents the "1 new / 1 stale prune" loop on files whose upload
    replaced them with a fresh Drive id.

Unchanged from earlier versions:
  • Moved files keep their old DB row until the new copy is safely in.
  • Progress logging inside walk_level — one line per chapter folder.
  • Case- and whitespace-insensitive book-name comparison.
  • Deleted Drive files removed from DB.
  • canonical_id cleared before question deletion.
  • md5Checksum primary, modifiedTime fallback.
  • Drive client cached per worker thread.
  • Batched commits (default 25 files).
  • Hard timeout on Drive API calls.
  • --dry-run / --workers / --batch flags.

Usage:
    python sync_any_level.py all
    python sync_any_level.py level5
    python sync_any_level.py gk
    python sync_any_level.py all --dry-run
    python sync_any_level.py level5 --workers 8 --batch 50
"""

import os
import io
import re
import sys
import json
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2
from psycopg2.extras import Json, execute_values

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError

from dotenv import load_dotenv


# ============================================================================
# CONFIG
# ============================================================================

load_dotenv()

SERVICE_ACCOUNT_FILE = "service-account.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

DEFAULT_WORKERS   = 6
DEFAULT_BATCH     = 25
DRIVE_TIMEOUT     = 60
MAX_WALK_DEPTH    = 6

STRIP_IMAGES_FROM_RAW = True


# ============================================================================
# LEVEL CONFIGURATION
# ============================================================================

LEVEL_CONFIG = {

    "level5": {
        "root": "188sR9OY8GO1ukjSaUWPaaAB4l3Fq0-Ck",
        "folder_map": {
            "Surveying":                           "1",
            "Construction Material":               "2",
            "Mechanics of material and structure": "3",
            "Hydraulics":                          "4",
            "Soil Mechanics":                      "5",
            "Structural Design":                   "6",
            "Building Construction Tech":          "7",
            "Water and Sanitation":                "8",
            "Irrigation":                          "9",
            "Highway":                             "10",
            "Estimating":                          "11",
            "C. management":                       "12",
            "airport":                             "13",
        },
        "default_book": "DPARSAD",
        "use_subfolder_as_book": True,   # subfolder = book
        "use_file_as_book":      False,  # direct files → default_book
        "root_file_map": {},
    },

    "gk": {
        "root": "1yQi7msSeY_4032n0XRJ8MFTtZeuHe8y2",
        "folder_map": {
            # Chapter folders now live directly under the GK root.
            # Inside each chapter folder, JSON files may be:
            #   • directly inside        → file stem becomes its own book
            #   • inside a subfolder    → subfolder name becomes the book,
            #                             file stem is the subtopic
            "chapter 1: gk":         "1",
            "CHAPTER 2: MANAGEMENT": "2",
            "chapter 3: iq":         "3",
        },
        "default_book": "GATE",          # unused when use_file_as_book=True
        "use_subfolder_as_book": True,   # subfolder = book
        "use_file_as_book":      True,   # GK: file stem = book for direct files
        "root_file_map": {},             # all files now inside chapter folders
    },

    "level7": {
        "root": "1vErXjvx2PAy7Q0d0mvYWCGuKjED42fwF",
        "folder_map": {
            "structtture":            "1",
            "survey":                 "2",
            "C  material":            "3",
            "concrete":               "4",
            "geotech":                "5",
            "construction managment": "6",
            "ESTIMATE":               "7",
            "drawing":                "8",
            "economics":              "9",
            "EPP":                    "10",
        },
        "default_book": "DPARSAD",
        "use_subfolder_as_book": True,
        "use_file_as_book":      False,
        "root_file_map": {},
    },
}


# ============================================================================
# PARSING CONSTANTS
# ============================================================================

DATA_URI_RE = re.compile(r"^data:([\w/+.-]+);base64,(.+)$", re.DOTALL)
QNUM_RE     = re.compile(r"^\s*(\d+)\s*[\.\)]\s*(.*)$", re.DOTALL)
BAD_ESCAPE  = re.compile(r"\\(?![\\\"/bfnrtu])")

IMAGE_KEYS = [
    "figure", "image", "img", "picture", "pic",
    "images", "imgs", "diagram", "image_url",
]
EXPLAIN_KEYS = ["explanation", "explain", "reason", "solution"]


# ============================================================================
# SMALL HELPERS
# ============================================================================

def norm_name(s):
    """Case- and whitespace-insensitive key for matching Drive names."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def with_timeout(fn, timeout):
    result = {"value": None, "error": None, "done": False}

    def _run():
        try:
            result["value"] = fn()
        except BaseException as e:
            result["error"] = e
        finally:
            result["done"] = True

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if not result["done"]:
        raise TimeoutError(f"operation timed out after {timeout}s")
    if result["error"]:
        raise result["error"]
    return result["value"]


# ============================================================================
# GOOGLE DRIVE
# ============================================================================

_drive_cache = {}
_drive_lock = threading.Lock()


def make_drive():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def thread_drive():
    tid = threading.get_ident()
    with _drive_lock:
        if tid not in _drive_cache:
            _drive_cache[tid] = make_drive()
        return _drive_cache[tid]


def is_folder(item):
    return item["mimeType"] == "application/vnd.google-apps.folder"


def list_children(drive, folder_id):
    items, token = [], None
    while True:
        def _call():
            return drive.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=("nextPageToken, files(id, name, mimeType, "
                        "modifiedTime, md5Checksum)"),
                pageSize=200,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(num_retries=3)

        response = with_timeout(_call, DRIVE_TIMEOUT)
        items.extend(response.get("files", []))
        token = response.get("nextPageToken")
        if not token:
            break
    return items


def walk_json_recursive(drive, folder_id, prefix="", depth=0):
    if depth > MAX_WALK_DEPTH:
        print(f"    [WARN] max walk depth {MAX_WALK_DEPTH} reached at "
              f"folder {folder_id}")
        return

    for item in list_children(drive, folder_id):
        if is_folder(item):
            sub_prefix = (prefix + item["name"] + "/") if prefix \
                         else (item["name"] + "/")
            yield from walk_json_recursive(drive, item["id"], sub_prefix, depth + 1)
        elif item["name"].lower().endswith(".json"):
            stem = item["name"][:-5]
            yield item, prefix + stem


def walk_level(drive, level_key):
    """
    Yield one dict per JSON file.

    Behaviour is driven by three per-level flags:

      use_subfolder_as_book = True
          Each subfolder inside a chapter folder becomes a `book`.
          Subtopic is the file's relative path inside that subfolder.
          (level5, level7, gk all use this for grouped files.)

      use_file_as_book = True
          Only set for GK. A direct JSON file inside a chapter folder
          becomes its own book: book = subtopic = file stem. This is
          what makes GK show "Constitution", "Citizen Charter", etc.
          in the app instead of a single generic "GATE" book.

      use_file_as_book = False  (level5, level7)
          Direct JSON files are placed under `default_book`
          ("DPARSAD" for level5/level7), subtopic = file stem.
    """
    cfg = LEVEL_CONFIG[level_key]
    norm_folder_map = {norm_name(k): v for k, v in cfg["folder_map"].items()}
    norm_root_map   = {norm_name(k): v for k, v in cfg.get("root_file_map", {}).items()}
    default_book    = cfg["default_book"]
    use_subfolder_as_book = cfg.get("use_subfolder_as_book", True)
    use_file_as_book      = cfg.get("use_file_as_book", False)

    print(f"    listing root: {cfg['root']} ...")
    root_children = list_children(drive, cfg["root"])

    folders_at_root = [x for x in root_children if is_folder(x)]
    files_at_root   = [x for x in root_children if not is_folder(x)]
    print(f"    root: {len(folders_at_root)} folder(s), "
          f"{len(files_at_root)} file(s)")

    for idx, item in enumerate(root_children, 1):
        # ------------------------------------------------------------------
        # Chapter folder
        # ------------------------------------------------------------------
        if is_folder(item):
            ch_key = norm_folder_map.get(norm_name(item["name"]))
            if not ch_key:
                print(f"    [WARN] unmapped chapter folder: {item['name']!r}")
                continue

            print(f"    [{idx}/{len(root_children)}] chapter "
                  f"{item['name']!r} → ch_key={ch_key}")

            subs = list_children(drive, item["id"])
            direct = [x for x in subs
                      if not is_folder(x) and x["name"].lower().endswith(".json")]
            subfolders = [x for x in subs if is_folder(x)]

            print(f"        {len(direct)} direct JSON, "
                  f"{len(subfolders)} subfolder(s)")

            # -- direct JSON files inside the chapter folder -----------------
            for f in direct:
                stem = f["name"][:-5]
                if use_file_as_book:
                    # GK: file stem is its own book
                    book     = stem
                    subtopic = stem
                else:
                    # level5 / level7: direct files fall under default_book
                    book     = default_book
                    subtopic = stem
                yield {
                    "chapter_key": ch_key,
                    "book":        book,
                    "subtopic":    subtopic,
                    "file_id":     f["id"],
                    "modified_time": f.get("modifiedTime"),
                    "md5":           f.get("md5Checksum"),
                }

            # -- files inside subfolders ------------------------------------
            for folder in subfolders:
                if use_subfolder_as_book:
                    # subfolder name = book (level5, level7, gk)
                    book = folder["name"]
                    n_files = 0
                    for f, subtopic in walk_json_recursive(drive, folder["id"]):
                        n_files += 1
                        yield {
                            "chapter_key": ch_key,
                            "book":        book,
                            "subtopic":    subtopic,
                            "file_id":     f["id"],
                            "modified_time": f.get("modifiedTime"),
                            "md5":           f.get("md5Checksum"),
                        }
                    print(f"        book {book!r}: {n_files} JSON file(s)")
                else:
                    # flatten (unused for current levels, kept for completeness)
                    n_files = 0
                    for f, subtopic in walk_json_recursive(
                        drive, folder["id"], prefix=folder["name"] + "/"
                    ):
                        n_files += 1
                        yield {
                            "chapter_key": ch_key,
                            "book":        default_book,
                            "subtopic":    subtopic,
                            "file_id":     f["id"],
                            "modified_time": f.get("modifiedTime"),
                            "md5":           f.get("md5Checksum"),
                        }
                    print(f"        subfolder {folder['name']!r}: "
                          f"{n_files} JSON file(s) (flattened)")

        # ------------------------------------------------------------------
        # Root JSON (only for levels that use root_file_map)
        # ------------------------------------------------------------------
        elif item["name"].lower().endswith(".json"):
            ch_key = norm_root_map.get(norm_name(item["name"]))
            if not ch_key:
                print(f"    [WARN] unmapped root JSON: {item['name']!r}")
                continue
            yield {
                "chapter_key": ch_key,
                "book":        default_book,
                "subtopic":    item["name"][:-5],
                "file_id":     item["id"],
                "modified_time": item.get("modifiedTime"),
                "md5":           item.get("md5Checksum"),
            }


def download_parse(drive, file_id):
    def _do():
        request = drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)
        return fh.read().decode("utf-8")

    text = with_timeout(_do, DRIVE_TIMEOUT * 3)
    return parse_json_lenient(text)


# ============================================================================
# TIMESTAMPS / CHANGE DETECTION
# ============================================================================

def normalize_timestamp(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def timestamps_differ(old_value, new_value, tolerance_seconds=1):
    old_dt = normalize_timestamp(old_value)
    new_dt = normalize_timestamp(new_value)
    if old_dt is None or new_dt is None:
        return old_dt != new_dt
    return abs((new_dt - old_dt).total_seconds()) > tolerance_seconds


def content_changed(db_md5, db_mtime, drive_md5, drive_mtime):
    if db_md5 and drive_md5:
        return db_md5 != drive_md5
    return timestamps_differ(db_mtime, drive_mtime)


# ============================================================================
# JSON PARSING
# ============================================================================

def extract_num_text(raw):
    raw = str(raw).strip()
    m = QNUM_RE.match(raw)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, raw


def split_data_uri(value):
    if not isinstance(value, str):
        return None, None
    s = value.strip()
    m = DATA_URI_RE.match(s)
    if m:
        return m.group(1), m.group(2)
    if len(s) > 200 and re.match(r"^[A-Za-z0-9+/=\s]+$", s):
        head = s[:20]
        if head.startswith("iVBOR"): return "image/png", s
        if head.startswith("/9j/"):  return "image/jpeg", s
        return "application/octet-stream", s
    return None, None


def parse_json_lenient(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(BAD_ESCAPE.sub(r"\\\\", text))


# ============================================================================
# SCHEMA
# ============================================================================

def ensure_schema(cur, conn):
    stmts = [
        "ALTER TABLE files ADD COLUMN IF NOT EXISTS drive_modified_time TIMESTAMPTZ",
        "ALTER TABLE files ADD COLUMN IF NOT EXISTS drive_md5 TEXT",
        "ALTER TABLE files ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS idx_files_file_id ON files(file_id)",
    ]
    for s in stmts:
        cur.execute(s)
    conn.commit()


# ============================================================================
# DB HELPERS
# ============================================================================

def delete_questions_for_file(cur, file_ref_id):
    cur.execute("""
        UPDATE questions SET canonical_id = NULL
        WHERE canonical_id IN (
            SELECT id FROM questions WHERE file_ref_id = %s
        )
    """, (file_ref_id,))
    cur.execute("""
        DELETE FROM question_images
        WHERE question_id IN (
            SELECT id FROM questions WHERE file_ref_id = %s
        )
    """, (file_ref_id,))
    cur.execute("DELETE FROM questions WHERE file_ref_id = %s", (file_ref_id,))


def hard_delete_file(cur, file_ref_id):
    delete_questions_for_file(cur, file_ref_id)
    cur.execute("DELETE FROM files WHERE id = %s", (file_ref_id,))


def ensure_book(cur, chapter_id, book_name):
    cur.execute("""
        INSERT INTO books (chapter_id, book_name)
        VALUES (%s, %s)
        ON CONFLICT (chapter_id, book_name)
        DO UPDATE SET book_name = EXCLUDED.book_name
        RETURNING id
    """, (chapter_id, book_name))
    return cur.fetchone()[0]


def ensure_file(cur, book_id, subtopic, drive_file_id, mtime, md5):
    """
    Upsert a files row. last_synced_at only advances when the file's
    content actually changed on Drive — otherwise it's preserved.
    """
    cur.execute("""
        INSERT INTO files
            (book_id, subtopic, file_id, drive_modified_time, drive_md5,
             last_synced_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (book_id, subtopic)
        DO UPDATE SET
            file_id             = EXCLUDED.file_id,
            drive_modified_time = EXCLUDED.drive_modified_time,
            drive_md5           = EXCLUDED.drive_md5,
            last_synced_at      = CASE
                WHEN files.drive_md5 IS DISTINCT FROM EXCLUDED.drive_md5
                THEN NOW()
                ELSE files.last_synced_at
            END
        RETURNING id
    """, (book_id, subtopic, drive_file_id, mtime, md5))
    return cur.fetchone()[0]


def _strip_images_from_raw(item):
    if not STRIP_IMAGES_FROM_RAW:
        return item
    clean, n = {}, 0
    for k, v in item.items():
        if k.lower() in IMAGE_KEYS:
            vals = v if isinstance(v, list) else [v]
            for val in vals:
                mime, b64 = split_data_uri(val)
                if mime and b64:
                    n += 1
            clean[k] = None
        else:
            clean[k] = v
    if n:
        clean["_images_stripped"] = n
    return clean


def insert_questions(cur, file_ref_id, data):
    if isinstance(data, dict):
        for key in ("questions", "data", "items"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        else:
            raise ValueError("dict without question list")
    if not isinstance(data, list):
        raise ValueError("not a list")

    delete_questions_for_file(cur, file_ref_id)

    q_rows = []
    for item in data:
        if not isinstance(item, dict):
            continue

        q_num, q_text = extract_num_text(item.get("q", ""))
        options = item.get("options", []) or []
        correct_idx = item.get("correct")
        if not isinstance(correct_idx, int) or not (0 <= correct_idx < len(options)):
            correct_idx = None
        correct_txt = options[correct_idx] if correct_idx is not None else None

        explain = next(
            (item[k] for k in EXPLAIN_KEYS if k in item and item[k]), None
        )

        images = []
        for key in item.keys():
            if key.lower() not in IMAGE_KEYS:
                continue
            values = item[key] if isinstance(item[key], list) else [item[key]]
            for value in values:
                mime, b64 = split_data_uri(value)
                if mime and b64:
                    images.append((mime, b64))

        raw_clean = _strip_images_from_raw(item)

        q_rows.append((
            file_ref_id, q_num, q_text, Json(options),
            correct_idx, correct_txt, explain, Json(raw_clean),
            images,
        ))

    if not q_rows:
        return 0, 0

    values = [row[:8] for row in q_rows]
    result = execute_values(
        cur,
        """
        INSERT INTO questions
            (file_ref_id, question_number, question_text, options,
             correct_index, correct_text, explanation, raw)
        VALUES %s
        RETURNING id
        """,
        values, page_size=500, fetch=True,
    )
    qids = [row[0] for row in result]

    image_rows = []
    for qid, row in zip(qids, q_rows):
        for pos, (mime, b64) in enumerate(row[8]):
            image_rows.append((qid, mime, b64, pos))
    if image_rows:
        execute_values(
            cur,
            """
            INSERT INTO question_images
                (question_id, mime_type, data_base64, position)
            VALUES %s
            """,
            image_rows, page_size=500,
        )

    return len(q_rows), len(image_rows)


# ============================================================================
# DOWNLOAD TASK
# ============================================================================

def download_task(entry):
    try:
        drive = thread_drive()
        data = download_parse(drive, entry["file_id"])
        return entry, data, None
    except HttpError as e:
        return entry, None, f"HTTP {getattr(e, 'status_code', '?')}: {e}"
    except TimeoutError as e:
        return entry, None, f"timeout: {e}"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return entry, None, f"parse error: {e}"


# ============================================================================
# SYNC ONE LEVEL
# ============================================================================

def sync_level(level_key, workers=DEFAULT_WORKERS, batch_size=DEFAULT_BATCH,
               dry_run=False):
    print(f"\n=== Syncing {level_key} ===")

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    ensure_schema(cur, conn)

    cur.execute("SELECT id FROM levels WHERE level_key = %s", (level_key,))
    row = cur.fetchone()
    if not row:
        print("  ERROR: level not found")
        cur.close(); conn.close()
        return
    level_id = row[0]

    cur.execute("""
        SELECT f.id, f.file_id, f.book_id, f.subtopic,
               f.drive_modified_time, f.drive_md5,
               b.book_name, c.chapter_key
        FROM files f
        JOIN books b    ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id
        WHERE c.level_id = %s
    """, (level_id,))
    known = {}
    for (file_ref_id, drive_id, book_id, subtopic, mtime, md5,
         book_name, chapter_key) in cur.fetchall():
        known[drive_id] = {
            "file_ref_id": file_ref_id,
            "book_id":     book_id,
            "book_name":   book_name,
            "chapter_key": chapter_key,
            "subtopic":    subtopic,
            "mtime":       mtime,
            "md5":         md5,
        }

    # Secondary index used when a Drive file_id was rotated by re-upload.
    known_by_path = {}
    for db in known.values():
        path_key = (db["chapter_key"],
                    norm_name(db["book_name"]),
                    db["subtopic"])
        known_by_path[path_key] = db

    cur.execute("SELECT chapter_key, id FROM chapters WHERE level_id = %s",
                (level_id,))
    chapter_ids = dict(cur.fetchall())

    print("  scanning Drive ...")
    drive = make_drive()

    live = []
    matched_refs = set()
    to_add, to_move, to_change = [], [], []

    for entry in walk_level(drive, level_key):
        live.append(entry)
        fid = entry["file_id"]
        db = known.get(fid)

        if db is None:
            # Try to match by (chapter, book, subtopic) — handles a
            # rotated Drive file_id from a re-upload.
            path_key = (entry["chapter_key"],
                        norm_name(entry["book"]),
                        entry["subtopic"])
            db = known_by_path.get(path_key)
            if db is not None:
                matched_refs.add(db["file_ref_id"])
                to_change.append((entry, db))
                continue
            to_add.append(entry)
            continue

        matched_refs.add(db["file_ref_id"])

        same_location = (
            db["chapter_key"] == entry["chapter_key"]
            and norm_name(db["book_name"]) == norm_name(entry["book"])
            and db["subtopic"] == entry["subtopic"]
        )
        if not same_location:
            to_move.append((entry, db))
            continue

        if content_changed(db["md5"], db["mtime"],
                           entry["md5"], entry["modified_time"]):
            to_change.append((entry, db))

    deleted_refs = [
        db["file_ref_id"] for db in known.values()
        if db["file_ref_id"] not in matched_refs
    ]

    moved_old_ref_by_drive_id = {
        e["file_id"]: db["file_ref_id"] for e, db in to_move
    }

    print(f"  {len(to_add)} new | "
          f"{len(to_change)} changed | "
          f"{len(to_move)} moved | "
          f"{len(deleted_refs)} deleted from Drive")

    if dry_run:
        print("\n  [DRY-RUN] No changes will be applied.\n")
        for entry in to_add:
            print(f"    + {entry['chapter_key']} / {entry['book']} / "
                  f"{entry['subtopic']}")
        for entry, db in to_change:
            print(f"    ~ {entry['chapter_key']} / {entry['book']} / "
                  f"{entry['subtopic']}")
        for entry, db in to_move:
            print(f"    > {db['chapter_key']}/{db['book_name']}/{db['subtopic']}"
                  f"  →  {entry['chapter_key']}/{entry['book']}/{entry['subtopic']}")
        for ref in deleted_refs:
            print(f"    - files.id = {ref}")
        cur.close(); conn.close()
        return

    if not (to_add or to_change or to_move or deleted_refs):
        print(f"  {level_key}: already up to date")
        cur.close(); conn.close()
        return

    for ref in deleted_refs:
        hard_delete_file(cur, ref)
    conn.commit()

    to_download = to_add + [e for e, _ in to_move] + [e for e, _ in to_change]

    results = []
    if to_download:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(download_task, e) for e in to_download]
            done = 0
            for fut in as_completed(futures):
                results.append(fut.result())
                done += 1
                if done % 20 == 0 or done == len(to_download):
                    print(f"    downloaded {done}/{len(to_download)}")

    total_q = total_img = 0
    successful = 0
    failures = []
    pending_since_commit = 0

    for idx, (entry, data, err) in enumerate(results, 1):
        label = f"{entry['chapter_key']}/{entry['book']}/{entry['subtopic']}"

        if err:
            print(f"    [ERROR] {label}: {err}")
            failures.append((label, err))
            continue

        chapter_id = chapter_ids.get(entry["chapter_key"])
        if not chapter_id:
            print(f"    [WARN] unknown chapter {entry['chapter_key']} — "
                  f"skipping {label}")
            failures.append((label, "unknown chapter"))
            continue

        try:
            book_id = ensure_book(cur, chapter_id, entry["book"])
            file_ref_id = ensure_file(
                cur, book_id, entry["subtopic"],
                entry["file_id"], entry["modified_time"], entry["md5"],
            )
            q, img = insert_questions(cur, file_ref_id, data)

            old_ref = moved_old_ref_by_drive_id.get(entry["file_id"])
            if old_ref is not None and old_ref != file_ref_id:
                hard_delete_file(cur, old_ref)

            total_q += q
            total_img += img
            successful += 1
            pending_since_commit += 1

            if pending_since_commit >= batch_size:
                conn.commit()
                pending_since_commit = 0

            print(f"    [{idx}/{len(results)}] {label}: {q} q, {img} img")

        except Exception as exc:
            conn.rollback()
            pending_since_commit = 0
            print(f"    [ERROR] insert {label}: {exc}")
            failures.append((label, str(exc)))

    if pending_since_commit:
        conn.commit()

    print()
    print(f"  {level_key}: "
          f"{len(to_add)} new, {len(to_change)} changed, "
          f"{len(to_move)} moved, {len(deleted_refs)} deleted")
    print(f"  {level_key}: {total_q} questions, {total_img} images synced")
    print(f"  {level_key}: {successful}/{len(to_download)} files processed, "
          f"{len(failures)} failures")
    for label, err in failures:
        print(f"    ! {label}: {err}")

    cur.close()
    conn.close()


# ============================================================================
# MAIN
# ============================================================================

def parse_args(argv):
    if len(argv) < 2:
        print("Usage: python sync_any_level.py <level_key|all> "
              "[--dry-run] [--workers N] [--batch N]")
        sys.exit(1)

    target = argv[1].lower()
    dry_run = "--dry-run" in argv
    workers = DEFAULT_WORKERS
    batch   = DEFAULT_BATCH

    if "--workers" in argv:
        i = argv.index("--workers")
        if i + 1 < len(argv):
            workers = int(argv[i + 1])
    if "--batch" in argv:
        i = argv.index("--batch")
        if i + 1 < len(argv):
            batch = int(argv[i + 1])

    if target == "all":
        levels = ["level5", "level7", "gk"]
    elif target in LEVEL_CONFIG:
        levels = [target]
    else:
        print(f"ERROR: unknown level '{target}'")
        print("Available: level5, level7, gk, all")
        sys.exit(1)

    return levels, workers, batch, dry_run


def main():
    levels, workers, batch, dry_run = parse_args(sys.argv)
    for level in levels:
        try:
            sync_level(level, workers=workers, batch_size=batch, dry_run=dry_run)
        except Exception as exc:
            print(f"\nERROR syncing {level}: {exc}")


if __name__ == "__main__":
    main()