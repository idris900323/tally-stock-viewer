"""Safety rails shared by tests/check_all.py and the test modules.

The tests write files (a test database, fixture images, caches). They must therefore only ever run inside
the throw-away copy that tests/check_all.py builds in the system temp folder -- never in the real project
folder and never against the real data/ folder. There is deliberately NO environment variable that turns
this off. A run counts as "inside the scratch copy" only when ALL of these hold:

  * the copy's root holds a marker file written by check_all.py, and the nonce in it equals the
    CHECK_ALL_NONCE that check_all.py passed to this very run;
  * the root is inside the system temp folder, is named check_all_*, and has no .git folder or .env file;
  * the root is not the real project it was copied from, and no data path that the app would resolve
    (data/, DB_PATH, LOG_DIR, LOG_FILE, the Drive token paths, IMAGE_SCAN_ROOT) is inside that real
    project's data/ folder.
"""
import json
import os
import tempfile

MARKER = ".check_all_scratch.json"
PREFIX = "check_all_"
# settings that can point the app at a file or folder; relative values are resolved against the scratch root
PATH_SETTINGS = ("DB_PATH", "LOG_DIR", "LOG_FILE", "GDRIVE_OAUTH_CLIENT_SECRETS_PATH", "GDRIVE_OAUTH_TOKEN_PATH", "IMAGE_SCAN_ROOT", "DATA_DIR")


def _real(path):
    return os.path.normcase(os.path.realpath(path))


def _inside(path, folder):
    path, folder = _real(path), _real(folder)
    return path == folder or path.startswith(folder.rstrip("/\\") + os.sep)


def unsafe_data_paths(real_root, scratch_root, env):
    """Return a list of human-readable problems; an empty list means every data path stays inside the scratch copy."""
    real_data = os.path.join(real_root, "data")
    problems = []
    candidates = {"scratch data folder": os.path.join(scratch_root, "data"), "scratch root": scratch_root}
    for name in PATH_SETTINGS:
        value = (env.get(name) or "").strip()
        if value:
            candidates[name] = value if os.path.isabs(value) else os.path.join(scratch_root, value)
    for label, path in candidates.items():
        if _inside(path, real_data):
            problems.append(f"{label} resolves to {path}, which is inside the real data folder {real_data}")
    if _real(scratch_root) == _real(real_root) or _inside(real_root, scratch_root):
        problems.append("the scratch copy is (or contains) the real project folder")
    return problems


def scratch_problems(root, env=None):
    """Why `root` is NOT a valid check_all scratch copy (empty list = valid)."""
    env = os.environ if env is None else env
    problems = []
    try:
        marker = json.load(open(os.path.join(root, MARKER), encoding="utf-8"))
    except (OSError, ValueError):
        return [f"{root} is not a check_all scratch copy (no {MARKER})"]
    if not env.get("CHECK_ALL_NONCE") or marker.get("nonce") != env.get("CHECK_ALL_NONCE"):
        problems.append("the scratch marker does not match this run")
    if not _inside(root, tempfile.gettempdir()):
        problems.append("the scratch copy is not inside the system temp folder")
    if not os.path.basename(os.path.normpath(root)).startswith(PREFIX):
        problems.append(f"the scratch folder name does not start with {PREFIX}")
    if os.path.exists(os.path.join(root, ".git")) or os.path.exists(os.path.join(root, ".env")):
        problems.append("the scratch copy contains .git or .env")
    real_root = marker.get("source") or ""
    if not real_root:
        problems.append("the scratch marker does not name the source project")
    else:
        problems += unsafe_data_paths(real_root, root, env)
    return problems


def require_scratch(root):
    """Call at the top of every test module, before importing the app. Exits instead of running anywhere else."""
    problems = scratch_problems(root)
    if problems:
        raise SystemExit("Refusing to run the tests here:\n  - " + "\n  - ".join(problems) +
                         "\nRun them with:  python tests/check_all.py   (it builds a temporary copy; data/ and .env are never touched).")


def safe_to_delete(path, nonce):
    """True only for the scratch folder this run created: right place, right name, right marker."""
    try:
        marker = json.load(open(os.path.join(path, MARKER), encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (marker.get("nonce") == nonce and os.path.basename(os.path.normpath(path)).startswith(PREFIX)
            and _inside(path, tempfile.gettempdir()) and _real(path) != _real(tempfile.gettempdir()))
