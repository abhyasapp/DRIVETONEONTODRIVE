#!/usr/bin/env python3
"""
generate_chapters_data_all.py  (v8)
-----------------------------------
Reads the DB and writes abhyas_export/chapters-data.js.

Every chapter ends with TWO extra subtopics:

    <prefix>.<N+1> Mixed Questions    — pooled unclassified, 50q parts
    <prefix>.<N+2> Original Questions — every unique source file

Changes since v7:
  • Original Questions labels are now meaningful:
      – L5 / L7 :  "BOOK.RANGE"           e.g. "DPARSAD.1-100"
      – GK      :  the filename stem      e.g. "constitution"
    No more "DPARSAD 1", "DPARSAD 2", "GATE 1", … running indices.
  • GK groups by *file* (topic) instead of by the meaningless "GATE" book.

The ChapterData accessor API at the bottom is preserved unchanged.
"""

import os
import re
import json
import psycopg2
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()


# ════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════

def next_two_prefixes(real_codes):
    """
    Given real subtopic codes for a chapter (e.g. ['3.1','3.2',…,'3.6']),
    return the two codes Mixed Questions and Original Questions should
    use. Example: ['3.1'…'3.6'] -> ('3.7', '3.8'). Returns (None, None)
    for a chapter with no real subtopics.
    """
    parsed = []
    for c in real_codes:
        try:
            nums = [int(p) for p in c.split(".")]
            parsed.append(nums)
        except ValueError:
            continue
    if not parsed:
        return None, None
    prefix   = parsed[0][:-1]
    max_last = max(p[-1] for p in parsed)
    if not prefix:
        mp = str(max_last + 1)
        op = str(max_last + 2)
    else:
        mp = ".".join(str(x) for x in prefix + [max_last + 1])
        op = ".".join(str(x) for x in prefix + [max_last + 2])
    return mp, op


def canon_book(name):
    """
    Map messy book_name values onto a small set of canonical labels.

    DB reality: 'DPARSAD', 'dparsad', 'D PARSAD', 'D parsad old book',
    'D PARSAD (new)' all mean the same book. Same for 'RK SHRESTHA' vs
    'Rk shrestha', 'Sunil Sah' vs 'sunil' vs 'Sunil', etc.

    Anything not recognized is returned as-is (stripped).
    """
    n = (name or "").strip().lower()
    n = n.replace(".", " ").replace("_", " ").replace("-", " ")
    n = " ".join(n.split())

    if "parsad" in n or "prasad" in n:
        return "DPARSAD"
    if "shrestha" in n or "sherestha" in n:
        return "RK SHRESTHA"
    if "sunil" in n:
        return "Sunil Sah"
    if "gate" in n:
        return "GATE"
    if "saarc" in n:
        return "SAARC"
    return name.strip() if name else "unknown"


# ── Original-Questions label helpers ───────────────────────────────────
# Matches "1-100", "1 – 100", "1 to 100", "1_100", "Q1–100" …
_RANGE_RE = re.compile(r"(\d+)\s*(?:[-–—]|to|_)\s*(\d+)\b")


def _range_of(s):
    """'1-100' / '1 to 100' / 'Sunil 1_100.pdf'  ->  '1-100' (or None)."""
    m = _RANGE_RE.search(s or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _stem(s):
    """Drop any nested-folder prefix and any file extension."""
    s = (s or "").rsplit("/", 1)[-1].strip()
    if s.lower().endswith(".json"):
        s = s[:-5]
    return s


def label_book(book, subtopic):
    """
    L5 / L7  ->  'BOOK.RANGE'
        ('DPARSAD',  '1-100')               -> 'DPARSAD.1-100'
        ('Sunil Sah','101-200')             -> 'Sunil Sah.101-200'
        ('DPARSAD',  'Ch1 Qn 51-75 extra')  -> 'DPARSAD.51-75'
        ('DPARSAD',  'no range here')       -> 'DPARSAD'
    """
    rng = _range_of(subtopic)
    return f"{book}.{rng}" if rng else (book or "unknown")


def label_gk(subtopic):
    """
    GK  ->  the topic (already the filename stem). Folder prefix removed.

        'constitution'                     -> 'constitution'
        'international relations 101-150'  -> 'international relations 101-150'
        'Book A/Current Plan'              -> 'Current Plan'
        ''                                 -> 'General'
    """
    s = _stem(subtopic)
    return s or "General"


def order_key(ch_key):
    """Sort chapters numerically when possible."""
    try:
        return (0, [int(p) for p in ch_key.split(".")])
    except ValueError:
        return (1, ch_key)


# ════════════════════════════════════════════════════════════════════════
# main build
# ════════════════════════════════════════════════════════════════════════

cur.execute("""
    SELECT level_key, label FROM levels
    WHERE level_key IN ('level5','level7','gk')
    ORDER BY level_key
""")
levels = cur.fetchall()

CH_NAMES     = {}
LEVEL_LABELS = {}
DRIVE        = {}

for level_key, label in levels:
    LEVEL_LABELS[level_key] = label
    CH_NAMES[level_key]     = {}
    DRIVE[level_key]        = {}

    # ── chapters ──
    cur.execute("""
        SELECT chapter_key, chapter_name FROM chapters
        WHERE level_id = (SELECT id FROM levels WHERE level_key = %s)
        ORDER BY chapter_key
    """, (level_key,))
    ch_rows = sorted(cur.fetchall(), key=lambda r: order_key(r[0]))
    for ch_key, ch_name in ch_rows:
        CH_NAMES[level_key][ch_key] = ch_name
        DRIVE[level_key][ch_key]    = {}

    # ── real subtopic codes per chapter ──
    cur.execute("""
        SELECT c.chapter_key, s.code
        FROM subtopics s JOIN chapters c ON c.id = s.chapter_id
        WHERE c.level_id = (SELECT id FROM levels WHERE level_key = %s)
          AND s.code <> 'mixedqn'
    """, (level_key,))
    codes_by_chapter = {}
    for ch_key, code in cur.fetchall():
        codes_by_chapter.setdefault(ch_key, []).append(code)

    # ── classified exports (non-mixedqn) ──
    cur.execute("""
        SELECT c.chapter_key, s.code, s.title, ef.part_number, ef.drive_file_id
        FROM export_files ef
        JOIN subtopics s ON s.id = ef.subtopic_id
        JOIN chapters c  ON c.id = s.chapter_id
        WHERE c.level_id = (SELECT id FROM levels WHERE level_key = %s)
          AND s.code <> 'mixedqn'
        ORDER BY c.chapter_key, s.code, ef.part_number
    """, (level_key,))
    classified_rows = cur.fetchall()

    # ── mixedqn exports ──
    cur.execute("""
        SELECT c.chapter_key, ef.part_number, ef.drive_file_id
        FROM export_files ef
        JOIN subtopics s ON s.id = ef.subtopic_id
        JOIN chapters c  ON c.id = s.chapter_id
        WHERE c.level_id = (SELECT id FROM levels WHERE level_key = %s)
          AND s.code = 'mixedqn'
        ORDER BY c.chapter_key, ef.part_number
    """, (level_key,))
    mixed_rows = cur.fetchall()

    # ── source files (for Original Questions) ──
    cur.execute("""
        SELECT c.chapter_key, b.book_name, f.subtopic, f.file_id
        FROM files f
        JOIN books b    ON b.id = f.book_id
        JOIN chapters c ON c.id = b.chapter_id
        WHERE c.level_id = (SELECT id FROM levels WHERE level_key = %s)
        ORDER BY c.chapter_key, b.book_name, f.subtopic
    """, (level_key,))
    source_rows = cur.fetchall()

    # ─────────────────────────────────────────────────────────────────
    # 1) classified subtopics — "All" or "part N"
    # ─────────────────────────────────────────────────────────────────
    for ch_key, code, title, part, fid in classified_rows:
        sub_label = f"{code} {title}"
        bucket = DRIVE[level_key][ch_key].setdefault(sub_label, {})
        bucket[f"part {part}"] = fid

    for ch_key in DRIVE[level_key]:
        for sub_label, bucket in list(DRIVE[level_key][ch_key].items()):
            if len(bucket) == 1 and "part 1" in bucket:
                DRIVE[level_key][ch_key][sub_label] = {"All": bucket["part 1"]}

    # ─────────────────────────────────────────────────────────────────
    # 2) Mixed Questions — parts 1..N, skip if empty
    # ─────────────────────────────────────────────────────────────────
    mixed_by_chapter = {}
    for ch_key, part, fid in mixed_rows:
        mixed_by_chapter.setdefault(ch_key, []).append((part, fid))

    for ch_key in DRIVE[level_key]:
        parts = sorted(mixed_by_chapter.get(ch_key, []))
        if not parts:
            continue                              # skip empties

        mp, _op = next_two_prefixes(codes_by_chapter.get(ch_key, []))
        sub_label = "Mixed Questions" if mp is None else f"{mp} Mixed Questions"

        DRIVE[level_key][ch_key][sub_label] = {
            f"part {part}": fid for part, fid in parts
        }

    # ─────────────────────────────────────────────────────────────────
    # 3) Original Questions
    #
    #    L5 / L7 :  group by canonical book; label = "BOOK.RANGE"
    #    GK      :  group by file (topic);   label = topic name
    #    Dedupe by Drive file_id so the same file appearing under two
    #    subtopic labels collapses to one entry. Skip empty chapters.
    # ─────────────────────────────────────────────────────────────────
    originals_by_chapter = {}
    for ch_key, book_name, file_subtopic, fid in source_rows:
        if level_key == "gk":
            # GK's book is always "GATE" — meaningless. Group by topic.
            group = _stem(file_subtopic) or "General"
        else:
            group = canon_book(book_name)

        originals_by_chapter \
            .setdefault(ch_key, {}) \
            .setdefault(group, []) \
            .append((file_subtopic or "", fid))

    for ch_key in DRIVE[level_key]:
        book_map = originals_by_chapter.get(ch_key, {})
        if not book_map:
            continue                              # skip empties

        _mp, op = next_two_prefixes(codes_by_chapter.get(ch_key, []))
        sub_label = "Original Questions" if op is None else f"{op} Original Questions"

        bucket = {}
        for group in sorted(book_map.keys(), key=lambda s: s.lower()):
            items = book_map[group]

            # Dedupe by Drive file_id
            seen_fids = set()
            unique = []
            for sub, fid in sorted(items, key=lambda t: (t[0] or "").lower()):
                if fid in seen_fids:
                    continue
                seen_fids.add(fid)
                unique.append((sub, fid))

            if not unique:
                continue

            for sub, fid in unique:
                if level_key == "gk":
                    label = label_gk(sub)
                else:
                    label = label_book(group, sub)

                base = label
                n = 2
                while label in bucket:
                    label = f"{base} ({n})"
                    n += 1
                bucket[label] = fid

        if bucket:
            DRIVE[level_key][ch_key][sub_label] = bucket


# ════════════════════════════════════════════════════════════════════════
# emit
# ════════════════════════════════════════════════════════════════════════

def to_js(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


CHAPTERDATA_BLOCK = r"""
/* ══════════════════════════════════════════════════════════════════════
   CHAPTERDATA — accessor API used by app.js / objective.js / admin.html.
   Signatures unchanged from the previous version.
   ══════════════════════════════════════════════════════════════════════ */
(function(){
'use strict';

function _levels(){ return Object.keys(LEVEL_LABELS); }
function levelLabel(lv){ return LEVEL_LABELS[lv] || lv; }
function chapters(lv){ return CH_NAMES[lv] || {}; }
function chapterName(lv, ch){
  return (CH_NAMES[lv] && CH_NAMES[lv][String(ch)]) || `Chapter ${ch}`;
}
function books(lv, ch){
  return (DRIVE[lv] && DRIVE[lv][String(ch)]) || {};
}
function files(lv, ch, sub){
  return books(lv, ch)[sub] || {};
}

function chapterFileRefs(lv, ch){
  const out     = [];
  const chSubs  = books(lv, ch);
  const chLabel = chapterName(lv, ch);
  Object.keys(chSubs).forEach(subLabel => {
    const fm = chSubs[subLabel] || {};
    Object.keys(fm).forEach(fileLabel => {
      const fid = fm[fileLabel];
      if (!fid) return;
      out.push({
        lv:       String(lv),
        ch:       String(ch),
        book:     subLabel,
        subtopic: fileLabel,
        fid:      String(fid),
        key:      `${lv}_${ch}_${subLabel}_${fileLabel}`,
        name:     `${chLabel} — ${subLabel} — ${fileLabel}`
      });
    });
  });
  return out;
}

function allFileRefs(){
  const out = [];
  _levels().forEach(lv => {
    Object.keys(CH_NAMES[lv] || {}).forEach(ch => {
      out.push(...chapterFileRefs(lv, ch));
    });
  });
  return out;
}

function fileCount(lv, ch, sub){
  if (!lv){
    let t = 0;
    _levels().forEach(l => {
      Object.keys(CH_NAMES[l] || {}).forEach(c => {
        t += chapterFileRefs(l, c).length;
      });
    });
    return t;
  }
  if (!ch){
    let t = 0;
    Object.keys(CH_NAMES[lv] || {}).forEach(c => {
      t += chapterFileRefs(lv, c).length;
    });
    return t;
  }
  if (!sub) return chapterFileRefs(lv, ch).length;
  return Object.values(files(lv, ch, sub) || {}).filter(Boolean).length;
}

if (typeof window !== 'undefined') {
  window.CH_NAMES      = CH_NAMES;
  window.LEVEL_LABELS  = LEVEL_LABELS;
  window.DRIVE         = DRIVE;
  window.ChapterData = {
    levels: _levels, levelLabel, chapters, chapterName,
    books, files, chapterFileRefs, allFileRefs, fileCount
  };
}
})();
"""

js = (
    "/* ══════════════════════════════════════════════════════════════════════\n"
    "   CHAPTERS-DATA.JS — ABHYAS AUTO-GENERATED\n"
    "   ──────────────────────────────────────────────────────────────────────\n"
    "   Four-level shape:  level -> chapter -> subtopic -> file(s)\n"
    "\n"
    "   Every chapter ends with:\n"
    "     • '<prefix>.<N+1> Mixed Questions'    — pooled unclassified, part N\n"
    "     • '<prefix>.<N+2> Original Questions' — every unique source file\n"
    "\n"
    "   Classified subtopics use leaves 'All' or 'part N'.\n"
    "   Mixed Questions uses 'part 1', 'part 2', …\n"
    "   Original Questions:\n"
    "     – L5 / L7 : '<Book>.<range>'         e.g. 'DPARSAD.1-100'\n"
    "     – GK      : '<topic>'                e.g. 'constitution'\n"
    "   Empty Mixed/Original buckets are omitted entirely.\n"
    "   ══════════════════════════════════════════════════════════════════════ */\n"
    "\nconst CH_NAMES = "     + to_js(CH_NAMES)     + ";\n"
    "\nconst LEVEL_LABELS = " + to_js(LEVEL_LABELS) + ";\n"
    "\nconst DRIVE = "        + to_js(DRIVE)        + ";\n"
    + CHAPTERDATA_BLOCK
)

out = os.path.join("abhyas_export", "chapters-data.js")
os.makedirs("abhyas_export", exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    f.write(js)

print(f"Wrote {out}")
for lv in DRIVE:
    n_ch   = len(DRIVE[lv])
    n_sub  = sum(len(DRIVE[lv][ch]) for ch in DRIVE[lv])
    n_file = sum(len(fm) for ch in DRIVE[lv] for fm in DRIVE[lv][ch].values())
    n_mixed = sum(
        1 for ch in DRIVE[lv]
        for k, v in DRIVE[lv][ch].items()
        if "Mixed Questions" in k and v
    )
    n_orig = sum(
        1 for ch in DRIVE[lv]
        for k, v in DRIVE[lv][ch].items()
        if "Original Questions" in k and v
    )
    print(f"  {lv}: {n_ch} chapters, {n_sub} subtopics, "
          f"{n_file} leaves, {n_mixed} Mixed, {n_orig} Original")

cur.close()
conn.close()