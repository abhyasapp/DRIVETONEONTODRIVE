#!/usr/bin/env python3
"""
list_project_files.py
---------------------
Walks a folder (default: the drivetoneon project root) and reports every
file. Supports filtering, exclusion of noisy folders, tree view, and
extension grouping.

Usage:
    python list_project_files.py                       # summary of project root
    python list_project_files.py --tree                # directory tree
    python list_project_files.py --path .              # explicit path
    python list_project_files.py --ext .py .json       # only these extensions
    python list_project_files.py --exclude venv logs abhyas_export
    python list_project_files.py --csv files.csv       # write CSV

Defaults exclude: venv, .venv, __pycache__, .git, node_modules,
_ patch_backup_*, abhyas_export
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime


DEFAULT_EXCLUDES = {
    "venv", ".venv", "__pycache__", ".git",
    "node_modules", "abhyas_export",
}
DEFAULT_EXCLUDE_PREFIXES = ("_patch_backup_",)


def should_skip(path, excludes, exclude_prefixes):
    name = os.path.basename(path)
    if name in excludes:
        return True
    for pfx in exclude_prefixes:
        if name.startswith(pfx):
            return True
    return False


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def walk(root, excludes, exclude_prefixes):
    """Yield (relpath, abspath, size, mtime)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not should_skip(os.path.join(dirpath, d),
                               excludes, exclude_prefixes)
        ]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield rel, full, st.st_size, st.st_mtime


def matches_ext(path, exts):
    if not exts:
        return True
    lowered = path.lower()
    for e in exts:
        if not e.startswith("."):
            e = "." + e
        if lowered.endswith(e.lower()):
            return True
    return False


def render_summary(root, files):
    by_ext = defaultdict(lambda: {"count": 0, "size": 0})
    total_size = 0
    for rel, full, size, mtime in files:
        ext = os.path.splitext(rel)[1].lower() or "(no ext)"
        by_ext[ext]["count"] += 1
        by_ext[ext]["size"] += size
        total_size += size

    print(f"\nRoot: {os.path.abspath(root)}")
    print(f"Files: {len(files)}   Total size: {human_size(total_size)}\n")

    print(f"{'ext':<12}{'files':>8}{'size':>14}")
    print("-" * 34)
    for ext in sorted(by_ext, key=lambda e: -by_ext[e]["size"]):
        c = by_ext[ext]["count"]
        s = human_size(by_ext[ext]["size"])
        print(f"{ext:<12}{c:>8}{s:>14}")


def render_list(files):
    print(f"\n{'size':>10}  {'modified':<19}  path")
    print("-" * 80)
    for rel, full, size, mtime in sorted(files, key=lambda x: x[0].lower()):
        mtime_s = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{human_size(size):>10}  {mtime_s:<19}  {rel}")


def render_tree(root, files, excludes, exclude_prefixes):
    """Print a compact tree with file counts per folder."""
    # Build a dict keyed by directory -> list of files
    dirs = defaultdict(list)
    for rel, full, size, mtime in files:
        d = os.path.dirname(rel)
        dirs[d].append(os.path.basename(rel))

    def walk_dir(rel_dir, indent=0):
        pad = "  " * indent
        if rel_dir == "":
            print(f"{pad}{os.path.basename(os.path.abspath(root)) or root}/")
        for name in sorted(dirs.get(rel_dir, [])):
            print(f"{pad}  {name}")
        subdirs = sorted({
            d for d in dirs
            if (os.path.dirname(d) == rel_dir) and d != rel_dir
        })
        for sub in subdirs:
            print(f"{pad}  {os.path.basename(sub)}/")
            walk_dir(sub, indent + 2)

    walk_dir("")


def write_csv(path, files):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "size_bytes", "modified_iso"])
        for rel, full, size, mtime in sorted(files, key=lambda x: x[0].lower()):
            w.writerow([
                rel,
                size,
                datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            ])
    print(f"\nWrote {path}  ({len(files)} rows)")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="List all files under a folder.")
    p.add_argument("--path", default=here,
                   help="Folder to walk. Defaults to this script's folder.")
    p.add_argument("--ext", nargs="*", default=None,
                   help="Only these extensions, e.g. --ext .py .json")
    p.add_argument("--exclude", nargs="*", default=[],
                   help="Folder names to skip (adds to defaults).")
    p.add_argument("--include-all", action="store_true",
                   help="Do not exclude any folder (walks venv/abhyas_export too).")
    p.add_argument("--tree", action="store_true",
                   help="Show a directory tree instead of the flat list.")
    p.add_argument("--summary", action="store_true",
                   help="Show a summary by extension instead of the flat list.")
    p.add_argument("--csv", metavar="FILE",
                   help="Write results to CSV instead of printing.")
    args = p.parse_args()

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"ERROR: not a directory: {root}")
        sys.exit(1)

    if args.include_all:
        excludes = set()
        prefixes = ()
    else:
        excludes = DEFAULT_EXCLUDES | set(args.exclude)
        prefixes = DEFAULT_EXCLUDE_PREFIXES

    files = list(walk(root, excludes, prefixes))
    files = [f for f in files if matches_ext(f[0], args.ext)]

    if not files:
        print("No files matched.")
        return

    if args.csv:
        write_csv(args.csv, files)
        return

    if args.tree:
        render_tree(root, files, excludes, prefixes)
    elif args.summary:
        render_summary(root, files)
    else:
        render_list(files)
        print(f"\nTotal: {len(files)} file(s)")


if __name__ == "__main__":
    main()