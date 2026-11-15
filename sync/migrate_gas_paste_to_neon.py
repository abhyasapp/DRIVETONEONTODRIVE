#!/usr/bin/env python3
"""
migrate_gas_paste_to_neon.py
-----------------------------
One-time migration of the GAS Q-reports rows the user pasted (after clearing
the sheet). Inserts directly into Neon reported_questions (idempotent upsert).
"""
import json
import os

from dotenv import load_dotenv

from utils.db import connect as db_connect

load_dotenv()

# Column order in the GAS sheet:
# id uid fileId questionSnapshot reason note reportedBy reportedAt status
# optionsSnapshot correctIndex chosenIndex section appVersion resolvedAt
# resolvedBy corrected correctedSnapshot qid
ROWS = [
    ["d157c349-d5be-46f3-9d74-e1ffb244f530", "1EG0uEIOS9dSx91PmGfWHvDR6UtCRv1D6_9", "1EG0uEIOS9dSx91PmGfWHvDR6UtCRv1D6", "55. The section modulus of a rectangular section is proportional to", "wrong_answer", "option c is correct", "abhyas", "2026-09-01T01:38:34.669Z", "open", "optionsSnapshot", "", "", "section", "appVersion", "", "", "", "", ""],
    ["f73c0b82-b78e-4973-b25d-f38c684cc703", "1ulh8RD7_hHeBUyrRD95kW_bgyvKQsk51_51", "1ulh8RD7_hHeBUyrRD95kW_bgyvKQsk51", "191. In general equation for the deflection of the beam: M=EI*  where EI is", "wrong_answer", "", "077bce046", "2026-10-23T05:50:09.157Z", "open", '["young\'s modulus of elasticity","flexural strength","stifness modulus","sectional rigidity"]', "1", "3", "results", "1.35", "", "", "", "", ""],
    ["51c4bad0-0c17-44ec-93ee-b30cca0e371e", "149Jiv13N8z5n2UdmhfE8RaXnGiQPz21z_63", "149Jiv13N8z5n2UdmhfE8RaXnGiQPz21z", "64. Bituminous fells are used for", "wrong_answer", "c", "077bce046", "2026-10-23T05:52:31.631Z", "open", '["covering A.C. sheets","covering sloping roofs","D.P.C.","none to these"]', "0", "2", "results", "1.35", "", "", "", "", ""],
    ["39be08d5-49b2-4a42-a0ea-b5f76275e1e3", "1F8NrUyduFKFqDzoDq48CZjLag-xQXCYe_9", "1F8NrUyduFKFqDzoDq48CZjLag-xQXCYe", "502. A reverse curve consists of", "wrong_answer", "diffrent direcions", "077bce046", "2026-10-25T03:53:47.303Z", "open", '["a single curve of a circle connecting two straights","two arcs of different radii bending in the same direction","two arcs of equal radii bending in the same direction","two arcs of equal or different radii bending in the same direction"]', "3", "3", "daily-paper", "1.35", "", "", "", "", ""],
    ["721a4c6a-31ed-4a87-9094-33c3b6ddf4ec", "188fC1VajbIZ-LPReuCBmh0hpd3ORgp7U_17", "188fC1VajbIZ-LPReuCBmh0hpd3ORgp7U", "18. The drawing provided by a client for the tender work to the contractor is known as", "wrong_answer", "", "077bce046", "2026-10-25T04:11:00.611Z", "open", '["concept drawing","working drawing","as built drawing","structural drawing"]', "0", "1", "results", "1.35", "", "", "", "", ""],
    ["3bab0b88-1b6c-42ce-a62c-4ab3e627db89", "1bzgiKA9qYcxqkYdKT0lJzvlra6XWe0oE_33", "1bzgiKA9qYcxqkYdKT0lJzvlra6XWe0oE", "94. The numbers of clause in Nepal engineering council act is ........", "wrong_answer", "", "077bce046", "2026-10-25T04:11:18.504Z", "open", '["35","36","37","38"]', "3", "2", "results", "1.35", "", "", "", "", ""],
    ["1df95a9e-1aed-4c17-8c0d-352ba1958073", "1CJHlW5OLb6Brs52WVc1jX9sXKVaw6Bpr_7", "1CJHlW5OLb6Brs52WVc1jX9sXKVaw6Bpr", "8. Match Group I and II...", "typo", "", "077bce046", "2026-10-27T04:07:04.761Z", "open", '["Refer to table","...","...","..."]', "1", "", "daily-paper", "1.35", "", "", "", "", ""],
    ["bbe31673-4e5f-4fac-a722-aad567d22e73", "1pUm2R7pXdID7pjlTvmRlDui7FfltBr0x_20", "1pUm2R7pXdID7pjlTvmRlDui7FfltBr0x", "21. At either end of a plane frame, maximum number of possible bending moments, are", "wrong_answer", "", "077bce046", "2026-10-27T04:11:15.741Z", "open", '["one","two","three","zero"]', "3", "0", "results", "1.35", "", "", "", "", ""],
    ["14989407-3199-4699-8e60-7de003e20e49", "1hValUmDeveADXR6iEdKeAsHqODOrAVdc_5", "1hValUmDeveADXR6iEdKeAsHqODOrAVdc", "6. At either end of a plane frame, maximum number of possible bending moments, are", "wrong_answer", "", "077bce046", "2026-10-27T04:11:31.893Z", "open", '["one","two","three","zero"]', "3", "0", "results", "1.35", "", "", "", "", ""],
    ["5b49f0df-27b5-4cca-bcd5-fd6d7ac3565e", "11UWRoG-JKMxE28HF44iUGYidJ46XIi17_39", "11UWRoG-JKMxE28HF44iUGYidJ46XIi17", "90. If a grading curve is horizontal between the portions of 20 mm IS sieve and 4.75 mm IS sieve, graded aggregates do not contain.", "wrong_answer", "discuss", "077bce046", "2026-10-27T04:12:05.569Z", "open", '["20 mm particles","10 mm particles","4.75 mm particles","all of the above"]', "3", "1", "results", "1.35", "", "", "", "", ""],
    ["fc4b59ab-c1a5-41f2-8c03-9fc2a50ff823", "1SItSfY3zEKsc3REoOHQXV_mpHXSS5jxf_6", "1SItSfY3zEKsc3REoOHQXV_mpHXSS5jxf", "7. Which among the following is not true about benefit cost analysis?", "wrong_answer", "C", "abhyasbymku", "2026-10-27T16:49:50.546Z", "open", '["cost and benefits must be converted to a common time base","a public project is normally acceptable if the B-C ratio is at least one","the analysis makes sure that the benefits are at least 10-20% more than the costs","the B-C ratio is a ratio of discounted benefits to discounted costs"]', "1", "2", "practice", "2.01", "", "", "", "", ""],
    ["9359fa75-e202-4ee2-a101-a085b5aae56b", "1V7H3z14ViPpn_XwWzW72cICfMizkaEmM_18", "1V7H3z14ViPpn_XwWzW72cICfMizkaEmM", "19. From which body did Nepal's Citizen Charter first originate?", "wrong_answer", "", "077bce046", "2026-11-01T03:17:12.604Z", "open", '["Office of the Prime Minister and Council of Ministers","Ministry of Federal Affairs and General Administration","Ministry of Home Affairs","Ministry of Finance"]', "2", "0", "results", "1.35", "", "", "", "", ""],
    ["bd5f44bb-52d3-420f-890a-ce6e102978b1", "1ae85BxIN_v2UQ96SiNnl02GneoaAcQyO_1", "1ae85BxIN_v2UQ96SiNnl02GneoaAcQyO", "2. What is/are the features of Nepalese federal system? I. Separation of Powers II. Division of Powers III. Independent Judiciary IV. A Written Constitution", "wrong_answer", "all of above", "077bce046", "2026-11-03T01:16:32.073Z", "open", '["I, III","I, III, IV","I, II","I, III, IV"]', "3", "1", "daily-paper", "1.35", "", "", "", "", ""],
]

PLACEHOLDERS = {"optionsSnapshot", "correctIndex", "chosenIndex", "section", "appVersion",
                "resolvedAt", "resolvedBy", "corrected", "correctedSnapshot", "qid"}


def to_int(v):
    v = (v or "").strip()
    if not v or v in PLACEHOLDERS:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def to_text(v):
    v = (v or "").strip()
    if not v or v in PLACEHOLDERS:
        return None
    return v


def to_options(v):
    v = (v or "").strip()
    if not v or v in PLACEHOLDERS:
        return None
    try:
        parsed = json.loads(v)
        return v if isinstance(parsed, list) else None
    except (json.JSONDecodeError, ValueError):
        return None


def main():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reported_questions (
            id TEXT PRIMARY KEY, uid TEXT, qid BIGINT, file_id TEXT,
            question_snapshot TEXT, options_snapshot TEXT, correct_index INTEGER,
            chosen_index INTEGER, reason TEXT, note TEXT, reported_by TEXT,
            reported_at TIMESTAMPTZ, status TEXT, resolved_at TIMESTAMPTZ,
            resolved_by TEXT, corrected BOOLEAN DEFAULT FALSE,
            corrected_snapshot TEXT, section TEXT, app_version TEXT,
            migrated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.commit()

    inserted = 0
    for r in ROWS:
        (rid, uid, file_id, qsnap, reason, note, reported_by, reported_at, status,
         opts, ci, cj, section, app_version, resolved_at, resolved_by, corrected,
         corrected_snap, qid) = r
        cur.execute("""
            INSERT INTO reported_questions
               (id, uid, qid, file_id, question_snapshot, options_snapshot,
                correct_index, chosen_index, reason, note, reported_by,
                reported_at, status, resolved_at, resolved_by, corrected,
                corrected_snapshot, section, app_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
              uid=EXCLUDED.uid, qid=EXCLUDED.qid, file_id=EXCLUDED.file_id,
              question_snapshot=EXCLUDED.question_snapshot,
              options_snapshot=EXCLUDED.options_snapshot,
              correct_index=EXCLUDED.correct_index,
              chosen_index=EXCLUDED.chosen_index,
              reason=EXCLUDED.reason, note=EXCLUDED.note,
              reported_by=EXCLUDED.reported_by, reported_at=EXCLUDED.reported_at,
              status=EXCLUDED.status, resolved_at=EXCLUDED.resolved_at,
              resolved_by=EXCLUDED.resolved_by, corrected=EXCLUDED.corrected,
              corrected_snapshot=EXCLUDED.corrected_snapshot,
              section=EXCLUDED.section, app_version=EXCLUDED.app_version,
              migrated_at=now()
        """, (
            rid, uid, to_int(qid), file_id, qsnap, to_options(opts),
            to_int(ci), to_int(cj), to_text(reason), to_text(note),
            to_text(reported_by), to_text(reported_at), to_text(status),
            to_text(resolved_at), to_text(resolved_by), False,
            to_text(corrected_snap), to_text(section), to_text(app_version),
        ))
        inserted += 1
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM reported_questions")
    total = cur.fetchone()[0]
    print(f"Migrated {inserted} GAS rows; Neon now has {total} rows total")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()