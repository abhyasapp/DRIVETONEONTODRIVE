#!/usr/bin/env python3
"""
export_any_level.py  (final)
----------------------------
Exports classified questions to JSON, one subtopic per folder.

Layout on disk:

    abhyas_export/<level label>/
        <Ch N - Chapter name>/
            <subtopic code>/
                <code> <title>.json            (single part)
                <code> <title> (part N).json   (multi-part)
                mixedqn.json                   (single part)
                mixedqn (N).json               (multi-part)
                <same>.meta.json               (sidecar, one per file)

Every file gets a .meta.json sidecar recording code, source_label,
part_number, and n_parts so the uploader never has to re-parse the
filename.

Usage:
    python export_any_level.py all
    python export_any_level.py level5
    python export_any_level.py all --dry-run
    python export_any_level.py all --include-unanswered
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time

import psycopg2
from dotenv import load_dotenv

load_dotenv()

OUTPUT_ROOT  = "abhyas_export"
MAX_PER_FILE = 50

_LEAD_NUM_RE  = re.compile(r"^\s*\d+\s*[\.\)]\s*")
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name):
    name = _UNSAFE_CHARS.sub("_", str(name))
    name = name.strip().strip(".")
    return name or "unnamed"


def strip_leading_number(text):
    return _LEAD_NUM_RE.sub("", str(text or "")).strip()


def build_question(new_number, q_text, options, correct_idx, explanation, images):
    body = strip_leading_number(q_text)
    item = {
        "q": f"{new_number}. {body}",
        "options": list(options or []),
        "correct": correct_idx if correct_idx is not None else 0,
        "explain": explanation or "",
    }
    if images:
        item["images"] = images
    return item


def atomic_write_json(path, payload):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def swap_dir(src, dst, attempts=6, delay=0.5):
    last_err = None
    for _ in range(attempts):
        try:
            if os.path.exists(dst):
                shutil.rmtree(dst)
            os.replace(src, dst)
            return
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(delay)
    raise last_err


def build_filename(code, title, part_number, n_parts):
    if code == "mixedqn":
        if n_parts > 1:
            return safe_filename(f"mixedqn ({part_number}).json")
        return safe_filename("mixedqn.json")
    base = f"{code} {title}"
    if n_parts > 1:
        base += f" (part {part_number})"
    return safe_filename(base + ".json")


def meta_path_for(json_path):
    return json_path[:-5] + ".meta.json"


# ── DB read ──────────────────────────────────────────────────────────────

def fetch_rows(cur, level_id, include_unanswered):
    where = "" if include_unanswered else "AND q.correct_index IS NOT NULL"
    cur.execute(f"""
        SELECT
            c.chapter_key, c.chapter_name, s.code, s.title,
            q.id, q.question_text, q.options, q.correct_index, q.explanation
        FROM questions q
        JOIN question_subtopics qs ON qs.question_id = q.id
        JOIN subtopics s           ON s.id           = qs.subtopic_id
        JOIN chapters c            ON c.id           = s.chapter_id
        WHERE c.level_id = %s
          AND q.canonical_id IS NULL
          {where}
        ORDER BY
            CASE WHEN c.chapter_key ~ '^[0-9]+(\\.[0-9]+)*$'
                 THEN c.chapter_key::numeric ELSE NULL END NULLS LAST,
            c.chapter_key,
            CASE WHEN s.code = 'mixedqn' THEN 1 ELSE 0 END,
            s.code,
            q.question_number NULLS LAST,
            q.id
    """, (level_id,))
    return cur.fetchall()


def fetch_images_for(cur, qids):
    if not qids:
        return {}
    out = {}
    CHUNK = 5000
    for i in range(0, len(qids), CHUNK):
        cur.execute("""
            SELECT question_id, mime_type, data_base64
            FROM question_images
            WHERE question_id = ANY(%s)
            ORDER BY question_id, position
        """, (qids[i:i + CHUNK],))
        for qid, mime, b64 in cur.fetchall():
            out.setdefault(qid, []).append(f"data:{mime};base64,{b64}")
    return out


def group_rows(rows):
    grouped = {}
    for (ch_key, ch_name, code, title, qid, q_text, options,
         correct_idx, explanation) in rows:
        ch = grouped.setdefault(ch_key, {"chapter_name": ch_name, "subs": {}})
        ch["subs"].setdefault(code, {"title": title, "items": []})
        ch["subs"][code]["items"].append(
            (qid, q_text, options, correct_idx, explanation)
        )
    return grouped


# ── export ───────────────────────────────────────────────────────────────

def export_level(cur, level_key, include_unanswered, dry_run):
    cur.execute("SELECT id, label FROM levels WHERE level_key = %s", (level_key,))
    row = cur.fetchone()
    if not row:
        print(f"  ERROR: level '{level_key}' not found.")
        return 0, 0
    level_id, label = row

    rows = fetch_rows(cur, level_id, include_unanswered)
    if not rows:
        print(f"  {level_key}: no questions to export")
        return 0, 0

    images_by_qid = fetch_images_for(cur, [r[4] for r in rows])
    grouped = group_rows(rows)

    if dry_run:
        print(f"  {level_key}: DRY-RUN")
        for ch_key in sorted(grouped, key=lambda x: (x.count("."), x)):
            ch = grouped[ch_key]
            for code in sorted(ch["subs"]):
                n = len(ch["subs"][code]["items"])
                if n:
                    parts = (n + MAX_PER_FILE - 1) // MAX_PER_FILE
                    print(f"      Ch {ch_key} / {code}: {n} q, {parts} file(s)")
        return 0, 0

    final_dir = os.path.join(OUTPUT_ROOT, safe_filename(label))
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    tmp_dir = os.path.join(OUTPUT_ROOT,
                           f".tmp_{safe_filename(label)}_{os.getpid()}")
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)

    files_written = q_written = 0

    try:
        for ch_key in sorted(grouped, key=lambda x: (x.count("."), x)):
            ch = grouped[ch_key]
            ch_dir = os.path.join(
                tmp_dir,
                safe_filename(f"{ch_key} - {ch['chapter_name']}"),
            )
            os.makedirs(ch_dir, exist_ok=True)

            for code in sorted(ch["subs"]):
                sub = ch["subs"][code]
                items = sub["items"]
                if not items:
                    continue
                chunks = [items[i:i + MAX_PER_FILE]
                          for i in range(0, len(items), MAX_PER_FILE)]
                n_parts = len(chunks)

                                # every subtopic gets its own folder: "<code> <title>"
                sub_dir_name = safe_filename(f"{code} {sub['title']}")
                sub_dir = os.path.join(ch_dir, sub_dir_name)
                os.makedirs(sub_dir, exist_ok=True)

                for part_idx, chunk in enumerate(chunks, 1):
                    payload = [
                        build_question(i, q_text, options, correct_idx,
                                       explain, images_by_qid.get(qid))
                        for i, (qid, q_text, options, correct_idx, explain)
                        in enumerate(chunk, 1)
                    ]
                    fname = build_filename(code, sub["title"],
                                           part_idx, n_parts)
                    json_path = os.path.join(sub_dir, fname)
                    atomic_write_json(json_path, payload)
                    atomic_write_json(meta_path_for(json_path), {
                        "code": code,
                        "title": sub["title"],
                        "source_label": None,
                        "part_number": part_idx,
                        "n_parts": n_parts,
                        "question_count": len(payload),
                    })
                    files_written += 1
                    q_written += len(payload)

        swap_dir(tmp_dir, final_dir)
    except Exception:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    print(f"  {level_key}: {files_written} file(s), {q_written} questions")
    return files_written, q_written


# ── CLI ──────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Export classified questions.")
    p.add_argument("target", help="level_key or 'all'")
    p.add_argument("--include-unanswered", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    levels = ["level5", "level7", "gk"] if args.target == "all" else [args.target]
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    print("Exporting to", os.path.abspath(OUTPUT_ROOT))
    if args.include_unanswered:
        print("Mode: include unanswered questions")
    if args.dry_run:
        print("Mode: DRY-RUN")

    start = time.time()
    total_files = total_q = 0
    for lv in levels:
        try:
            f, q = export_level(cur, lv, args.include_unanswered, args.dry_run)
            total_files += f
            total_q += q
        except Exception as e:
            print(f"  {lv}: FAILED — {type(e).__name__}: {e}")

    cur.close()
    conn.close()
    print()
    print(f"Total: {total_files} file(s), {total_q} question(s), "
          f"{time.time() - start:.1f}s")


if __name__ == "__main__":
    main()