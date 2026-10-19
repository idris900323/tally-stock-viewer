#!/usr/bin/env python3
"""ONE command to check that nothing broke:

    python tests/check_all.py            run everything
    python tests/check_all.py --update   build NEW golden snapshots in a temp folder (nothing in the project is changed)
    python tests/check_all.py -k backup  run only test modules whose name contains "backup"

It always copies the code (NOT data/, logs/, .env, .git, .venv) into a fresh temporary folder and runs every
test there, so your real data folder, database, Drive credentials and .env are never read or changed. There is
no switch to turn this off. It refuses to start if any data path the app would use (data/, DB_PATH, LOG_FILE,
...) resolves into the real data/ folder, it never copies files back into the project, and when it is
interrupted it removes only its own temporary folder. Real Drive, real Tally and the real site are never
contacted (fakes only). Exit code 0 = all passed, 1 = a test failed, 2 = refused to start.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scratch_guard  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE_DIRS = {".git", "data", "logs", "__pycache__", ".venv", "venv", ".state_snapshot", "node_modules", "brag-output", "ai_tryon_test", "archived_unneeded"}
NEVER_COPY = (".env",)           # any file whose name is .env or starts with .env.

NOT_COVERED = """
What this does NOT cover (be honest about the gaps):
  * a real browser or phone (no JavaScript is executed here; templates/JS are only scanned for the URLs they call)
  * real Google Drive, real Tally, real Render SSH, the installed PWA / service worker in a real browser
  * Linux-only paths (/proc descriptor breakdown) -- the helper is unit-tested with fake data only
  * the real production data size/timing (709 cars, ~15,000 rows): memory behaviour is checked with small data
  * the visual look of any page (snapshots compare structure and text hashes, not pixels)
"""


def copy_code(dest):
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(folder, ROOT)
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for name in files:
            if name in (".git", ".env") or name.startswith(".env.") or name.endswith((".pyc", ".db", ".log", ".pid")):
                continue
            shutil.copy2(os.path.join(folder, name), os.path.join(dest, rel, name))
    os.makedirs(os.path.join(dest, "data"), exist_ok=True)


def _raise_interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="write new tests/golden/*.json files into a temp folder for you to review and copy")
    parser.add_argument("-k", dest="pattern", default="", help="only run test modules whose file name contains this text")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the scratch folder (to inspect it)")
    args = parser.parse_args()

    for name in ("SIGTERM", "SIGBREAK"):          # make kill/close behave like Ctrl+C so the cleanup below runs
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _raise_interrupt)

    scratch = tempfile.mkdtemp(prefix=scratch_guard.PREFIX)
    nonce = uuid.uuid4().hex
    child = None
    print(f"Working in a temporary copy: {scratch}\n(your data/, logs/ and .env are not copied)\n", flush=True)
    try:
        copy_code(scratch)
        with open(os.path.join(scratch, scratch_guard.MARKER), "w", encoding="utf-8") as fh:
            json.dump({"nonce": nonce, "source": ROOT}, fh)
        # Judge the environment as the user gave it (before anything is stripped): a data path that points
        # into the real data/ folder is a reason to stop, not something to quietly override.
        problems = scratch_guard.unsafe_data_paths(ROOT, scratch, os.environ)
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("MACHINE_ROLE", "BACKUP_MAX_DELETIONS", "IMAGE_SCAN_ROOT", "FEEDER_MODE")
               and not k.startswith(("GDRIVE", "CLOUD_SYNC", "CHECK_ALL_"))}
        env.update(CHECK_ALL_NONCE=nonce, PYTHONUNBUFFERED="1", PYTHONWARNINGS="ignore::ResourceWarning")
        problems += scratch_guard.scratch_problems(scratch, env)
        if problems:
            print("CHECK_ALL: REFUSED TO START -- nothing was run:\n  - " + "\n  - ".join(problems), file=sys.stderr)
            return 2
        if args.update:
            env["CHECK_ALL_UPDATE"] = "1"
        modules = sorted(f[:-3] for f in os.listdir(os.path.join(scratch, "tests")) if f.startswith("test_") and f.endswith(".py") and args.pattern in f)
        if not modules:
            print("No test modules matched."); return 2
        command = [sys.executable, "-m", "unittest"] + (["-v"] if args.verbose else []) + [f"tests.{m}" for m in modules]
        child = subprocess.Popen(command, cwd=scratch, env=env)
        code = child.wait()
        if args.update:
            golden_src = os.path.join(scratch, "tests", "golden")
            out = tempfile.mkdtemp(prefix="check_all_goldens_")
            for name in os.listdir(golden_src):
                shutil.copy2(os.path.join(golden_src, name), os.path.join(out, name))
            print(f"\nNew golden files are in {out}  (NOTHING in the project was changed).\n"
                  f"Review:  git diff --no-index tests/golden \"{out}\"   then copy the ones you intend to keep into tests/golden.")
        print("\n" + ("CHECK_ALL: PASS" if code == 0 else "CHECK_ALL: FAIL") + f"  ({len(modules)} test modules)")
        print(NOT_COVERED)
        return 0 if code == 0 else 1
    except KeyboardInterrupt:
        print("\nCHECK_ALL: interrupted -- stopping the tests and removing only the temporary copy.", file=sys.stderr)
        return 130
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if args.keep:
            print(f"(temporary copy kept at {scratch})")
        elif scratch_guard.safe_to_delete(scratch, nonce):
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            print(f"(not deleting {scratch}: it does not look like this run's own temporary folder)", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
