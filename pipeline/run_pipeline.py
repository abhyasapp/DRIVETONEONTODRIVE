#!/usr/bin/env python3
"""
run_pipeline.py  (v6.2 - with corrections step)
------------------------------------------------
Daily pipeline: sync -> corrections -> dedupe -> classify -> export -> upload -> chapters

Steps (11):
   1. sync         sync_any_level.py all
   2. corrections  apply_corrections.py --apply
   3. dedupe       dedupe_exact.py --apply --quiet
   4. kw_l5        classify_any_level.py level5   / run in
   5. kw_l7        classify_any_level.py level7    |  parallel
   6. kw_gk        classify_any_level.py gk       /
   7. gemini       classify_with_gemini.py all
   8. export       export_any_level.py all
   9. upload       upload_any_level.py all --workers 6
  10. chapters     generate_chapters_data_all.py
  11. chapters_up  upload_chapters_data.py

Behaviour:
  * Exit code is NON-ZERO if pre-flight fails, dedupe aborts, or any step
    fails (so GitHub Actions turns red instead of green).
  * --quiet still prints the last lines of any failing step (tracebacks).
  * Optional steps are skipped cleanly when their script is missing.
  * UTF-8 safe.
  * Each step is its own subprocess -- one failure doesn't stop the rest.
  * Dedupe failure STOPS the pipeline.
  * Streams every line to terminal AND today's log.
"""

import argparse
import collections
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime

try:
    import ctypes
except ImportError:  # pragma: no cover
    ctypes = None

try:
    import psycopg2
except ImportError:
    psycopg2 = None

from dotenv import load_dotenv

# ════════════════════════════════════════════════════════════════════════
# Paths + .env (loaded from the script directory, not the CWD)
# ════════════════════════════════════════════════════════════════════════

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT, ".env"))

LOG_DIR = os.path.join(PROJECT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"pipeline_{date.today().isoformat()}.log")


# ════════════════════════════════════════════════════════════════════════
# Python interpreter
# ════════════════════════════════════════════════════════════════════════

def find_python():
    if sys.platform.startswith("win"):
        cands = [
            os.path.join(PROJECT, "venv", "Scripts", "python.exe"),
            os.path.join(PROJECT, ".venv", "Scripts", "python.exe"),
        ]
    else:
        cands = [
            os.path.join(PROJECT, "venv", "bin", "python"),
            os.path.join(PROJECT, ".venv", "bin", "python"),
        ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return sys.executable


VENV_PY = find_python()


# ════════════════════════════════════════════════════════════════════════
# Step definitions
# ════════════════════════════════════════════════════════════════════════

def _env_int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Gemini can be very slow on a big backlog. Locally the default is 8h; in CI set
# GEMINI_STEP_TIMEOUT (seconds) below the job timeout so the later steps
# (export / upload / chapters) still get to run.
GEMINI_TIMEOUT = _env_int("GEMINI_STEP_TIMEOUT", 28800)

STEPS = [
    ("sync",        "Sync from Drive",
     "sync.sync_any_level", ["all"],                        1800),
    ("corrections", "Apply corrections to Neon",
     "sync.apply_corrections", ["--apply"],                  600),
    ("dedupe",      "Exact dedupe",
     "dedupe.dedupe_exact", ["--apply", "--quiet"],            900),
    ("kw_l5",       "Keyword classify level5",
     "classify.classify_any_level", ["level5"],                  600),
    ("kw_l7",       "Keyword classify level7",
     "classify.classify_any_level", ["level7"],                  600),
    ("kw_gk",       "Keyword classify gk",
     "classify.classify_any_level", ["gk"],                      600),
    ("gemini",      "Gemini classify",
     "classify.classify_with_gemini", ["all"],       GEMINI_TIMEOUT),
    ("export",      "Export to JSON",
     "export.export_any_level", ["all"],                       900),
    ("upload",      "Upload JSON to Drive",
     "upload.upload_any_level", ["all", "--workers", "6"],    1800),
    ("chapters",    "Generate chapters-data",
     "export.generate_chapters_data_all", [],                  300),
    ("chapters_up", "Upload chapters-data",
     "upload.upload_chapters_data", [],                        300),
]

# Steps whose names start with this run in parallel with each other.
PARALLEL_PREFIX = "kw_"

# If any of these fail, stop the pipeline.
FATAL_STEPS = {"dedupe"}

# Steps that must NOT be retried automatically.
NO_RETRY_STEPS = {"gemini", "upload", "corrections"}

# Steps skipped when the script isn't present.
OPTIONAL_STEPS = set()   # step names that may be skipped if their script is absent

ALL_STEP_NAMES = [s[0] for s in STEPS]

# Which steps need which kind of credentials.
DB_STEPS = {"sync", "corrections", "dedupe", "kw_l5", "kw_l7", "kw_gk",
            "gemini", "export", "chapters", "chapters_up"}
DRIVE_STEPS = {"sync", "upload", "chapters_up"}

# Lines we still print in --quiet mode (headers + totals).
QUIET_KEEP = ("Pipeline ", "Total", "Done", "FAILED", "ERROR",
              "OK --", "Wrote ", "===")


# ════════════════════════════════════════════════════════════════════════
# Active-process registry
# ════════════════════════════════════════════════════════════════════════

_active_procs = set()
_active_procs_lock = threading.Lock()


# ════════════════════════════════════════════════════════════════════════
# Logging
# ════════════════════════════════════════════════════════════════════════

_log_lock = threading.Lock()


def log(fh, msg, prefix=None):
    ts = datetime.now().strftime("%H:%M:%S")
    head = f"[{ts}]"
    if prefix:
        head += f" [{prefix}]"
    line = f"{head} {msg}"
    with _log_lock:
        print(line, flush=True)
        if fh is not None:
            try:
                fh.write(line + "\n")
                fh.flush()
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════
# Process helpers
# ════════════════════════════════════════════════════════════════════════

def _windows_graceful_break(pid):
    if ctypes is None or not hasattr(ctypes, "windll"):
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GenerateConsoleCtrlEvent(1, pid)
        return True
    except Exception:
        return False


def kill_tree(proc):
    if proc is None or proc.poll() is not None:
        return

    if sys.platform.startswith("win"):
        _windows_graceful_break(proc.pid)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.2)

        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return

    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.2)

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _pump(proc, q):
    try:
        for line in proc.stdout:
            q.put(line)
    except Exception:
        pass
    finally:
        q.put(None)


def module_exists(dotted):
    """True if `python -m <dotted>` can find the module (no import executed
    for the leaf; the parent package is resolved from PROJECT)."""
    path = os.path.join(PROJECT, *dotted.split(".")) + ".py"
    return os.path.isfile(path)


TAIL_LINES = 40


def run_step(name, label, script, args, timeout, fh, quiet=False):
    # `script` is a dotted module path like "sync.sync_any_level".
    # subprocess.Popen runs it with cwd=PROJECT so the package is importable.
    if not module_exists(script):
        log(fh, f"  ! script not found: {script}", prefix=name)
        return False, 0.0, "missing script"

    tail = collections.deque(maxlen=TAIL_LINES)
    cmd = [VENV_PY, "-u", "-m", script] + args
    log(fh, f"  $ {' '.join(cmd)}", prefix=name)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    popen_kwargs = {}
    if sys.platform.startswith("win"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    started = time.time()
    deadline = started + timeout if timeout else None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=PROJECT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            **popen_kwargs,
        )
    except Exception as e:
        log(fh, f"  ! error launching: {e}", prefix=name)
        return False, 0.0, "launch failed"

    with _active_procs_lock:
        _active_procs.add(proc)

    q = queue.Queue()
    threading.Thread(target=_pump, args=(proc, q), daemon=True).start()

    eof = False
    killed = False
    last_activity = time.time()

    try:
        while not eof:
            now = time.time()

            if deadline and now > deadline and not killed:
                log(fh, f"  ! exceeded {timeout}s -- killing tree", prefix=name)
                kill_tree(proc)
                killed = True
                last_activity = now

            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None and (time.time() - last_activity) > 5.0:
                    break
                continue

            if line is None:
                eof = True
                break

            last_activity = time.time()
            tail.append(line.rstrip("\n\r"))

            if not quiet:
                log(fh, line.rstrip("\n\r"), prefix=name)
            elif line.strip().startswith(QUIET_KEEP):
                log(fh, line.rstrip("\n\r"), prefix=name)

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        with _active_procs_lock:
            _active_procs.discard(proc)

    elapsed = time.time() - started
    rc = proc.returncode if proc else -1
    ok = (rc == 0) and not killed

    # In --quiet mode the traceback would otherwise be lost: dump the tail.
    if quiet and not ok and tail:
        log(fh, f"  --- last {len(tail)} lines of {name} ---", prefix=name)
        for t in tail:
            log(fh, f"  | {t}", prefix=name)
        log(fh, "  --- end ---", prefix=name)

    return ok, elapsed, ("killed" if killed else f"exit {rc}")


# ════════════════════════════════════════════════════════════════════════
# Pre-flight checks
# ════════════════════════════════════════════════════════════════════════

def preflight(step_names):
    problems = []

    needs_db = bool(set(step_names) & DB_STEPS)
    needs_drive = bool(set(step_names) & DRIVE_STEPS)

    if needs_db and not os.getenv("DATABASE_URL"):
        problems.append("DATABASE_URL not set in .env")

    if needs_drive:
        if not os.path.exists(os.path.join(PROJECT, "service-account.json")):
            problems.append("service-account.json missing")
        # Drive upload auth: JSON token in env (CI) OR local pickle file.
        if not os.getenv("GDRIVE_OAUTH_TOKEN") and not os.path.exists(
                os.path.join(PROJECT, "oauth-token.pickle")):
            problems.append("no Drive OAuth: set GDRIVE_OAUTH_TOKEN or create "
                            "oauth-token.pickle (run upload once interactively)")

    if needs_db and psycopg2 is not None and os.getenv("DATABASE_URL"):
        # Neon (and other serverless Postgres) may need a while to wake up,
        # so retry a few times with a generous timeout.
        last_err = None
        for attempt in range(1, 5):
            try:
                conn = psycopg2.connect(os.getenv("DATABASE_URL"),
                                        connect_timeout=30)
                conn.close()
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(5 * attempt)
        if last_err is not None:
            problems.append(f"DB connect failed after 4 tries: {last_err}")

    return problems


# ════════════════════════════════════════════════════════════════════════
# Conditional skip logic
# ════════════════════════════════════════════════════════════════════════

def mixedqn_count():
    if psycopg2 is None or not os.getenv("DATABASE_URL"):
        return None
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"), connect_timeout=10)
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(DISTINCT qs.question_id)
            FROM question_subtopics qs
            JOIN subtopics s ON s.id = qs.subtopic_id
            WHERE s.code = 'mixedqn'
              AND qs.classified_by IS DISTINCT FROM 'deferred'
        """)
        n = cur.fetchone()[0]
        cur.close()
        conn.close()
        return n
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════
# Argparse
# ════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="Daily pipeline for ABHYAS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Step names:\n  " + ", ".join(ALL_STEP_NAMES),
    )
    p.add_argument("--only", default=None,
                   help="Comma-separated step names to run exclusively.")
    p.add_argument("--skip", default=None,
                   help="Comma-separated step names to skip.")
    p.add_argument("--from-step", default=None,
                   help="Start from this step name onward.")
    p.add_argument("--skip-gemini", action="store_true",
                   help="Alias for --skip gemini.")
    p.add_argument("--serial", action="store_true",
                   help="Disable parallel kw_* execution.")
    p.add_argument("--quiet", action="store_true",
                   help="Log only headers + summary lines from steps.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan and exit without running anything.")
    p.add_argument("--list", action="store_true",
                   help="List available step names and exit.")
    return p.parse_args()


# ════════════════════════════════════════════════════════════════════════
# Step selection
# ════════════════════════════════════════════════════════════════════════

def _parse_csv(s):
    if not s:
        return set()
    return {x.strip() for x in s.split(",") if x.strip()}


def _reject_unknown(names, label):
    bad = names - set(ALL_STEP_NAMES)
    if bad:
        print(f"Unknown step name(s) for {label}: {', '.join(sorted(bad))}")
        print("Known:", ", ".join(ALL_STEP_NAMES))
        sys.exit(1)


def select_steps(args):
    wanted = _parse_csv(args.only) if args.only else None
    if wanted is not None:
        _reject_unknown(wanted, "--only")

    skip = _parse_csv(args.skip)
    if skip:
        _reject_unknown(skip, "--skip")
    if args.skip_gemini:
        skip.add("gemini")

    from_idx = 0
    if args.from_step:
        if args.from_step not in ALL_STEP_NAMES:
            print(f"Unknown step: {args.from_step}")
            print("Known:", ", ".join(ALL_STEP_NAMES))
            sys.exit(1)
        from_idx = ALL_STEP_NAMES.index(args.from_step)

    selected = []
    for i, step in enumerate(STEPS):
        name = step[0]
        if i < from_idx:
            continue
        if wanted is not None and name not in wanted:
            continue
        if name in skip:
            continue
        selected.append(step)
    return selected


def group_parallel(steps, allow_parallel):
    if not allow_parallel:
        return [("single", s) for s in steps]

    runs = []
    buf = []
    for s in steps:
        if s[0].startswith(PARALLEL_PREFIX):
            buf.append(s)
        else:
            if buf:
                runs.append(("parallel", buf))
                buf = []
            runs.append(("single", s))
    if buf:
        runs.append(("parallel", buf))
    return runs


# ════════════════════════════════════════════════════════════════════════
# Parallel runner
# ════════════════════════════════════════════════════════════════════════

def run_parallel(group, fh, quiet):
    results = [None] * len(group)

    def _worker(idx, step):
        name, label, script, args, timeout = step
        try:
            ok, elapsed, note = run_step(name, label, script, args,
                                         timeout, fh, quiet)
            if (not ok
                    and name not in FATAL_STEPS
                    and name not in NO_RETRY_STEPS
                    and note != "missing script"):
                log(fh, f"  retrying {name} after {note} ...", prefix=name)
                ok2, elapsed2, note = run_step(name, label, script, args,
                                               timeout, fh, quiet)
                ok = ok2
                elapsed += elapsed2
            results[idx] = (step, ok, elapsed, note)
        except Exception as e:
            results[idx] = (step, False, 0.0, f"exception: {e}")

    threads = []
    for i, s in enumerate(group):
        t = threading.Thread(target=_worker, args=(i, s), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return results


# ════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    def _on_signal(signum, frame):
        try:
            print(f"\n[!] signal {signum} -- killing all running steps ...",
                  flush=True)
        except Exception:
            pass
        with _active_procs_lock:
            procs = list(_active_procs)
        for p in procs:
            try:
                kill_tree(p)
            except Exception:
                pass
        sys.exit(130)

    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:
        pass

    args = parse_args()

    if args.list:
        print("Available step names:")
        for name, label, script, sa, t in STEPS:
            print(f"  {name:<12}  {label}  [{script}]")
        return

    selected = select_steps(args)
    if not selected:
        print("No steps selected.")
        return

    selected_names = [s[0] for s in selected]
    runs = group_parallel(selected, allow_parallel=not args.serial)

    if args.dry_run:
        print("DRY-RUN -- nothing will execute.\n")
        for kind, payload in runs:
            if kind == "single":
                print(f"  {payload[0]}")
            else:
                names = [s[0] for s in payload]
                print(f"  {' + '.join(names)}   (parallel)")
        return

    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        log(fh, "=" * 66)
        log(fh, f"Pipeline start -- {datetime.now():%Y-%m-%d %H:%M:%S}")
        log(fh, f"Python: {VENV_PY}")
        log(fh, f"Steps:  {selected_names}")
        log(fh, f"Mode:   {'serial' if args.serial else 'parallel kw_*'}"
                f"{' quiet' if args.quiet else ''}")
        log(fh, "=" * 66)

        problems = preflight(selected_names)
        if problems:
            log(fh, "Pre-flight failed:")
            for p in problems:
                log(fh, f"  - {p}")
            log(fh, "Fix the above and retry.")
            sys.exit(2)
        log(fh, "Pre-flight OK")

        results = []
        aborted = False
        skip_gemini_check_done = False

        for kind, payload in runs:
            if kind == "single":
                name, label, script, sa, timeout = payload

                if name == "gemini" and not skip_gemini_check_done:
                    n = mixedqn_count()
                    skip_gemini_check_done = True
                    if n == 0:
                        log(fh, "  (skipped -- no mixedqn rows left to classify)",
                            prefix="gemini")
                        results.append((name, True, 0.0, "skipped"))
                        continue

                log(fh, "-" * 60)
                log(fh, f">> {name}  --  {label}")
                log(fh, "-" * 60)

                if name in OPTIONAL_STEPS and not module_exists(script):
                    log(fh, f"  (skipped -- optional script {script} not found)",
                        prefix=name)
                    results.append((name, True, 0.0, "skipped (missing)"))
                    continue

                ok, elapsed, note = run_step(name, label, script, sa,
                                             timeout, fh, args.quiet)

                if (not ok
                        and name not in FATAL_STEPS
                        and name not in NO_RETRY_STEPS
                        and note != "missing script"):
                    log(fh, f"  retrying {name} after {note} ...", prefix=name)
                    ok, elapsed2, note = run_step(name, label, script, sa,
                                                  timeout, fh, args.quiet)
                    elapsed += elapsed2

                results.append((name, ok, elapsed, note))
                log(fh, f"  -> {'OK' if ok else 'FAILED'}  ({elapsed:.1f}s)",
                    prefix=name)

                if not ok and name in FATAL_STEPS:
                    log(fh, f"  !! {name} failed -- stopping pipeline.")
                    aborted = True
                    break

            else:
                names = [s[0] for s in payload]
                log(fh, "-" * 60)
                log(fh, f">> {' + '.join(names)}  (parallel)")
                log(fh, "-" * 60)

                group_results = run_parallel(payload, fh, args.quiet)
                for step, ok, elapsed, note in group_results:
                    if step is None:
                        continue
                    name = step[0]
                    results.append((name, ok, elapsed, note))
                    log(fh, f"  -> {'OK' if ok else 'FAILED'}  ({elapsed:.1f}s)",
                        prefix=name)

        log(fh, "")
        log(fh, "=" * 66)
        log(fh, "SUMMARY")
        log(fh, "=" * 66)
        log(fh, f"  {'step':<14}{'status':<10}{'time':>10}   note")
        for name, ok, elapsed, note in results:
            status = "OK" if ok else "FAILED"
            log(fh, f"  {name:<14}{status:<10}{elapsed:>9.1f}s   {note}")

        failed = [r for r in results if not r[1]]
        log(fh, "")
        if aborted:
            log(fh, f"Pipeline ABORTED after {len(results)} step(s).")
        elif failed:
            log(fh, f"{len(failed)} step(s) failed.")
        else:
            log(fh, "All steps OK.")
        log(fh, f"Log: {LOG_FILE}")
        log(fh, "=" * 66)

    # Non-zero exit so CI (GitHub Actions / Task Scheduler) notices failures.
    if aborted:
        sys.exit(3)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
