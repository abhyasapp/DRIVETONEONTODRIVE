"""Offline smoke test - needs no credentials, DB or network.

    python tests/smoke.py

Checks:
  1. every .py file parses
  2. every pipeline step points at an existing module
  3. every in-repo import resolves to a real file
  4. every third-party import is covered by requirements.txt
  5. keyword JSON files are valid
Exit code 1 on any problem (used as a CI gate before the real run).
"""

import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

SKIP_DIRS = {".git", "archive", "logs", "__pycache__", "tests", "venv", ".venv"}
LOCAL_PKGS = {d for d in os.listdir(ROOT)
              if os.path.isdir(d) and d not in SKIP_DIRS and not d.startswith(".")}
PIP_FOR = {            # import root -> distribution name in requirements.txt
    "psycopg2": "psycopg2-binary",
    "dotenv": "python-dotenv",
    "google": None,    # google.* handled below
    "googleapiclient": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "openai": "openai",
}
GOOGLE_DISTS = {"google-auth", "google-genai", "google-auth-oauthlib",
                "google-auth-httplib2", "google-api-python-client"}

problems = []


def fail(msg):
    problems.append(msg)
    print("  FAIL", msg)


def py_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def requirements():
    names = set()
    for line in open("requirements.txt", encoding="utf-8"):
        line = line.split("#")[0].strip()
        if line:
            names.add(re.split(r"[<>=!~\[ ]", line)[0].lower())
    return names


def main():
    reqs = requirements()
    files = list(py_files())
    print(f"[1] parsing {len(files)} python files")
    trees = {}
    for p in files:
        try:
            trees[p] = ast.parse(open(p, encoding="utf-8").read(), p)
        except SyntaxError as e:
            fail(f"{os.path.relpath(p, ROOT)}: syntax error line {e.lineno}")

    print("[2] pipeline steps -> modules")
    from pipeline.run_pipeline import STEPS
    for step in STEPS:
        mod = step[2]
        if not os.path.isfile(os.path.join(ROOT, *mod.split(".")) + ".py"):
            fail(f"step '{step[0]}' module missing: {mod}")

    print("[3/4] imports")
    stdlib = sys.stdlib_module_names
    for p, tree in trees.items():
        rel = os.path.relpath(p, ROOT)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for full in names:
                root = full.split(".")[0]
                if root in stdlib:
                    continue
                if root in LOCAL_PKGS:
                    target = os.path.join(ROOT, *full.split("."))
                    if not (os.path.isfile(target + ".py") or os.path.isdir(target)):
                        fail(f"{rel}: local import not found: {full}")
                    continue
                if root == "google":
                    if not (reqs & GOOGLE_DISTS):
                        fail(f"{rel}: google import but no google-* in requirements")
                    continue
                dist = PIP_FOR.get(root, root).lower() if PIP_FOR.get(root, root) else root
                if dist not in reqs:
                    fail(f"{rel}: '{full}' not covered by requirements.txt ({dist})")

    print("[5] keyword JSON")
    for n in ("l5", "gk", "l7"):
        path = os.path.join("config", f"{n}_keywords.json")
        try:
            json.load(open(path, encoding="utf-8"))
        except Exception as e:
            fail(f"{path}: {e}")

    print()
    if problems:
        print(f"SMOKE TEST FAILED ({len(problems)} problem(s))")
        sys.exit(1)
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
