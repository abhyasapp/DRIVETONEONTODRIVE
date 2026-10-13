"""Central settings: paths, Drive parent, keyword files.

Override Drive parent with env DRIVE_PARENT_ID without editing code.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRIVE_PARENT = os.getenv("DRIVE_PARENT_ID", "").strip() \
    or "1_gAGILhdleeR6gBUfEFSKOVpa8x5wkCE"

KEYWORD_FILE_FOR = {
    "level5": os.path.join(ROOT, "config", "l5_keywords.json"),
    "gk":     os.path.join(ROOT, "config", "gk_keywords.json"),
    "level7": os.path.join(ROOT, "config", "l7_keywords.json"),
}
