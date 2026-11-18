"""
levels_data.py
--------------
Chapter/subtopic structure and file manifests for Level 5 and GK.
Level 7 is already loaded and does not appear here.

Use with setup_new_levels.py
"""

# =====================================================================
# CHAPTER / SUBTOPIC STRUCTURE (from the PSC syllabus PDF)
# =====================================================================

LEVEL5_STRUCTURE = {
    "label": "Level 5 — Diploma",
    "chapters": {
        "1": {"name": "Surveying", "subtopics": {
            "1.1": "General",
            "1.2": "Levelling",
            "1.3": "Plane Tabling",
            "1.4": "Theodolite and Traverse",
            "1.5": "Contouring",
            "1.6": "Setting Out",
        }},
        "2": {"name": "Construction Materials", "subtopics": {
            "2.1": "Stone",
            "2.2": "Cement",
            "2.3": "Clay and Clay Products",
            "2.4": "Paints and Varnishes",
            "2.5": "Bitumen",
        }},
        "3": {"name": "Mechanics of Materials and Structures", "subtopics": {
            "3.1": "Mechanics of Materials",
            "3.2": "Mechanics of Beams",
            "3.3": "Simple Strut Theory",
        }},
        "4": {"name": "Hydraulics", "subtopics": {
            "4.1": "General",
            "4.2": "Hydro-Kinematics and Dynamics",
            "4.3": "Measurement of Discharge",
            "4.4": "Flows",
        }},
        "5": {"name": "Soil Mechanics", "subtopics": {
            "5.1": "General",
            "5.2": "Soil Water Relation",
            "5.3": "Compaction of Soil",
            "5.4": "Shear Strength of Soils",
            "5.5": "Earth Pressures",
            "5.6": "Foundation Engineering",
        }},
        "6": {"name": "Structural Design", "subtopics": {
            "6.1": "RC Sections in Bending",
            "6.2": "Shear and Bond for RC",
            "6.3": "Axially Loaded RC Columns",
            "6.4": "Design and Drafting of RC",
        }},
        "7": {"name": "Building Construction Technology", "subtopics": {
            "7.1": "Foundations",
            "7.2": "Walls",
            "7.3": "Damp Proofing",
            "7.4": "Concrete Technology",
            "7.5": "Wood Work",
            "7.6": "Flooring and Finishing",
        }},
        "8": {"name": "Water Supply and Sanitation", "subtopics": {
            "8.1": "General",
            "8.2": "Gravity Water Supply System",
            "8.3": "Design of Sewer",
            "8.4": "Excreta Disposal",
        }},
        "9": {"name": "Irrigation Engineering", "subtopics": {
            "9.1": "General",
            "9.2": "Crop Water Requirement",
            "9.3": "Irrigation Canals",
        }},
        "10": {"name": "Highway Engineering", "subtopics": {
            "10.1": "General",
            "10.2": "Geometric Design",
            "10.3": "Drainage System",
            "10.4": "Road Pavement",
            "10.5": "Road Machineries",
            "10.6": "Road Construction Technology",
            "10.7": "Bridge",
            "10.8": "Road Maintenance and Repair",
            "10.9": "Tracks and Trails",
        }},
        "11": {"name": "Estimating and Costing", "subtopics": {
            "11.1": "General",
            "11.2": "Rate Analysis",
            "11.3": "Specifications",
            "11.4": "Valuation",
        }},
        "12": {"name": "Construction Management", "subtopics": {
            "12.1": "Organization",
            "12.2": "Site Management",
            "12.3": "Contract Procedure",
            "12.4": "Accounts",
            "12.5": "Planning and Control",
        }},
        "13": {"name": "Airport Engineering", "subtopics": {
            "13.1": "General",
            "13.2": "Design",
            "13.3": "Airport Maintenance",
        }},
    },
}

GK_STRUCTURE = {
    "label": "General Knowledge (shared)",
    "chapters": {
        "1": {"name": "General Awareness", "subtopics": {
            "1.1":  "GEO&DEMO",
            "1.2":  "NAT RESOURCES",
            "1.3":  "PERIODIC PLAN",
            "1.4":  "SUST DEV&ENV",
            "1.5":  "SCIENCE & TECH",
            "1.6":  "PUBLIC HEALTH",
            "1.7":  "CONSTITUTION",
            "1.8":  "INTL AFFAIRS",
            "1.9":  "REGIONAL ORGS",
            "1.10": "CURRENT AFFAIRS",
        }},
        "2": {"name": "Management", "subtopics": {
            "2.1":  "OFFICE MGMT",
            "2.2":  "CIVIL SERVICE ACT",
            "2.3":  "FEDERAL MINISTRY",
            "2.4":  "CONSTITUTIONAL BODIES",
            "2.5":  "BUDGET & ACCOUNTING",
            "2.6":  "SERVICE DELIVERY",
            "2.7":  "GOVERNANCE",
            "2.8":  "PUBLIC CHARTER",
            "2.9":  "MGMT FUNDAMENTALS",
            "2.10": "HUMAN VALUES",
        }},
        "3": {"name": "IQ", "subtopics": {
            "3.1": "Logical Reasoning",
            "3.2": "Numerical Reasoning",
            "3.3": "Spatial Reasoning",
        }},
    },
}



LEVEL5_FILES = [
    # --- Chapter 1 Surveying ---
    ("1", "Sunil Sah", "Surveying 1-100",       "1OAlD5XUf-Ecmj4hNViPAqInI5GUcMExG"),
    ("1", "Sunil Sah", "Surveying 101-200",     "1Q6E7isqILUnHG9ZPxTsIltIlEodqr05i"),
    ("1", "Sunil Sah", "Surveying 201-300",     "1vZRNltiqTuJxJn8RVCzrSojNeyRYFEfp"),
    ("1", "Sunil Sah", "Sunil Surveying 301-400", "13z92Vn1uV7Gw217q-GXQVuOQoV9gUrJO"),
    ("1", "Sunil Sah", "Sunil Surveying 401-500", "1OikW1FEi5Zuei4IrWpbjsTr3-O_hY8PQ"),
    ("1", "Sunil Sah", "Sunil Surveying 501-575", "1WUg2w7SlHVpAEyOlFyfbnymQ-xivfwNi"),

    # --- Chapter 2 Construction Material ---
    ("2", DEFAULT_L5_BOOK, "1-100 sunil",   "1DCi7TZlsRLXbswMXZ_phkNSvpvR4qEYC"),
    ("2", DEFAULT_L5_BOOK, "101-200 sunil", "1l2_oKmLGjbMZJAY2EXniIcsXBjgox1LI"),
    ("2", DEFAULT_L5_BOOK, "201-300 sunil", "1D-Q5Dx7r_PeLb8tuQSJrfdDsSFwje__V"),
    ("2", DEFAULT_L5_BOOK, "301-400 sunil", "1Ofpj_R63e8ibarImI4Kx4Hjk1GZ5aknd"),
    ("2", DEFAULT_L5_BOOK, "401-500 sunil", "1WLSUMqyN8bnj9WuQPGRNxK0ssMqDtJ-O"),
    ("2", DEFAULT_L5_BOOK, "501-613 sunil", "1bQ-eFt4DnPTkejie6Jf435EtGEiwVobO"),

    # --- Chapter 3 Mechanics ---
    ("3", "DPARSAD",   "01_Stress_Strain_Elastic_Properties_Q1-53",  "1RWIKVWCl9djWzUyAL0fMOoeNLIYQfQAo"),
    ("3", "DPARSAD",   "02_Shear_Force_Bending_Moment_Basics_Q54-116","18XMCDrjLIHKW8aHJYleGOgsnBFX5ANyH"),
    ("3", "DPARSAD",   "03_Bending_Deflection_Theory_Q117-164",     "1zkZh7YFEn_-fyk1QfxBtl3VvQ5aY-Euk"),
    ("3", "DPARSAD",   "04_Beam_Diagram_Relations_Thrust_Q165-187", "1ZOWTP4bQeuvcwui5xkjqppdCUBY_WT-0"),
    ("3", "DPARSAD",   "05_Columns_Struts_Ties_Trusses_Q188-221",   "1_1IxcEHCZPlqN5JfT0agDZXoUxBmiroN"),
    ("3", "DPARSAD",   "06_General_SOM_Revision_Mixed_Q222-250",    "1q3-aYC9cL7u7Ga4r5kGgGVE9BCiipX8J"),
    ("3", "Sunil Sah", "sunil 1-100 updated",   "18PHUlO1w4w1P6fAJFl1sVA-vuaArgX4c"),
    ("3", "Sunil Sah", "suni 101-200 updated",  "1WqWfdmpL-boetcfcaRVkAD07U2wScgMz"),
    ("3", "Sunil Sah", "suni 201-300 updated",  "1P75cTo6Emx6cKMjhKpSHObrcIZK_Q48Z"),
    ("3", "Sunil Sah", "suni 301-400 updated",  "1iltEhT0DIWa98sAnRSZs7l83Sl5Fp4PA"),
    ("3", "Sunil Sah", "suni 401-432 updated",  "1H1u78Q95fPXDAzALAwMF2PyrKYqwIluc"),

    # --- Chapter 4 Hydraulics ---
    ("4", DEFAULT_L5_BOOK, "Sunil 1-100",   "1W0Haw_2D00dCGnytzmtiDG40WXiW356m"),
    ("4", DEFAULT_L5_BOOK, "Sunil 101-200", "1q0lScj2EGQYv7n16ZY2qbluOG_xWtgd_"),
    ("4", DEFAULT_L5_BOOK, "Sunil 201-300", "1--gbS8anKXq77VRjm76vm1JzPBQ81NjY"),
    ("4", DEFAULT_L5_BOOK, "Sunil 301-400", "1TcZcXzv7A7eQP_WK7EEySIfWVESgF-fQ"),
    ("4", DEFAULT_L5_BOOK, "Sunil 401-450", "1YwIBiSps43xKr3d5qeODQpW4bQcEFTNj"),
    ("4", DEFAULT_L5_BOOK, "Sunil 451-520", "1WOcNHBJKZJzcjKDTKz215XvGN7N3o7WR"),

    # --- Chapter 5 Soil Mechanics ---
    ("5", DEFAULT_L5_BOOK, "Soil Sunil 1-100",   "11DZsjZfw4WbmglOxGRErYh9VBNv1-yy7"),
    ("5", DEFAULT_L5_BOOK, "Soil Sunil 101-200", "1l6ZBNY7MlRItTKOsglSF9xDGEQ_hrVZI"),
    ("5", DEFAULT_L5_BOOK, "Soil Sunil 201-300", "1ZMPdpgCvJ4LNPVSr0enHKyGxVIr7QLth"),
    ("5", DEFAULT_L5_BOOK, "Soil Sunil 301-400", "1zKOJP55egY2xSu8yxTbQZwRulKyyqwb3"),

    # --- Chapter 6 Structural Design ---
    ("6", DEFAULT_L5_BOOK, "Struct Sunil 1-100",   "1ZTRwGwGkdg6DpZzVizUQEkF-Z1IQT3a2"),
    ("6", DEFAULT_L5_BOOK, "Struct Sunil 101-200", "1NJIDXdgssUhX0QcnmIIN0QLWQjuP-gjs"),
    ("6", DEFAULT_L5_BOOK, "Struct Sunil 201-300", "1n7Qn2gqNo6du6XKb7AjwBypuJIKKqXEd"),
    ("6", DEFAULT_L5_BOOK, "Struct Sunil 301-360", "1utWod1N1YyvWcxXTa-UkD6YobEPmVXri"),
    ("6", DEFAULT_L5_BOOK, "Struct Sunil 361-417", "1i9tauS85s-o8G3L49QgmBaE-6isjbRLW"),

    # --- Chapter 7 Building Construction Tech ---
    ("7", "RK Shrestha", "R.K 1-65",      "1kzYm9czns3Do26a2XV8-tTm5VSJ_TUXt"),
    ("7", "RK Shrestha", "R.K 65-130",    "164FLjujhfBl-q2Q_CaYcuW0T89fY1avd"),
    ("7", "RK Shrestha", "RK 131-195",    "19DsKXwT_RSX1B_xlQHz06tR0J8HLNf1C"),
    ("7", "RK Shrestha", "R.K 196-260",   "1mZP6ujsccyC8OlKwMyGssf9t4st7-sPB"),
    ("7", "Sunil Sah", "BT Sunil 1-100",   "1H8b2DIcDQQ4dCDRaJctM6mYOyMMa7Rh-"),
    ("7", "Sunil Sah", "BT Sunil 101-200", "1jYggTJbHhYxZDvroz5XIk-1O-I-trv5A"),
    ("7", "Sunil Sah", "BT Sunil 201-300", "15f2CiEgfd0y45C6YiAujV35bpvHBpGnG"),
    ("7", "Sunil Sah", "BT Sunil 301-400", "1upPz6YXp7yLLjz73lnzUP828EFApy-Mb"),
    ("7", "Sunil Sah", "BT Sunil 401-500", "1eVtbEWc9LGsLty0Y2ZM6PDeIk2ykBP2b"),
    ("7", "Sunil Sah", "BT Sunil 501-600", "1_tytL1YFi_8glzjswiGKelgkQrvNNAfQ"),
    ("7", "Sunil Sah", "BT Sunil 601-658", "1V_Cgasu59PipCReiiMRzcqeKDHwTfDoe"),
    ("7", "Sunil Sah", "BT Sunil 659-716", "161Hw8Db80fggFIBHTkzKwiDqm9oApAA7"),

    # --- Chapter 8 Water & Sanitation ---
    ("8", "Sunil Sah", "WSunil 1-100",   "1oCYIwNj8h6SdiOP4bB5HNG3cyX6ZRygd"),
    ("8", "Sunil Sah", "WSunil 101-200", "1lSJuN-fvaBRsyABUNPApWm09rZn_V0ko"),
    ("8", "Sunil Sah", "WSunil 201-300", "1aEj_hw63qbOfJsIp1AwnyulTRIQ-e1xh"),
    ("8", "Sunil Sah", "WSunil 301-400", "1tHiL_rKWaNRHd0y2yphJpElMNKBLDiGq"),
    ("8", "Sunil Sah", "WSunil 401-500", "1A1Hh0YqmMLsLE7-Ey-9YrYHbRYccE7iZ"),
    ("8", "Sunil Sah", "WSunil 501-554", "1LnUcKO28KnhVXIerrj003UsrLAiTdUoP"),

    # --- Chapter 9 Irrigation ---
    ("9", DEFAULT_L5_BOOK, "irr sunil 1-100",   "1SFVGZBZCmcsMlBRLuv3rKjOr-ocKuJTy"),
    ("9", DEFAULT_L5_BOOK, "irr sunil 101-200", "1CeQH3i49wF9dc0e6mIznjiSA3A9Qxf3u"),
    ("9", DEFAULT_L5_BOOK, "irr sunil 201-300", "1ho8sdNzmE9YHBfUCBWg86ti4oH8w_UxE"),
    ("9", DEFAULT_L5_BOOK, "irr sunil 301-376", "1Zg4rlXqtmFgUctS53I_ga_gtj-u3KEPC"),
    ("9", DEFAULT_L5_BOOK, "irr sunil 376-408", "1rcHJY4DnOgABTsz1JrrwUlLcfU7VWKhm"),

    # --- Chapter 10 Highway ---
    ("10", "Sunil Sah", "H Sunil 1-100",   "1oglXPjLqCifewdj8-0MDx-UI5oCVyJ0G"),
    ("10", "Sunil Sah", "H Sunil 101-200", "1DJWlgozHY4-Obn4FAzL4ITZZJIiyCcrD"),
    ("10", "Sunil Sah", "H Sunil 201-300", "1NdciqWDMenHJd9Nl2euJaazASJRkYbtE"),
    ("10", "Sunil Sah", "H Sunil 301-400", "1ZDhz_MWRSrGpOqNXj69-Plkl2RRaja9M"),
    ("10", "Sunil Sah", "H Sunil 401-449", "1KEops4saZRcQGJPtcFAunGue8A392Qwy"),

    # --- Chapter 11 Estimating ---
    ("11", "Sunil Sah",   "Sunil 1-50",              "1PatlHpX83cgMO8VH9bbOq6aRoifCNoNW"),
    ("11", "Sunil Sah",   "sunil 50-100",            "1S82Lnx41zlFQx4-H7bGWW1Zt-I7zCeSx"),
    ("11", "Sunil Sah",   "sunil 101-150",           "1pN1as3DjClVrYhEWBKXXwR2n4Egd4IIc"),
    ("11", "Sunil Sah",   "Sunil 151-200",           "1WdqpEn0eSgZzhbT7X5ycF6m57bpRctBZ"),
    ("11", "Sunil Sah",   "sunil 201-260 updated",   "1WmIZf9XFN9CUPxJ9rzwBf6NvE_qrje42"),
    ("11", "Sunil Sah",   "sunil 261-312 updated",   "1wJZMh8dJYUF4Pm-qa80sU0sYMYX8hhKj"),
    ("11", "RK Shrestha", "RK Shrestha estimating 1-50", "1ivzRvvI9ZqXyyin4ncwW-GQIzECHOEDF"),
    ("11", "RK Shrestha", "Estimating Rk 50-100",    "1RLHdLWtDPgQnpNDpO4fdRGBHwMI0LLsX"),
    ("11", "RK Shrestha", "Estimating Rk 101-183",   "193NC9O8OnKkqdC8dXCxjzvB-sYnD49_q"),

    # --- Chapter 12 Construction Management ---
    ("12", "Sunil Sah", "C.MSunil 1-100",    "1atj3Pt2St3Ag_9Lp1IIKfyFfzyES4jCu"),
    ("12", "Sunil Sah", "C.M sunil 101-200", "1EgH0tKtUJQVmsopLeh61lXTTDzMqbMy6"),
    ("12", "Sunil Sah", "C.M sunil201-300",  "1ErTJa6lzuCmqtcWMFMH-bwWQnsTPQByU"),
    ("12", "Sunil Sah", "C.M sunil 301-348", "10YYfufwvVqTi5XKlSlDQRzeap99HI8Lo"),

    # --- Chapter 13 Airport ---
    ("13", "Sunil Sah",      "Air Sunil 1-70",    "1W_tOzVueuNMTJEj4Zuwxkk4TxaSyHMm1"),
    ("13", "Sunil Sah",      "Air Sunil 71-154",  "1t8HiHvCUclSdZ_le0PzOe-D5a77xsVpj"),
    ("13", DEFAULT_L5_BOOK,  "L5-airport dparsad", "1uxYrB-uf5NSsrjV51lL7hsrvdlDfWP0i"),
]


GK_FILES = [
    # --- Chapter 1 General Awareness ---
    ("1", "GATE", "constitution",                     "1HRlsrjjxF8tW89R4fT9wTGjAnFt0gfVS"),
    ("1", "GATE", "international affairs",            "1pomfoXhK1mWU7QAO-oihx2SMsPJqxOCh"),
    ("1", "GATE", "international relations 101-150",  "1TTD1aKKl_P6nGcUxzyyDkpbH2O7hPx_u"),
    ("1", "GATE", "international relations 151-180",  "1dEdvbPuxrSKys_jlMAJkaHGLuSHQ47fF"),
    ("1", "GATE", "Current Plan",                     "1S39S--nt9QVepKlcszdpntB8rRgAEqfd"),
    ("1", "GATE", "Sustainable Development",          "1922CKz8p81DWUWhimygloqKDNDQXbXXF"),
    ("1", "iNTERNATIONAL AFFAIRS", "saarc_mcq_batch1", "1mgbD6Zr3t5zu7oGAraj1BeIXOqbGSojp"),
    ("1", "iNTERNATIONAL AFFAIRS", "saarc_mcq_batch2", "1SWybO9ZCwzFPvAY6k_8StQk9Nf04MZzm"),

    # --- Chapter 2 Public Management ---
    ("2", "GATE", "nijamati ain",                     "1W628YzIRlatpN0BhdtVqv1V7q44Fn8F0"),
    ("2", "GATE", "governance",                       "1xO4bk4QCPzqCORupW07MtNj-60OFlqZE"),
    ("2", "GATE", "Functional Scope Public Service",  "1TdUQchW6i2LBKdWsjMNNTGwB5hAR1iCB"),
    ("2", "GATE", "Citizen Charter",                  "1SkPP7n4nIjmdEzWu8zZ5nywQaY2BmGQD"),
    ("2", "GATE", "fundamentals of management",       "1G5BtPfzG_bnmDhIORtJM6N5cmCEiAt3A"),
    ("2", "GATE", "Public Policy",                    "1u3hA3p7wtUaDvzDioLVrFaq02WUuNCpt"),
    ("2", "planning and management", "fundamentals of management",        "1Gzu0Or4R4TufdNbRAfIvuWsstiNRJzJ3"),
    ("2", "planning and management", "Fundamentals of management part 2","1pqYGgBageMaEsa6QZoCcqzpjr1Bpd3DA"),
]