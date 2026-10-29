#!/usr/bin/env python3
"""Local question DB review dashboard. Start with: python review/dashboard.py"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from psycopg2 import sql

from psycopg2.extras import Json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils.db import connect  # noqa: E402
from dedupe.near_candidates import refresh_review_candidates  # noqa: E402
from review import duplicate_reviews  # noqa: E402

_lock = threading.RLock()
_conn = None


def query(sql, params=()):
    global _conn
    with _lock:
        last = None
        for _attempt in (1, 2, 3):
            try:
                if _conn is None or _conn.closed:
                    _conn = connect()
                cur = _conn.cursor()
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall() if cur.description else []
                cur.close()
                _conn.commit()
                return [dict(zip(cols, r)) for r in rows]
            except Exception as exc:
                last = exc
                try:
                    if _conn:
                        _conn.rollback()
                        _conn.close()
                except Exception:
                    pass
                _conn = None
        raise last


def one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else {}


def _review_write(fn, body):
    global _conn
    with _lock:
        if _conn is None or _conn.closed:
            _conn = connect()
        cur = _conn.cursor()
        try:
            duplicate_reviews.ensure_review_schema(cur)
            result = fn(body, cur)
            _conn.commit() if result.get("ok") else _conn.rollback()
            return result
        except Exception as exc:
            _conn.rollback()
            return {"ok": False, "error": str(exc)}
        finally:
            cur.close()


def api_review_candidates(params):
    global _conn
    with _lock:
        if _conn is None or _conn.closed:
            _conn = connect()
        cur = _conn.cursor()
        try:
            duplicate_reviews.ensure_review_schema(cur)
            result = duplicate_reviews.api_review_candidates(params, cur)
            _conn.commit()
            return result
        except Exception:
            _conn.rollback()
            raise
        finally:
            cur.close()


def api_review_refresh(_body):
    global _conn
    with _lock:
        if _conn is None or _conn.closed:
            _conn = connect()
        cur = _conn.cursor()
        try:
            duplicate_reviews.ensure_review_schema(cur)
            count = refresh_review_candidates(cur, _conn)
            return {"ok": True, "candidate_count": count}
        except Exception as exc:
            _conn.rollback()
            return {"ok": False, "error": str(exc)}
        finally:
            cur.close()


def api_summary():

    q = one("""
        SELECT count(*)::int AS questions,
               count(DISTINCT identity_key)::int AS unique_identity,
               count(DISTINCT question_text)::int AS unique_texts,
               count(*) FILTER (WHERE correct_index IS NULL
                     AND coalesce(btrim(correct_text),'') = '')::int AS unanswered,
               count(*) FILTER (WHERE correct_index IS NOT NULL AND options IS NOT NULL
                     AND jsonb_array_length(options) > 0
                     AND (correct_index < 0 OR correct_index >= jsonb_array_length(options)))::int AS out_of_range,
               count(*) FILTER (WHERE options IS NULL OR jsonb_array_length(options) = 0)::int AS empty_options,
               count(*) FILTER (WHERE coalesce(btrim(explanation),'') = '')::int AS no_explanation,
               count(*) FILTER (WHERE corrected_at IS NOT NULL)::int AS corrected,
               coalesce(round(avg(quality_score)::numeric, 3), 0) AS avg_quality
        FROM questions WHERE canonical_id IS NULL
    """)
    dup = one("""
        SELECT count(*)::int AS groups, coalesce(sum(n-1),0)::int AS extra FROM (
            SELECT c.id, q.identity_key, count(*) AS n
            FROM questions q JOIN files f ON f.id=q.file_ref_id
            JOIN books b ON b.id=f.book_id JOIN chapters c ON c.id=b.chapter_id
            WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
            GROUP BY c.id,q.identity_key HAVING count(*) > 1) x
    """)
    dup_text = one("""
        SELECT count(*)::int AS groups FROM (
            SELECT c.id,q.question_text FROM questions q
            JOIN files f ON f.id=q.file_ref_id JOIN books b ON b.id=f.book_id
            JOIN chapters c ON c.id=b.chapter_id
            WHERE q.canonical_id IS NULL
            GROUP BY c.id,q.question_text HAVING count(*) > 1) y
    """)
    dup_conflict = one("""
        SELECT count(*)::int AS count FROM (
            SELECT c.id,q.identity_key FROM questions q
            JOIN files f ON f.id=q.file_ref_id JOIN books b ON b.id=f.book_id
            JOIN chapters c ON c.id=b.chapter_id
            WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
            GROUP BY c.id,q.identity_key HAVING count(*) > 1
               AND count(DISTINCT CASE
                   WHEN q.correct_index IS NULL AND coalesce(btrim(q.correct_text),'') = '' THEN '#unanswered'
                   WHEN coalesce(btrim(q.correct_text),'') <> '' THEN lower(regexp_replace(q.correct_text,'[[:space:]]','','g'))
                   WHEN q.correct_index >= 0 AND q.correct_index < jsonb_array_length(q.options)
                        THEN lower(regexp_replace(q.options->>q.correct_index,'[[:space:]]','','g'))
                   ELSE '#invalid-answer' END) > 1) z
    """)
    counts = one("""
        SELECT (SELECT count(*) FROM books)::int AS books,
               (SELECT count(*) FROM books b WHERE NOT EXISTS (
                   SELECT 1 FROM files f JOIN questions q ON q.file_ref_id=f.id
                   WHERE f.book_id = b.id))::int AS empty_books,
               (SELECT count(*) FROM chapters)::int AS chapters,
               (SELECT count(*) FROM subtopics)::int AS subtopics,
               (SELECT count(*) FROM files)::int AS files,
               (SELECT count(*) FROM export_files)::int AS export_parts,
               (SELECT count(*) FROM export_files WHERE drive_file_id IS NULL
                    OR drive_file_id = '')::int AS export_parts_unuploaded,
               (SELECT count(*) FROM question_images)::int AS image_rows,
               (SELECT count(*) FROM applied_corrections)::int AS corrections_applied,
               (SELECT count(*) FROM dedup_deletions)::int AS dedup_deleted
    """)
    classified = query("""
        SELECT coalesce(classified_by,'(none)') AS k, count(*)::int AS v
        FROM question_subtopics GROUP BY 1 ORDER BY 2 DESC
    """)
    levels = query("""
        SELECT l.level_key AS k, count(DISTINCT q.id)::int AS v
        FROM levels l JOIN chapters c ON c.level_id=l.id
        JOIN books b ON b.chapter_id=c.id JOIN files f ON f.book_id=b.id
        JOIN questions q ON q.file_ref_id=f.id
        WHERE q.canonical_id IS NULL GROUP BY 1 ORDER BY 1
    """)
    deletions = query("SELECT reason,count(*)::int AS v FROM dedup_deletions GROUP BY 1 ORDER BY 2 DESC")
    return {"questions": q, "dup": {**dup, "text_groups": dup_text["groups"],
                                     "conflicting": dup_conflict["count"]},
            "counts": counts, "classified_by": classified,
            "levels": levels, "deletions": deletions}


UNANSWERED = "q.correct_index IS NULL AND coalesce(btrim(q.correct_text),'') = ''"


def api_issues():
    issues = []

    def add(iid, sev, title, count, hint, view=None, extra=None):
        issues.append({"id": iid, "severity": sev, "title": title,
                       "count": count, "hint": hint, "view": view, "extra": extra})

    n = one(f"SELECT count(*)::int c FROM questions q WHERE q.canonical_id IS NULL AND {UNANSWERED}")["c"]
    add("unanswered", "warn" if n else "ok", "Unanswered questions", n,
        "correct_index NULL and no correct_text", view="unanswered")
    n = one("""SELECT count(*)::int c FROM questions WHERE canonical_id IS NULL
               AND correct_index IS NOT NULL AND options IS NOT NULL AND jsonb_array_length(options)>0
               AND (correct_index<0 OR correct_index>=jsonb_array_length(options))""")["c"]
    add("range", "error" if n else "ok", "Answer index out of range", n,
        "correct_index outside options bounds")
    n = one("""SELECT count(*)::int c FROM questions WHERE canonical_id IS NULL
               AND (options IS NULL OR jsonb_array_length(options)=0)""")["c"]
    add("options", "error" if n else "ok", "Questions with no options", n,
        "options missing or empty array")
    n = one("SELECT count(*)::int c FROM question_subtopics WHERE classified_by='deferred'")["c"]
    add("deferred", "warn" if n else "ok", "Deferred classifications", n,
        "LLM outage leftovers — rerun classification with deferred retry")
    n = one("""SELECT count(*)::int c FROM question_subtopics qs
               JOIN subtopics s ON s.id=qs.subtopic_id WHERE s.code='mixedqn'""")["c"]
    add("mixed", "warn" if n else "ok", "Unclassified mixedqn links", n,
        "keyword and model classifiers did not place these")
    n = one("""SELECT count(*)::int c FROM (        SELECT c.id,q.identity_key FROM questions q
        JOIN files f ON f.id=q.file_ref_id JOIN books b ON b.id=f.book_id
        JOIN chapters c ON c.id=b.chapter_id
        WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
        GROUP BY c.id,q.identity_key HAVING count(*)>1) x
""")["c"]
    add("dupes", "warn" if n else "ok", "Duplicate identity groups", n,
        "same question+options appear multiple times", view="dupes")
    orphans = [
        ("orphan_q", "questions → missing file", "SELECT count(*)::int c FROM questions q LEFT JOIN files f ON f.id=q.file_ref_id WHERE f.id IS NULL"),
        ("orphan_f", "files → missing book", "SELECT count(*)::int c FROM files f LEFT JOIN books b ON b.id=f.book_id WHERE b.id IS NULL"),
        ("orphan_b", "books → missing chapter", "SELECT count(*)::int c FROM books b LEFT JOIN chapters c ON c.id=b.chapter_id WHERE c.id IS NULL"),
        ("orphan_qs", "subtopic links → missing question", "SELECT count(*)::int c FROM question_subtopics qs LEFT JOIN questions q ON q.id=qs.question_id WHERE q.id IS NULL"),
        ("orphan_st", "subtopic links → missing subtopic", "SELECT count(*)::int c FROM question_subtopics qs LEFT JOIN subtopics s ON s.id=qs.subtopic_id WHERE s.id IS NULL"),
    ]
    for iid, title, sql in orphans:
        n = one(sql)["c"]
        add(iid, "error" if n else "ok", f"Orphaned rows: {title}", n,
            "referential integrity probe")
    n = one("SELECT count(*)::int c FROM applied_corrections")["c"]
    add("corrections", "warn" if not n else "ok", "Corrections applied", n,
        "0 may mean correction sync has not run")
    return issues


def api_dupes(params):
    limit = max(1, min(int(params.get("limit", ["50"])[0]), 200))
    offset = max(0, int(params.get("offset", ["0"])[0]))
    total = one("""SELECT count(*)::int c FROM (
        SELECT c.id,q.identity_key FROM questions q
        JOIN files f ON f.id=q.file_ref_id JOIN books b ON b.id=f.book_id
        JOIN chapters c ON c.id=b.chapter_id
        WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
        GROUP BY c.id,q.identity_key HAVING count(*)>1) x""")["c"]
    rows = query("""
        SELECT q.identity_key,c.id AS chapter_id,c.chapter_name,
               count(*)::int AS n,
               count(DISTINCT CASE
                   WHEN q.correct_index IS NULL AND coalesce(btrim(q.correct_text),'') = '' THEN '#unanswered'
                   WHEN coalesce(btrim(q.correct_text),'') <> '' THEN lower(regexp_replace(q.correct_text,'[[:space:]]','','g'))
                   WHEN q.correct_index >= 0 AND q.correct_index < jsonb_array_length(q.options)
                        THEN lower(regexp_replace(q.options->>q.correct_index,'[[:space:]]','','g'))
                   ELSE '#invalid-answer' END)::int AS answers,
               min(q.question_text) AS sample,min(q.id) AS sample_id
        FROM questions q JOIN files f ON f.id=q.file_ref_id
        JOIN books b ON b.id=f.book_id JOIN chapters c ON c.id=b.chapter_id
        WHERE q.identity_key IS NOT NULL AND q.canonical_id IS NULL
        GROUP BY c.id,c.chapter_name,q.identity_key HAVING count(*)>1
        ORDER BY answers DESC,n DESC,sample_id LIMIT %s OFFSET %s
    """, (limit, offset))
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


def api_dup_detail(params):
    key = params.get("key", [""])[0]
    if not key:
        return {"error": "missing key"}
    try:
        chapter_id = int(params.get("chapter", ["0"])[0])
    except (TypeError, ValueError):
        return {"error": "chapter must be an integer"}
    rows = query("""
        SELECT q.id,q.question_number,q.question_text,q.options,q.correct_index,
               q.correct_text,q.explanation,q.quality_score,q.corrected_at,q.created_at,
               b.book_name,f.file_id,q.identity_key,c.id AS chapter_id,
               (SELECT s.code||' '||s.title FROM question_subtopics qs
                JOIN subtopics s ON s.id=qs.subtopic_id
                WHERE qs.question_id=q.id AND s.code<>'mixedqn'
                ORDER BY qs.confidence DESC NULLS LAST LIMIT 1) AS subtopic
        FROM questions q LEFT JOIN files f ON f.id=q.file_ref_id
        LEFT JOIN books b ON b.id=f.book_id LEFT JOIN chapters c ON c.id=b.chapter_id
        WHERE q.identity_key=%s AND c.id=%s AND q.canonical_id IS NULL ORDER BY q.id
    """, (key, chapter_id))
    return {"rows": rows}


def api_questions(params):
    limit = max(1, min(int(params.get("limit", ["50"])[0]), 200))
    offset = max(0, int(params.get("offset", ["0"])[0]))
    search = (params.get("q", [""])[0] or "").strip()
    level = (params.get("level", [""])[0] or "").strip()
    only = (params.get("only", [""])[0] or "").strip()
    where, args = ["q.canonical_id IS NULL"], []
    if search:
        where.append("(q.question_text ILIKE %s OR q.identity_key ILIKE %s)")
        args += [f"%{search}%", f"%{search}%"]
    if level:
        where.append("l.level_key=%s")
        args.append(level)
    if only == "unanswered":
        where.append(UNANSWERED)
    elif only == "dupes":
        where.append("""(SELECT count(*) FROM questions x
                         JOIN files xf ON xf.id=x.file_ref_id
                         JOIN books xb ON xb.id=xf.book_id
                         JOIN chapters xc ON xc.id=xb.chapter_id
                         WHERE x.identity_key=q.identity_key AND xc.id=c2.id
                         AND x.canonical_id IS NULL)>1""")
    elif only == "unclassified":
        where.append("""NOT EXISTS (SELECT 1 FROM question_subtopics qs
                         JOIN subtopics s ON s.id=qs.subtopic_id
                         WHERE qs.question_id=q.id AND s.code<>'mixedqn')""")
    condition = " AND ".join(where)
    joins = """FROM questions q LEFT JOIN files f ON f.id=q.file_ref_id
        LEFT JOIN books b ON b.id=f.book_id LEFT JOIN chapters c2 ON c2.id=b.chapter_id
        LEFT JOIN levels l ON l.id=c2.level_id"""
    total = one(f"SELECT count(*)::int c {joins} WHERE {condition}", tuple(args))["c"]
    rows = query(f"""
        SELECT q.id,q.question_number,q.question_text,q.options,q.correct_index,
               q.correct_text,q.explanation,q.quality_score,q.corrected_at,
               b.book_name,l.level_key,c2.chapter_name,
               (SELECT s.code||' '||s.title FROM question_subtopics qs
                JOIN subtopics s ON s.id=qs.subtopic_id
                WHERE qs.question_id=q.id AND s.code<>'mixedqn'
                ORDER BY qs.confidence DESC NULLS LAST LIMIT 1) AS subtopic,
               q.identity_key,
               (SELECT count(*) FROM questions x
                JOIN files xf ON xf.id=x.file_ref_id JOIN books xb ON xb.id=xf.book_id
                JOIN chapters xc ON xc.id=xb.chapter_id
                WHERE x.identity_key=q.identity_key AND xc.id=c2.id
                AND x.canonical_id IS NULL)::int AS copies
        {joins} WHERE {condition} ORDER BY q.id LIMIT %s OFFSET %s
    """, tuple(args + [limit, offset]))
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


def ensure_schema():
    global _conn
    with _lock:
        if _conn is None or _conn.closed:
            _conn = connect()
        cur = _conn.cursor()
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS review_deletions (
                    id BIGSERIAL PRIMARY KEY,question_id BIGINT NOT NULL,
                    snapshot JSONB NOT NULL,reason TEXT,deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    restored_at TIMESTAMPTZ
                )
            """)
            duplicate_reviews.ensure_review_schema(cur)
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
        finally:
            cur.close()


def api_verify(_params):
    started = time.time()
    checks = api_issues()
    n = one("""SELECT count(*)::int c FROM questions q WHERE q.canonical_id IS NULL AND NOT EXISTS
             (SELECT 1 FROM question_subtopics qs WHERE qs.question_id=q.id)""")["c"]
    checks.append({"id":"no_link","severity":"error" if n else "ok",
                   "title":"Questions without subtopic links","count":n,
                   "hint":"could be absent from exports","view":"questions"})
    n = one("SELECT count(*)::int c FROM dedupe_review_candidates WHERE status='pending'")["c"]
    checks.append({"id":"fuzzy_pending","severity":"warn" if n else "ok",
                   "title":"Fuzzy duplicate candidates awaiting review","count":n,
                   "hint":"similarity is not proof; inspect manually","view":"review"})
    errors = sum(x["severity"] == "error" for x in checks)
    warnings = sum(x["severity"] == "warn" for x in checks)
    return {"verdict":"FAIL" if errors else "WARN" if warnings else "PASS",
            "errors":errors,"warnings":warnings,
            "passed":sum(x["severity"] == "ok" for x in checks),
            "seconds":round(time.time()-started,2),"checks":checks}


def api_delete(body):
    global _conn
    try:
        qid = int(body.get("id"))
    except (TypeError, ValueError):
        return {"ok":False,"error":"id must be an integer"}
    reason = str(body.get("reason") or "dashboard delete").strip()[:300]
    with _lock:
        if _conn is None or _conn.closed:
            _conn = connect()
        cur = _conn.cursor()
        try:
            cur.execute("SELECT to_jsonb(q) FROM questions q WHERE id=%s FOR UPDATE",(qid,))
            row = cur.fetchone()
            if not row:
                _conn.rollback()
                return {"ok":False,"error":f"question {qid} not found"}
            snapshot = {"question":row[0],"child_rows":{}}
            child_fks = duplicate_reviews.discover_question_child_fks(cur)
            for schema_name,table_name,column_name in child_fks:
                statement=sql.SQL("SELECT to_jsonb(x) FROM {}.{} x WHERE {}=%s").format(
                    sql.Identifier(schema_name),sql.Identifier(table_name),sql.Identifier(column_name))
                cur.execute(statement,(qid,))
                snapshot["child_rows"][f"{schema_name}.{table_name}.{column_name}"]=[r[0] for r in cur.fetchall()]
            cur.execute("INSERT INTO review_deletions(question_id,snapshot,reason) VALUES(%s,%s,%s) RETURNING id",
                        (qid,Json(snapshot,dumps=lambda x:json.dumps(x,default=str)),reason))
            audit_id = cur.fetchone()[0]
            cur.execute("DELETE FROM dedupe_review_candidates WHERE q1_id=%s OR q2_id=%s",(qid,qid))
            child_fks = duplicate_reviews.discover_question_child_fks(cur)
            for schema_name,table_name,column_name in child_fks:
                if table_name == "questions" and column_name == "canonical_id":
                    statement=sql.SQL("UPDATE {}.{} SET {}=NULL WHERE {}=%s").format(
                        sql.Identifier(schema_name),sql.Identifier(table_name),
                        sql.Identifier(column_name),sql.Identifier(column_name))
                else:
                    statement=sql.SQL("DELETE FROM {}.{} WHERE {}=%s").format(
                        sql.Identifier(schema_name),sql.Identifier(table_name),sql.Identifier(column_name))
                cur.execute(statement,(qid,))
            cur.execute("DELETE FROM questions WHERE id=%s",(qid,))
            if cur.rowcount != 1:
                raise RuntimeError("question delete did not affect one row")
            _conn.commit()
            return {"ok":True,"id":qid,"audit_id":audit_id}
        except Exception:
            _conn.rollback()
            raise
        finally:
            cur.close()


def api_edit(body, cur):
    return duplicate_reviews.api_edit_question(body, cur)


def api_review(body, cur):
    return duplicate_reviews.api_review_candidate(body, cur)


def api_apply_exact_group(body):
    try:
        chapter_id = int(body.get("chapter_id"))
    except (TypeError, ValueError):
        return {"ok":False,"error":"chapter_id must be an integer"}
    identity_key = str(body.get("identity_key") or "").strip()
    if not identity_key:
        return {"ok":False,"error":"identity_key is required"}

    def apply(_body, cur):
        duplicate_reviews.ensure_review_schema(cur)
        cur.execute("""SELECT q.id,q.question_text,q.options,q.correct_index,
                q.correct_text,q.quality_score,q.corrected_at
            FROM questions q JOIN files f ON f.id=q.file_ref_id
            JOIN books b ON b.id=f.book_id JOIN chapters c ON c.id=b.chapter_id
            WHERE c.id=%s AND q.identity_key=%s AND q.canonical_id IS NULL
            ORDER BY q.id FOR UPDATE OF q""", (chapter_id,identity_key))
        rows=cur.fetchall()
        if len(rows)<2:
            return {"ok":False,"error":"exact duplicate group no longer has multiple live questions"}
        from dedupe.content_identity import answer_identity
        from dedupe.dedupe_exact import pick_keeper
        from dedupe.merge_questions import discover_question_child_fks,merge_question_rows
        groups={}
        for row in rows:
            qid,text,options,correct_index,correct_text,quality,corrected=row
            key=answer_identity(correct_text,correct_index,options)
            item={"id":qid,"text":text,"options":options or [],"ci":correct_index,
                  "ct":correct_text,"quality_score":float(quality or 0),
                  "corrected_at":corrected.timestamp() if corrected else 0.0}
            groups.setdefault(key,[]).append(item)
        child_fks=discover_question_child_fks(cur)
        merged=0
        for answer_key,bucket in groups.items():
            if answer_key.startswith("__") or len(bucket)<2:
                continue
            keeper=pick_keeper(bucket)
            for duplicate in bucket:
                if duplicate["id"]==keeper["id"]:
                    continue
                merge_question_rows(cur,keeper["id"],duplicate["id"],reason="review_exact_group",
                    signature=identity_key,child_fks=child_fks)
                merged+=1
        return {"ok":True,"merged":merged,"retained":len(rows)-merged,
                "reason":"only copies with an exact identity and a matching verified answer merge"}

    return _review_write(apply,body)


def api_apply_exact(body):
    try:
        keeper_id = int(body.get("keeper_id"))
        duplicate_id = int(body.get("duplicate_id"))
    except (TypeError, ValueError):
        return {"ok":False,"error":"keeper_id and duplicate_id must be integers"}

    def apply(_body, cur):
        duplicate_reviews.ensure_review_schema(cur)
        from dedupe.content_identity import identity
        cur.execute("""SELECT q.id,q.question_text,q.options,q.correct_text,q.correct_index,c.id
            FROM questions q JOIN files f ON f.id=q.file_ref_id
            JOIN books b ON b.id=f.book_id JOIN chapters c ON c.id=b.chapter_id
            WHERE q.id=ANY(%s) AND q.canonical_id IS NULL FOR UPDATE OF q""",
            ([keeper_id,duplicate_id],))
        rows=cur.fetchall()
        if len(rows)!=2:
            return {"ok":False,"error":"questions missing"}
        byid={r[0]:r for r in rows}; keeper=byid[keeper_id]; dup=byid[duplicate_id]
        if keeper[5]!=dup[5]:
            return {"ok":False,"error":"questions are not in the same chapter"}
        if identity(keeper[1],keeper[2] or [])!=identity(dup[1],dup[2] or []):
            return {"ok":False,"error":"questions are not exact question+option matches"}
        keeper_answer = duplicate_reviews._answer_key(keeper[3],keeper[4],keeper[2])
        duplicate_answer = duplicate_reviews._answer_key(dup[3],dup[4],dup[2])
        if keeper_answer.startswith("__") or duplicate_answer.startswith("__"):
            return {"ok":False,"error":"both questions need valid, consistent answers before merging"}
        if keeper_answer != duplicate_answer:
            return {"ok":False,"error":"answer conflict; correct the answers before merging"}
        from dedupe.merge_questions import discover_question_child_fks,merge_question_rows
        merge_question_rows(cur,keeper_id,duplicate_id,reason="review_exact",
            signature=identity(keeper[1],keeper[2] or []),
            child_fks=discover_question_child_fks(cur))
        return {"ok":True,"keeper_id":keeper_id,"deleted_id":duplicate_id}

    return _review_write(apply,body)


def api_delete_dispatch(body):
    return api_delete(body)


ROUTES = {
    "/api/summary":lambda p:api_summary(),
    "/api/issues":lambda p:api_issues(),
    "/api/verify":api_verify,
    "/api/dupes":api_dupes,
    "/api/dup/detail":api_dup_detail,
    "/api/questions":api_questions,
    "/api/review/candidates":api_review_candidates,
}
POST_ROUTES = {
    "/api/delete":api_delete_dispatch,
    "/api/edit":lambda b:_review_write(api_edit,b),
    "/api/review/action":lambda b:_review_write(api_review,b),
    "/api/review/refresh":lambda b:api_review_refresh(b),
    "/api/merge/exact":api_apply_exact,
    "/api/merge/exact-group":api_apply_exact_group,
}

PAGE = (Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
       if Path(__file__).with_name("dashboard.html").is_file() else r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Abhyas question DB review</title><style>
:root{--bg:#0f1218;--card:#171c26;--line:#252c3a;--fg:#e6ebf4;--dim:#8b95a8;--ok:#38c172;--warn:#f4b942;--err:#e35d5d;--acc:#5b8cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;background:#12161f;border-bottom:1px solid var(--line);padding:10px 18px;display:flex;gap:18px;align-items:center;z-index:5;flex-wrap:wrap}h1{font-size:16px;margin:0;font-weight:650}nav{display:flex;gap:6px;flex-wrap:wrap}
nav button,.pg{background:#232a39;border:1px solid var(--line);color:var(--fg);padding:6px 12px;border-radius:8px;cursor:pointer}nav button.on{background:var(--acc);color:white}
main{padding:18px;max-width:1280px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}.card,.q,.copy,.drawer{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px}.card .v{font-size:26px;font-weight:700}.card .k,.muted,.meta{color:var(--dim);font-size:12px}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
input[type=text],textarea,select{background:var(--card);border:1px solid var(--line);color:var(--fg);padding:7px 10px;border-radius:8px;font:inherit;min-width:180px}textarea{width:100%;min-height:72px}.issue{display:flex;gap:12px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;margin:8px 0}.issue .n{margin-left:auto;font-weight:bold}.pill{padding:2px 8px;border-radius:20px;font-size:11px;background:#1d2740;color:#9db9ff}.err{color:#ff9c9c}.ok{color:#8ff0bd}.warn{color:#ffd98a}.copy .hdr{display:flex;gap:8px;align-items:center;flex-wrap:wrap;color:var(--dim);font-size:12px}.opt{padding:4px 10px;margin:3px;background:#12161f;border-radius:6px;white-space:pre-wrap}.right{border:1px solid #2c6b47;background:#143a26}.exp{color:var(--dim);margin-top:8px}.danger{color:#ff9c9c}.candidate{border-left:3px solid var(--acc)}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
</style></head><body><header><h1>Abhyas question DB review</h1><nav><button data-tab="overview" class="on">Overview</button><button data-tab="issues">Verify</button><button data-tab="dupes">Exact</button><button data-tab="review">Fuzzy review</button><button data-tab="questions">Questions</button></nav><span id="status" class="muted"></span></header>
<main><section id="tab-overview"></section><section id="tab-issues" hidden><div class="toolbar"><button class="pg" id="verifyBtn">Run full verify</button><span id="verifyBanner" class="muted">checks run against DB</span></div><div id="issueList"></div></section>
<section id="tab-dupes" hidden><div class="toolbar"><button class="pg" id="dupPrev">‹ prev</button><span id="dupPos" class="muted"></span><button class="pg" id="dupNext">next ›</button></div><table><thead><tr><th>#</th><th>chapter</th><th>copies</th><th>answers</th><th>sample question</th><th></th></tr></thead><tbody id="dupRows"></tbody></table><div id="dupDetail"></div></section>
<section id="tab-review" hidden><div class="toolbar"><select id="rstatus"><option value="pending">pending</option><option value="merged">merged</option><option value="rejected">rejected</option><option value="all">all</option></select><button class="pg" id="reviewRefresh">Refresh candidates</button><span id="reviewPos" class="muted"></span><button class="pg" id="reviewPrev">‹ prev</button><button class="pg" id="reviewNext">next ›</button></div><div id="reviewRows"></div></section>
<section id="tab-questions" hidden><div class="toolbar"><input id="qs" type="text" placeholder="search text or identity"><select id="qlvl"><option value="">all levels</option></select><select id="qonly"><option value="">all</option><option value="unanswered">unanswered</option><option value="dupes">duplicate members</option><option value="unclassified">unclassified</option></select><button class="pg" id="qPrev">‹ prev</button><span id="qPos" class="muted"></span><button class="pg" id="qNext">next ›</button></div><div id="qRows"></div></section></main>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));const status=t=>$('#status').textContent=t;
async function get(url){status('loading…');try{const r=await fetch(url);const j=await r.json();if(!r.ok)throw Error(j.error||r.status);status('');return j}catch(e){status('ERROR '+e.message);return null}}
async function post(url,body){status('saving…');try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw Error(j.error||r.status);status('');return j}catch(e){status('ERROR '+e.message);return null}}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!=='tab-'+b.dataset.tab);if(b.dataset.tab==='dupes')loadDupes(0);if(b.dataset.tab==='review')loadReview(0);if(b.dataset.tab==='issues')loadIssues();if(b.dataset.tab==='questions')loadQuestions(0)});
function card(k,v){return `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`}
async function loadOverview(){const d=await get('/api/summary');if(!d)return;const q=d.questions,c=d.counts,dp=d.dup;$('#tab-overview').innerHTML=`<div class="grid">${card('Questions',q.questions)}${card('unique identities',q.unique_identity)}${card('unanswered',q.unanswered)}${card('duplicate groups',dp.groups)}${card('extra duplicate copies',dp.extra)}${card('answer conflicts',dp.conflicting)}${card('corrected questions',q.corrected)}${card('chapters',c.chapters)}${card('source files',c.files)}${card('images',c.image_rows)}${card('dedupe audit merges',c.dedup_deleted)}</div><h2>Classification sources</h2><div class="card">${d.classified_by.map(x=>`${esc(x.k)}: ${x.v}`).join(' · ')}</div><h2>Levels</h2><div class="card">${d.levels.map(x=>`${esc(x.k)}: ${x.v}`).join(' · ')}</div>`}
async function loadIssues(){const d=await get('/api/issues');if(d)renderIssues(d)}function renderIssues(rows){$('#issueList').innerHTML=rows.map(i=>`<div class="issue"><span>${esc(i.title)}<small class="muted"> — ${esc(i.hint||'')}</small></span><b class="n">${i.count}</b>${i.view?`<button class="pg" data-view="${i.view}">inspect</button>`:''}</div>`).join('');$('#issueList').querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>document.querySelector(`nav button[data-tab="${b.dataset.view==='review'?'review':b.dataset.view==='dupes'?'dupes':'questions'}"]`).click())}
$('#verifyBtn').onclick=async()=>{const d=await get('/api/verify');if(!d)return;$('#verifyBanner').textContent=`${d.verdict}: ${d.passed} passed, ${d.warnings} warnings, ${d.errors} errors in ${d.seconds}s`;renderIssues(d.checks)};
let dupOff=0,dupRows=[];async function loadDupes(off){dupOff=off;const d=await get(`/api/dupes?limit=50&offset=${off}`);if(!d)return;dupRows=d.rows;$('#dupPos').textContent=d.total?`${off+1}–${Math.min(off+50,d.total)} of ${d.total}`:'0 groups';$('#dupPrev').disabled=off<=0;$('#dupNext').disabled=off+50>=d.total;$('#dupRows').innerHTML=d.rows.map((r,i)=>`<tr><td>${off+i+1}</td><td>${esc(r.chapter_name)}</td><td>${r.n}</td><td>${r.answers>1?'<span class="err">conflict</span>':'same'}</td><td>${esc(r.sample).slice(0,180)}</td><td><button class="pg" data-key="${esc(r.identity_key)}" data-chapter="${r.chapter_id}">inspect</button><button class="pg" data-merge="${esc(r.identity_key)}" data-chapter="${r.chapter_id}">merge safe exacts</button></td></tr>`).join('')||'<tr><td colspan="6">No exact groups</td></tr>';$('#dupRows').querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>showDup(b.dataset.key,+b.dataset.chapter));$('#dupRows').querySelectorAll('[data-merge]').forEach(b=>b.onclick=()=>mergeExactKey(b.dataset.key,+b.dataset.chapter))}
$('#dupPrev').onclick=()=>loadDupes(Math.max(0,dupOff-50));$('#dupNext').onclick=()=>loadDupes(dupOff+50);
async function showDup(key,chapter){const d=await get('/api/dup/detail?key='+encodeURIComponent(key)+'&chapter='+chapter);if(!d)return;$('#dupDetail').innerHTML=`<div class="drawer"><b>Exact group · ${d.rows.length} copies</b>${d.rows.map(r=>`<div class="copy"><div class="hdr"><span>#${r.id}</span><span>${esc(r.book_name||'')}</span><span>${esc(r.subtopic||'')}</span><span>${r.corrected_at?'human-corrected':''}</span><button class="pg" data-edit="${r.id}">edit</button><button class="pg danger" data-delete="${r.id}">delete</button></div><div>${esc(r.question_text)}</div>${(r.options||[]).map((o,i)=>`<div class="opt ${i===r.correct_index?'right':''}">[${i}] ${esc(o)}</div>`).join('')}<div class="exp">${esc(r.explanation||'')}</div></div>`).join('')}</div>`;$('#dupDetail').querySelectorAll('[data-delete]').forEach(b=>b.onclick=async()=>{if(confirm('Delete this question? Snapshot is kept in review_deletions.')){await post('/api/delete',{id:+b.dataset.delete,reason:'exact group review'});showDup(key,chapter);loadDupes(dupOff)}});$('#dupDetail').querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editQuestion(d.rows.find(x=>x.id===+b.dataset.edit),key,chapter));$('#dupDetail').scrollIntoView({behavior:'smooth'})}
async function mergeExactKey(key,chapter){const d=await get('/api/dup/detail?key='+encodeURIComponent(key)+'&chapter='+chapter);if(!d||d.rows.length<2)return;const keeper=d.rows.find(x=>x.corrected_at)||d.rows[0];if(!confirm(`Merge only copies whose exact content AND verified answer agree? Keep #${keeper.id}. Unanswered, invalid, or conflicting copies stay for review.`))return;const res=await post('/api/merge/exact-group',{identity_key:key,chapter_id:chapter});if(res&&res.ok){status(`merged ${res.merged}; retained ${res.retained} for review`);loadDupes(dupOff);showDup(key,chapter)}}
function editQuestion(r,key,chapter){const text=prompt('Edit question text',r.question_text);if(text===null)return;const opts=prompt('Edit options as JSON array',JSON.stringify(r.options||[]));if(opts===null)return;let options;try{options=JSON.parse(opts)}catch(e){alert('Options must be valid JSON');return}const ci=prompt('Correct option index, or blank for unanswered',r.correct_index??'');if(ci!==''&&(!Number.isInteger(Number(ci))||Number(ci)<0||Number(ci)>=options.length)){alert('Correct index must be blank or point to a valid option');return}const explanation=prompt('Explanation',r.explanation||'');if(explanation===null)return;post('/api/edit',{id:r.id,question_text:text,options,correct_index:ci===''?null:Number(ci),explanation,set_source_correction:true}).then(x=>{if(x&&x.ok){status('Edited; correction will be reapplied on refresh');loadDupes(dupOff);showDup(key,chapter)}})}
let reviewOff=0;async function loadReview(off){reviewOff=off;const d=await get(`/api/review/candidates?status=${encodeURIComponent($('#rstatus').value)}&limit=25&offset=${off}`);if(!d)return;$('#reviewPos').textContent=d.total?`${off+1}–${Math.min(off+25,d.total)} of ${d.total}`:'0 candidates';$('#reviewPrev').disabled=off<=0;$('#reviewNext').disabled=off+25>=d.total;$('#reviewRows').innerHTML=d.rows.map(r=>`<div class="copy candidate"><div class="hdr"><b>Candidate ${r.id}</b><span>chapter ${esc(r.chapter_name||r.chapter_id)}</span><span>stem ${Number(r.stem_score).toFixed(1)}%</span><span>options ${Number(r.options_score).toFixed(1)}%</span><span>${esc(r.status)}</span></div>${reviewCopy(r.q1_id,r.q1_text,r.q1_options,r.q1_correct_index,r.q1_correct_text,r.q1_explanation,'A')}${reviewCopy(r.q2_id,r.q2_text,r.q2_options,r.q2_correct_index,r.q2_correct_text,r.q2_explanation,'B')}<div class="toolbar"><button class="pg" data-reject="${r.id}">Not duplicate</button><button class="pg" data-merge="${r.id}" data-a="${r.q1_id}" data-b="${r.q2_id}">Approve merge (choose keeper)</button><button class="pg" data-edit="${r.id}" data-a="${r.q1_id}" data-b="${r.q2_id}">Edit A/B</button></div></div>`).join('')||'<p class="muted">No candidates in this view.</p>';$('#reviewRows').querySelectorAll('[data-reject]').forEach(b=>b.onclick=async()=>{await post('/api/review/action',{id:+b.dataset.reject,action:'reject'});loadReview(reviewOff)});$('#reviewRows').querySelectorAll('[data-merge]').forEach(b=>b.onclick=async()=>{const a=+b.dataset.a,bid=+b.dataset.b,k=Number(prompt(`Keeper question ID? Enter ${a} or ${bid}`,String(a)));if(![a,bid].includes(k))return;const x=await post('/api/review/action',{id:+b.dataset.merge,action:'merge',keeper_id:k});if(x&&x.ok)loadReview(reviewOff)});$('#reviewRows').querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>{const row=d.rows.find(x=>x.id===+b.dataset.edit),a=+b.dataset.a,bid=+b.dataset.b,id=Number(prompt(`Which question to edit? Enter ${a} or ${bid}`,String(a)));if(![a,bid].includes(id))return;editQuestion({id,question_text:id===a?row.q1_text:row.q2_text,options:id===a?row.q1_options:row.q2_options,correct_index:id===a?row.q1_correct_index:row.q2_correct_index,explanation:id===a?row.q1_explanation:row.q2_explanation},null,null)})}
function reviewCopy(id,text,options,ci,ct,ex,label){return `<div class="copy"><div class="hdr"><b>${label} · #${id}</b><span>answer ${ci==null?'unanswered':`[${ci}] ${esc(ct||'')}`}</span></div><div>${esc(text||'')}</div>${(options||[]).map((o,i)=>`<div class="opt ${i===ci?'right':''}">[${i}] ${esc(o)}</div>`).join('')}<div class="exp">${esc(ex||'')}</div></div>`}
$('#reviewRefresh').onclick=async()=>{const r=await post('/api/review/refresh',{});if(r&&r.ok){status(`refreshed; ${r.candidate_count} candidates`);loadReview(0)}};$('#rstatus').onchange=()=>loadReview(0);$('#reviewPrev').onclick=()=>loadReview(Math.max(0,reviewOff-25));$('#reviewNext').onclick=()=>loadReview(reviewOff+25);
let qOff=0;async function loadQuestions(off){qOff=off;const p=new URLSearchParams({limit:50,offset:off,q:$('#qs').value,level:$('#qlvl').value,only:$('#qonly').value});const d=await get('/api/questions?'+p);if(!d)return;$('#qPos').textContent=d.total?`${off+1}–${Math.min(off+50,d.total)} of ${d.total}`:'0';$('#qPrev').disabled=off<=0;$('#qNext').disabled=off+50>=d.total;$('#qRows').innerHTML=d.rows.map(r=>`<div class="q"><div class="meta">#${r.id} · ${esc(r.level_key||'')} · ${esc(r.chapter_name||'')} · ${esc(r.subtopic||'')} · ${r.copies} copies <button class="pg" data-edit="${r.id}">edit</button><button class="pg danger" data-delete="${r.id}">delete</button></div><div>${esc(r.question_text)}</div>${(r.options||[]).map((o,i)=>`<div class="opt ${i===r.correct_index?'right':''}">[${i}] ${esc(o)}</div>`).join('')}</div>`).join('');$('#qRows').querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editQuestion(d.rows.find(x=>x.id===+b.dataset.edit),null,null));$('#qRows').querySelectorAll('[data-delete]').forEach(b=>b.onclick=async()=>{if(confirm('Delete with audit snapshot?')){await post('/api/delete',{id:+b.dataset.delete});loadQuestions(qOff)}})}
$('#qs').oninput=()=>{clearTimeout(window._qt);window._qt=setTimeout(()=>loadQuestions(0),300)};$('#qlvl').onchange=$('#qonly').onchange=()=>loadQuestions(0);$('#qPrev').onclick=()=>loadQuestions(Math.max(0,qOff-50));$('#qNext').onclick=()=>loadQuestions(qOff+50);
(async()=>{await loadOverview();const d=await get('/api/summary');if(d)$('#qlvl').insertAdjacentHTML('beforeend',d.levels.map(x=>`<option value="${esc(x.k)}">${esc(x.k)}</option>`).join(''));loadIssues()})();
</script></body></html>""")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html")
        fn = ROUTES.get(u.path)
        if not fn:
            return self._send(404, json.dumps({"error":"not found"}), "application/json")
        try:
            result = fn(parse_qs(u.query))
            return self._send(200, json.dumps(result, default=str), "application/json")
        except Exception as exc:
            return self._send(500, json.dumps({"error":str(exc)}), "application/json")

    def do_POST(self):
        u = urlparse(self.path)
        fn = POST_ROUTES.get(u.path)
        if not fn:
            return self._send(404, json.dumps({"error":"not found"}), "application/json")
        try:
            n = min(max(int(self.headers.get("Content-Length") or 0), 0), 1_000_000)
            body = json.loads(self.rfile.read(n) or b"{}")
            result = fn(body)
            return self._send(200 if result.get("ok") else 400,
                              json.dumps(result, default=str), "application/json")
        except Exception as exc:
            return self._send(500, json.dumps({"error":str(exc)}), "application/json")


def main():
    ap = argparse.ArgumentParser(description="Local Abhyas DB review dashboard")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    try:
        ensure_schema()
        n = one("SELECT count(*)::int AS c FROM questions WHERE canonical_id IS NULL")["c"]
        print(f"[dashboard] connected: {n:,} live questions")
    except Exception as exc:
        print(f"[dashboard] DB check failed: {exc}")
        print("[dashboard] serving anyway; API will retry per request")
    print(f"[dashboard] http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
