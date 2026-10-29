"""Question duplicate review operations used by the local dashboard."""

import re

from psycopg2.extras import Json

from dedupe.content_identity import (
    identity, compute_quality_score, source_question_key, source_location_key,
)
from dedupe.source_aliases import ensure_correction_source_alias_schema
from dedupe.merge_questions import (
    discover_question_child_fks,
    ensure_merge_schema,
    merge_question_rows,
)


def ensure_review_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dedupe_review_candidates (
            id BIGSERIAL PRIMARY KEY,
            chapter_id INTEGER NOT NULL,
            q1_id BIGINT NOT NULL,
            q2_id BIGINT NOT NULL,
            stem_score REAL NOT NULL,
            options_score REAL NOT NULL,
            q1_identity_key TEXT,
            q2_identity_key TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            review_note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reviewed_at TIMESTAMPTZ,
            UNIQUE (q1_id, q2_id),
            CHECK (q1_id < q2_id),
            CHECK (status IN ('pending', 'merged', 'rejected'))
        )
    """)
    cur.execute("ALTER TABLE dedupe_review_candidates ADD COLUMN IF NOT EXISTS q1_identity_key TEXT")
    cur.execute("ALTER TABLE dedupe_review_candidates ADD COLUMN IF NOT EXISTS q2_identity_key TEXT")
    ensure_merge_schema(cur)
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS source_identity TEXT")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS source_question_key TEXT")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS corrected_at TIMESTAMPTZ")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS identity_key TEXT")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS identity_version SMALLINT")
    cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS quality_score REAL DEFAULT 0")
    ensure_correction_source_alias_schema(cur)


def api_review_candidates(params, cur):
    limit = max(1, min(int(params.get("limit", ["50"])[0]), 200))
    offset = max(0, int(params.get("offset", ["0"])[0]))
    status = (params.get("status", ["pending"])[0] or "pending").strip()
    if status not in {"pending", "merged", "rejected", "all"}:
        return {"error": "invalid review status"}
    where = "" if status == "all" else "WHERE d.status=%s"
    args = () if status == "all" else (status,)
    cur.execute(f"SELECT count(*) FROM dedupe_review_candidates d {where}", args)
    total = cur.fetchone()[0]
    cur.execute(f"""
        SELECT d.id,d.chapter_id,d.q1_id,d.q2_id,d.stem_score,d.options_score,
               d.q1_identity_key,d.q2_identity_key,
               d.status,d.review_note,d.created_at,d.reviewed_at,
               c.chapter_name,
               q1.question_text AS q1_text,q1.options AS q1_options,
               q1.correct_index AS q1_correct_index,q1.correct_text AS q1_correct_text,
               q1.explanation AS q1_explanation,q1.quality_score AS q1_quality_score,
               q1.corrected_at AS q1_corrected_at,q1.source_identity AS q1_source_identity,
               q1.file_ref_id AS q1_file_ref_id,
               q2.question_text AS q2_text,q2.options AS q2_options,
               q2.correct_index AS q2_correct_index,q2.correct_text AS q2_correct_text,
               q2.explanation AS q2_explanation,q2.quality_score AS q2_quality_score,
               q2.corrected_at AS q2_corrected_at,q2.source_identity AS q2_source_identity,
               q2.file_ref_id AS q2_file_ref_id
        FROM dedupe_review_candidates d
        JOIN questions q1 ON q1.id=d.q1_id
        JOIN questions q2 ON q2.id=d.q2_id
        JOIN chapters c ON c.id=d.chapter_id
        {where}
        ORDER BY d.stem_score DESC,d.options_score DESC,d.id
        LIMIT %s OFFSET %s
    """, args + (limit, offset))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


def api_edit_question(body, cur):
    try:
        qid = int(body.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "id must be an integer"}
    text = str(body.get("question_text") or "").strip()
    options = body.get("options")
    if not text or not isinstance(options, list) or not options or not all(isinstance(x, str) for x in options):
        return {"ok": False, "error": "question_text and a non-empty string options array are required"}
    correct_index = body.get("correct_index")
    if correct_index is not None and (isinstance(correct_index, bool) or
                                      not isinstance(correct_index, int) or
                                      not 0 <= correct_index < len(options)):
        return {"ok": False, "error": "correct_index must be null or a valid option index"}
    explanation = body.get("explanation")
    cur.execute("""
        SELECT q.id,q.identity_key,q.source_identity,q.file_ref_id,
               q.source_question_key,q.raw,q.corrected_at,q.question_number
        FROM questions q
        WHERE q.id=%s AND q.canonical_id IS NULL FOR UPDATE OF q
    """, (qid,))
    old = cur.fetchone()
    if not old:
        return {"ok": False, "error": "question not found or already merged"}
    new_identity = identity(text, options)
    source_identity = old[2] or old[1] or new_identity
    file_ref_id, source_key, raw, old_corrected_at, question_number = old[3:8]
    raw = raw if isinstance(raw, dict) else {}
    source_question = str(raw.get("q") or "")
    source_number = str(question_number or "")
    if not source_number and source_key and source_key.startswith("n:"):
        source_number = source_key[2:]
    if not source_number:
        match = re.match(r"^\s*(\d+)\s*[.)]", source_question)
        if match:
            source_number = match.group(1)
    correct_text = options[correct_index] if correct_index is not None else None
    cur.execute("""
        UPDATE questions SET question_text=%s,options=%s,correct_index=%s,
            correct_text=%s,explanation=%s,identity_key=%s,source_identity=%s,
            source_question_key=COALESCE(source_question_key,%s),
            identity_version=2,corrected_at=now(),quality_score=%s
        WHERE id=%s
    """, (text, Json(options), correct_index, correct_text, explanation,
          new_identity, source_identity,
          source_key or source_question_key(question_number or None, source_question, raw.get("options") or []),
          compute_quality_score(explanation, options, len(text)), qid))
    if body.get("set_source_correction"):
        cur.execute("""SELECT c.id,b.book_name,f.subtopic,f.file_id
            FROM files f JOIN books b ON b.id=f.book_id JOIN chapters c ON c.id=b.chapter_id
            WHERE f.id=%s""", (file_ref_id,))
        location = cur.fetchone()
        if location:
            chapter_id, book_name, subtopic, _source_file_id = location
            source_location = source_location_key(book_name, subtopic)
            source_key = source_key or source_question_key(
                question_number or source_number or None, source_question, raw.get("options") or [])
            cur.execute("""INSERT INTO correction_source_aliases_v2
                (chapter_id,source_identity,source_location,source_number,question_text,
                 options,correct_index,correct_text,explanation,identity_key,quality_score,corrected_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                (chapter_id,source_identity,source_location,
                 (source_key[2:] if source_key.startswith("n:") else ""),
                 text,Json(options),correct_index,correct_text,explanation,new_identity,
                 compute_quality_score(explanation,options,len(text))))
    cur.execute("""
        UPDATE dedupe_review_candidates SET status='pending',review_note=NULL,
            reviewed_at=NULL
        WHERE (q1_id=%s OR q2_id=%s) AND status='rejected'
    """, (qid, qid))

    return {"ok": True, "id": qid, "identity_key": new_identity}


def api_review_candidate(body, cur):
    try:
        candidate_id = int(body.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "candidate id must be an integer"}
    action = str(body.get("action") or "").strip()
    ensure_review_schema(cur)
    cur.execute("""SELECT id,chapter_id,q1_id,q2_id,status,
            q1_identity_key,q2_identity_key FROM dedupe_review_candidates
        WHERE id=%s FOR UPDATE""", (candidate_id,))
    row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "candidate not found"}
    _cid, chapter_id, q1_id, q2_id, status, q1_identity, q2_identity = row
    if status != "pending":
        return {"ok": False, "error": f"candidate already {status}"}

    note = str(body.get("note") or "").strip()[:500]
    if action in {"reject", "not_duplicate"}:
        cur.execute("""UPDATE dedupe_review_candidates SET status='rejected',
            review_note=%s,reviewed_at=now() WHERE id=%s""", (note, candidate_id))
        return {"ok": True, "status": "rejected"}

    if action == "edit":
        edits = body.get("edits")
        if not isinstance(edits, list) or not edits:
            return {"ok": False, "error": "edits must contain question edits"}
        for edit in edits:
            if not isinstance(edit, dict):
                return {"ok": False, "error": "each edit must be an object"}
        try:
            edit_ids = [int(item.get("id", -1)) for item in edits if isinstance(item, dict)]
        except (TypeError, ValueError):
            return {"ok": False, "error": "edit ids must be integers"}
        if len(edit_ids) != len(edits) or any(edit_id not in (q1_id, q2_id) for edit_id in edit_ids):
            return {"ok": False, "error": "edits may only target these candidate questions"}
        for edit in edits:
            result = api_edit_question({**edit, "set_source_correction": True}, cur)
            if not result.get("ok"):
                raise ValueError(result.get("error", "edit failed"))
        cur.execute("""UPDATE dedupe_review_candidates SET status='rejected',
            review_note=%s,reviewed_at=now() WHERE id=%s""",
                    (note or "questions edited; regenerate candidates", candidate_id))
        return {"ok": True, "status": "edited"}

    if action not in {"merge", "exact_merge"}:
        return {"ok": False, "error": "action must be reject, edit, or merge"}
    try:
        keeper_id = int(body.get("keeper_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "keeper_id must select one of the two candidates"}
    if keeper_id not in (q1_id, q2_id):
        return {"ok": False, "error": "keeper_id must be q1_id or q2_id"}
    duplicate_id = q2_id if keeper_id == q1_id else q1_id
    cur.execute("""SELECT q.id,c.id,q.question_text,q.options,q.correct_index,q.correct_text,
               q.explanation,q.raw,q.identity_key
        FROM questions q JOIN files f ON f.id=q.file_ref_id
        JOIN books b ON b.id=f.book_id JOIN chapters c ON c.id=b.chapter_id
        WHERE q.id=ANY(%s) AND q.canonical_id IS NULL FOR UPDATE OF q
    """, ([keeper_id, duplicate_id],))
    questions = cur.fetchall()
    if len(questions) != 2 or any(q[1] != chapter_id for q in questions):
        return {"ok": False, "error": "candidate questions changed or are not in the recorded chapter"}
    questions_by_id = {question[0]: question for question in questions}
    if (q1_identity is None or q2_identity is None
            or identity(questions_by_id[q1_id][2],questions_by_id[q1_id][3] or []) != q1_identity
            or identity(questions_by_id[q2_id][2],questions_by_id[q2_id][3] or []) != q2_identity):
        return {"ok": False, "error": "candidate is stale; refresh review candidates before approval"}

    keeper = next(q for q in questions if q[0] == keeper_id)
    duplicate = next(q for q in questions if q[0] == duplicate_id)
    keeper_answer_key = _answer_key(keeper[5], keeper[4], keeper[3])
    duplicate_answer_key = _answer_key(duplicate[5], duplicate[4], duplicate[3])
    if keeper_answer_key.startswith("__") or duplicate_answer_key.startswith("__"):
        return {"ok": False, "error": "both questions need valid, consistent correct answers before merging"}
    if keeper_answer_key != duplicate_answer_key:
        return {"ok": False, "error": "answer conflict; edit both answers to agree before merging"}

    before = {"question": keeper[2], "options": keeper[3],
              "correct_index": keeper[4], "correct_text": keeper[5],
              "explanation": keeper[6], "raw": keeper[7]}
    edits = body.get("edits") or []
    if action == "merge" and edits:
        edit_map = {int(item["id"]): item for item in edits}
        for edit_id, edit in edit_map.items():
            if edit_id not in (q1_id, q2_id):
                return {"ok": False, "error": "edits may only target the two candidate questions"}
            text = str(edit.get("question_text") or "").strip()
            opts = edit.get("options")
            ci = edit.get("correct_index")
            if not text or not isinstance(opts, list) or not opts or not all(isinstance(x, str) for x in opts):
                return {"ok": False, "error": "each edit needs question text and a non-empty string options array"}
            if ci is not None and (isinstance(ci, bool) or not isinstance(ci, int) or not 0 <= ci < len(opts)):
                return {"ok": False, "error": "edit has invalid correct_index"}
            cur.execute("SELECT id,identity_key,source_identity,file_ref_id,source_question_key,raw,corrected_at,question_number FROM questions WHERE id=%s FOR UPDATE", (edit_id,))
            old_edit = cur.fetchone()
            if not old_edit:
                raise ValueError("question disappeared during edit")
            raw = old_edit[5] if isinstance(old_edit[5], dict) else {}
            new_identity = identity(text, opts)
            source_identity = old_edit[2] or old_edit[1] or new_identity
            source_key = old_edit[4] or source_question_key(old_edit[7], raw.get("q", ""), raw.get("options") or [])
            correct_text = opts[ci] if ci is not None else None
            cur.execute("""UPDATE questions SET question_text=%s,options=%s,
                correct_index=%s,correct_text=%s,explanation=%s,identity_key=%s,
                source_identity=%s,source_question_key=%s,identity_version=2,
                corrected_at=now(),quality_score=%s WHERE id=%s""",
                (text,Json(opts),ci,correct_text,edit.get("explanation"),new_identity,
                 source_identity,source_key,compute_quality_score(edit.get("explanation"),opts,len(text)),edit_id))
            location_cur = cur.connection.cursor()
            location_cur.execute("""SELECT c.id,b.book_name,f.subtopic
                FROM files f JOIN books b ON b.id=f.book_id JOIN chapters c ON c.id=b.chapter_id
                WHERE f.id=%s""", (old_edit[3],))
            location = location_cur.fetchone()
            location_cur.close()
            if location:
                chapter_id_for_edit, book_name, subtopic = location
                cur.execute("""INSERT INTO correction_source_aliases_v2
                    (chapter_id,source_identity,source_location,source_number,question_text,
                     options,correct_index,correct_text,explanation,identity_key,quality_score,corrected_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                    (chapter_id_for_edit,source_identity,source_location_key(book_name,subtopic),
                     source_key[2:] if source_key.startswith("n:") else "",text,Json(opts),ci,
                     correct_text,edit.get("explanation"),new_identity,
                     compute_quality_score(edit.get("explanation"),opts,len(text))))
        cur.execute("SELECT correct_text,correct_index,options FROM questions WHERE id=%s", (keeper_id,))
        keeper_answer = cur.fetchone()
        cur.execute("SELECT correct_text,correct_index,options FROM questions WHERE id=%s", (duplicate_id,))
        duplicate_answer = cur.fetchone()
        if (_answer_key(*keeper_answer).startswith("__")
                or _answer_key(*duplicate_answer).startswith("__")
                or _answer_key(*keeper_answer) != _answer_key(*duplicate_answer)):
            return {"ok": False, "error": "edited answers conflict; make them agree before merge"}
    merge_question_rows(cur, keeper_id, duplicate_id,
                        reason="manual_near" if action == "merge" else "manual_exact",
                        signature=identity(keeper[2], keeper[3] or []),
                        child_fks=discover_question_child_fks(cur),
                        keeper_before=before)
    cur.execute("""UPDATE dedupe_review_candidates SET status='merged',
        review_note=%s,reviewed_at=now() WHERE id=%s""", (note, candidate_id))
    return {"ok": True, "status": "merged", "keeper_id": keeper_id,
            "deleted_id": duplicate_id}


def _answer_key(value, correct_index=None, options=None):
    from dedupe.content_identity import answer_identity
    return answer_identity(value, correct_index, options)


def _answer_from_question(correct_text, correct_index, options):
    if correct_text is not None and str(correct_text).strip():
        return correct_text
    if (isinstance(correct_index, int) and not isinstance(correct_index, bool)
            and isinstance(options, (list, tuple))
            and 0 <= correct_index < len(options)):
        return options[correct_index]
    return None
