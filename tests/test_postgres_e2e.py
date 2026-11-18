"""End-to-end checks against a REAL PostgreSQL (not mocks, not PGlite).

Runs only when TEST_DATABASE_URL points at a local database; it drops and
recreates the public schema there, so it refuses anything that is not
localhost. It never reads DATABASE_URL, so it cannot touch Neon.

    TEST_DATABASE_URL=postgresql://user:pw@127.0.0.1/abhyastest \\
        python -m unittest tests.test_postgres_e2e

CI runs it against a throwaway postgres service container.
"""
import json
import os
import unittest
from unittest import mock
from urllib.parse import urlparse

URL = os.getenv("TEST_DATABASE_URL", "")
LOCAL = urlparse(URL).hostname in ("localhost", "127.0.0.1", "::1") if URL else False
SCHEMA = os.path.join(os.path.dirname(__file__), "fixtures", "schema_min.sql")


@unittest.skipUnless(URL and LOCAL, "set TEST_DATABASE_URL to a local Postgres to run")
class PostgresEndToEnd(unittest.TestCase):
    def test_reports_and_corrections_loop(self):
        os.environ["DATABASE_URL"] = URL
        from utils.db import connect
        from utils.admin_sql import ensure_installed, is_installed
        from utils import backend
        from sync import sync_reports as sr, admin_apply, apply_corrections as ac

        conn = connect()
        cur = conn.cursor()
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        with open(SCHEMA, "r", encoding="utf-8") as fh:
            cur.execute(fh.read())
        conn.commit()

        def check(label, cond, detail=None):
            self.assertTrue(cond, f"{label}: {detail!r}")

        ac.ensure_schema(cur, conn)
        conn.commit()
        check("SQL not installed yet", not is_installed(cur))
        check("ensure_installed installs the whole SQL file from psycopg2", ensure_installed(conn) is True)
        check("second call is a no-op", ensure_installed(conn) is False)

        # fixtures
        cur.execute("INSERT INTO levels (id, name) VALUES (1,'level5') ON CONFLICT DO NOTHING") if False else None
        conn.commit()
        cur.execute("select column_name from information_schema.columns where table_name='questions' order by ordinal_position")
        print("questions cols:", [r[0] for r in cur.fetchall()])

        # ---- fixtures: one chapter/book/file, two questions (one merged into the other)
        cur.execute("INSERT INTO chapters (id, chapter_name, level_id) VALUES (1,'Hydraulics',1)")
        cur.execute("INSERT INTO books (id, chapter_id, book_name) VALUES (1,1,'Sunil Sah')")
        cur.execute("INSERT INTO files (id, book_id, subtopic, file_id) VALUES (1,1,'Pumps','drv-file-1')")
        for qid, text, qn in ((501,'Pump head unit?','50011.110001'),(502,'Pump head unit??','50011.110002')):
            cur.execute("""INSERT INTO questions (id, question_number, question_text, options, correct_index,
                           correct_text, identity_key, source_identity, file_ref_id, stable_qn_id, source_question_key)
                           VALUES (%s,1,%s,%s::jsonb,1,'Pa','old-ident','src-1',1,%s,%s)""",
                        (qid, text, json.dumps(["m","Pa","W"]), qn, f"n:{qid}"))
        cur.execute("UPDATE questions SET canonical_id=501 WHERE id=502")
        conn.commit()

        # ---- pull: upsert real rows, paginated through a fake backend
        reports = [
          {"id":"rep-1","uid":"u1","qid":501,"fileId":"drv-file-1","questionSnapshot":"Pump head unit?","optionsSnapshot":["m","Pa","W"],
           "correctIndex":1,"chosenIndex":0,"reason":"wrong answer","reportedAt":"2026-11-10T00:00:00Z","status":"open","section":"hydraulics","appVersion":"1.2"},
          {"id":"rep-2","uid":"u2","qid":502,"reason":"typo","reportedAt":"2026-11-11T00:00:00Z","status":"open"},
          {"id":"rep-3","uid":"u3","qid":None,"reason":"unlinked","reportedAt":"2026-11-12T00:00:00Z","status":None},
        ]
        calls = []
        def fake_call(action, **kw):
            calls.append((action, kw))
            if action == "syncreportssince":
                return {"success": True, "reports": reports if kw["since"] == 0 else [], "now": 1700000000000}
            if action == "resolvereports":
                ids = [i["id"] for i in kw["items"]]
                return {"success": True, "updated": ids, "missing": [], "failed": []}
        with mock.patch.object(backend, "call", fake_call):
            st = sr.run(conn, full=True)
        check("first run pulled 3 reports", st["pulled"] == 3, st)
        cur.execute("select id, status, qid, backend_dirty from reported_questions order by id"); rows = cur.fetchall()
        check("rows stored; missing status defaults to open", [r[1] for r in rows] == ["open","open","open"], rows)
        check("watermarks saved", sr.get_setting(cur, sr.K_SINCE) == 1700000000000 and sr.get_setting(cur, sr.K_FULL) > 0)
        check("no reconcile before any correction", st["reconciled"] == 0, st)
        check("nothing pending to push", st["pending"] == 0 and st["push_status"] == "nothing", st)

        # ---- correct by qn id through the real admin_apply -> process_correction
        fix = [{"qn":"50011.110001","question":"Unit of pump head?","options":["metre","Pascal","Watt"],"correctIndex":0,"explanation":"head is a length"}]
        info = {}
        res = admin_apply.run(fix, apply=True, conn=conn, info=info)
        check("admin_apply applied the correction", res and res[0][1] is True, res)
        cur.execute("select question_text, correct_index, correct_text from questions where id=501")
        q = cur.fetchone(); check("question rewritten in Neon", q[0]=="Unit of pump head?" and q[1]==0 and q[2]=="metre", q)
        check("both reports (incl. the one on the merged duplicate) closed", sorted(info.get("reports_resolved", [])) == ["rep-1","rep-2"], info)
        cur.execute("select id,status,corrected,backend_dirty from reported_questions order by id")
        check("closed reports flagged for push-back; unlinked one untouched",
              cur.fetchall() == [("rep-1","resolved",True,True),("rep-2","resolved",True,True),("rep-3","open",False,False)])

        # ---- push back
        calls.clear()
        with mock.patch.object(backend, "call", fake_call):
            st = sr.run(conn)
        resolve_calls = [c for c in calls if c[0]=="resolvereports"]
        check("push sent exactly the two closed reports", resolve_calls and sorted(i["id"] for i in resolve_calls[0][1]["items"])==["rep-1","rep-2"], calls)
        check("push stats ok", st["pushed"]==2 and st["push_status"]=="ok", st)
        cur.execute("select count(*) from reported_questions where backend_dirty"); check("nothing pending afterwards", cur.fetchone()[0]==0)

        # ---- pull must not clobber a pending local change
        cur.execute("UPDATE reported_questions SET status='resolved', backend_dirty=true WHERE id='rep-3'"); conn.commit()
        with mock.patch.object(backend, "call", lambda a, **k: {"success":True,"reports":reports,"now":1700000000001} if a=="syncreportssince" else {"success":True,"updated":[]}):
            sr.run(conn, full=True, do_push=False)
        cur.execute("select status from reported_questions where id='rep-3'")
        check("a pending local resolve survives a pull that says 'open'", cur.fetchone()[0]=="resolved")

        # ---- unknown backend endpoint keeps changes pending
        def old_backend(action, **kw):
            if action == "resolvereports": raise backend.UnknownAction("Unknown action")
            return {"success": True, "reports": [], "now": 1700000000002}
        with mock.patch.object(backend, "call", old_backend):
            st = sr.run(conn)
        check("old backend: warning only, changes stay pending", st["push_status"]=="endpoint-missing" and st["pending"]>=1, st)

        # ---- admin_apply edge cases on real data
        again = admin_apply.run(fix, apply=True, conn=conn, info={})
        cur.execute("select count(*) from admin_corrections")
        check("identical resubmission is a no-op (no second audit row)", again[0][1] is True and "no change" in again[0][2] and cur.fetchone()[0]==1, again)
        batch = admin_apply.run([
            {"qn":"nope.999","question":"x?","options":["a","b"],"correctIndex":0},
            {"qn":"50011.110001","question":"Unit of pump head, again?","options":["metre","Pascal","Watt"],"correctIndex":0},
            {"qn":"50011.110001","question":"y","options":["a","b"],"correctIndex":9},
        ], apply=True, conn=conn, info={})
        check("unknown id fails, valid item still applies, bad index rejected, in one batch",
              [r[1] for r in batch]==[False, True, False], batch)
        cur.execute("select count(*) from admin_corrections"); check("two audits now (initial + second edit)", cur.fetchone()[0]==2)
        cur.execute("select abhyas_revert_correction(max(id)) from admin_corrections"); conn.commit()
        cur.execute("select question_text from questions where id=501")
        check("undo of the 2nd admin_apply correction restores the 1st", cur.fetchone()[0]=="Unit of pump head?")

        # ---- undo via the SQL function reopens reports
        cur.execute("select max(id) from admin_corrections where reverted_at is null"); aud = cur.fetchone()[0]
        cur.execute("select abhyas_revert_correction(%s)", (aud,)); conn.commit()
        cur.execute("select id,status from reported_questions where id in ('rep-1','rep-2') order by id")
        check("undo reopens exactly the closed reports", cur.fetchall()==[("rep-1","open"),("rep-2","open")])
        cur.execute("select question_text from questions where id=501"); check("undo restores the question text", cur.fetchone()[0]=="Pump head unit?")

        # ---- dry run writes nothing
        cur.execute("select count(*) from applied_corrections"); before = cur.fetchone()[0]
        res = admin_apply.run(fix, apply=False, conn=conn)
        cur.execute("select count(*) from applied_corrections"); check("dry run changes nothing", cur.fetchone()[0]==before and res[0][1] is not None)

        cur.close()
        conn.close()


if __name__ == "__main__":
    unittest.main()
