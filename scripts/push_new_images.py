#!/usr/bin/env python3
"""Push newly-added/changed images from the local "S.S IMAGE" folder to
Render over SCP, then trigger a remote rescan -- so new stock photos show
up on the live site without touching Training Mode at all.

Run via the project-root push_new_images.bat (double-click, no manual
PowerShell). Safe to re-run any time: a local manifest
(data/.image_push_manifest.json, git-ignored along with the rest of data/)
tracks which files have already been pushed by relative path + size + last-
modified time, so a normal run only pushes what's genuinely new or changed.

FIRST RUN IS SPECIAL: if no manifest exists yet, this seeds it from the
current state of the local folder -- every file already there is recorded
as "already pushed" without touching the network. This matters because the
local folder already holds the full catalog migrated to Render earlier; a
naive first run would otherwise try to re-push all of it. Only a *second*
run (with new/changed files added since) actually pushes anything.

Needs these in the project's .env (see .env.example / MIGRATION_DAY_INSTRUCTIONS.md):
    RENDER_SSH_ADDRESS   e.g. srv-xxxxx@ssh.singapore.render.com (Connect tab)
    RENDER_DATA_DIR      Render disk data path (default: /opt/render/project/src/data)
    RESCAN_TRIGGER_URL   e.g. https://tally-stock-viewer.onrender.com/admin/system/trigger_rescan
    RESCAN_TRIGGER_TOKEN same value as RESCAN_TRIGGER_TOKEN set on Render
Local image folder defaults to IMAGE_SCAN_ROOT (same variable the app itself
uses), or data/S.S IMAGE under the project root if that's unset.
"""
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
IMAGE_FOLDER_NAME = "S.S IMAGE"

if load_dotenv is not None:
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def resolve_local_root():
    configured = _env("IMAGE_SCAN_ROOT")
    if configured:
        return configured if os.path.isabs(configured) else os.path.abspath(os.path.join(PROJECT_ROOT, configured))
    return os.path.join(PROJECT_ROOT, "data", IMAGE_FOLDER_NAME)


LOCAL_ROOT = resolve_local_root()
MANIFEST_PATH = os.path.join(PROJECT_ROOT, "data", ".image_push_manifest.json")
RENDER_SSH_ADDRESS = _env("RENDER_SSH_ADDRESS")
RENDER_DATA_DIR = _env("RENDER_DATA_DIR", "/opt/render/project/src/data")
REMOTE_ROOT = f"{RENDER_DATA_DIR.rstrip('/')}/{IMAGE_FOLDER_NAME}"
RESCAN_TRIGGER_URL = _env("RESCAN_TRIGGER_URL")
RESCAN_TRIGGER_TOKEN = _env("RESCAN_TRIGGER_TOKEN")
MANIFEST_SAVE_EVERY = 20


def scan_local_files(root):
    """relative_path (posix, forward slashes) -> {"size": int, "mtime": float}."""
    files = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            full_path = os.path.join(folder, name)
            rel_path = os.path.relpath(full_path, root).replace("\\", "/")
            try:
                stat = os.stat(full_path)
            except OSError as exc:
                print(f"  [WARN] Could not stat {rel_path}: {exc}")
                continue
            files[rel_path] = {"size": stat.st_size, "mtime": stat.st_mtime}
    return files


def load_manifest():
    if not os.path.isfile(MANIFEST_PATH):
        return None
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_manifest(manifest):
    os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
    tmp_path = MANIFEST_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    os.replace(tmp_path, MANIFEST_PATH)


def is_unchanged(entry, info):
    return entry is not None and entry.get("size") == info["size"] and entry.get("mtime") == info["mtime"]


def ssh_mkdir_p(remote_dir):
    remote_cmd = "mkdir -p " + shlex.quote(remote_dir)
    proc = subprocess.run(
        ["ssh", RENDER_SSH_ADDRESS, remote_cmd],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")


def scp_push(local_path, remote_path):
    proc = subprocess.run(
        ["scp", local_path, f"{RENDER_SSH_ADDRESS}:{remote_path}"],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")


def trigger_remote_rescan():
    print("\nTriggering remote rescan on Render...")
    try:
        resp = requests.post(
            RESCAN_TRIGGER_URL,
            headers={"X-Rescan-Token": RESCAN_TRIGGER_TOKEN},
            timeout=120,
        )
    except requests.RequestException as exc:
        print(f"  [FAILED] Could not reach {RESCAN_TRIGGER_URL}: {exc}")
        return False

    if resp.status_code != 200:
        print(f"  [FAILED] Rescan trigger rejected (HTTP {resp.status_code}): {resp.text[:300]}")
        return False

    data = resp.json()
    stats = data.get("stats") or {}
    print("  [OK] Remote rescan complete:")
    print(f"       New images found and added this pass: {data.get('scanned', 0)}")
    print(f"       Total images now catalogued on Render: {stats.get('total_images', 'unknown')}")
    print(f"       Missing (on-record but file not found): {data.get('missing_count', 0)}")
    return True


def main():
    print(f"Local image folder : {LOCAL_ROOT}")
    print(f"Render target       : {RENDER_SSH_ADDRESS}:{REMOTE_ROOT}" if RENDER_SSH_ADDRESS else "Render target       : (RENDER_SSH_ADDRESS not set)")
    print(f"Manifest             : {MANIFEST_PATH}")
    print()

    if not os.path.isdir(LOCAL_ROOT):
        print(f"[ERROR] Local image folder does not exist: {LOCAL_ROOT}")
        return 1

    current_files = scan_local_files(LOCAL_ROOT)
    print(f"Found {len(current_files)} local image file(s).")

    existing_manifest = load_manifest()
    if existing_manifest is None:
        print(
            f"\nNo manifest found at {MANIFEST_PATH} -- this is the FIRST RUN.\n"
            f"Seeding the manifest from the {len(current_files)} file(s) already in the folder,\n"
            "marking all of them as already pushed. Nothing will be pushed or triggered this run."
        )
        save_manifest(current_files)
        print(f"\n[OK] Manifest seeded with {len(current_files)} file(s). Re-run this after adding new images.")
        return 0

    if not RENDER_SSH_ADDRESS:
        print("[ERROR] RENDER_SSH_ADDRESS is not set in .env -- cannot push. Aborting (manifest left untouched).")
        return 1

    manifest = dict(existing_manifest)
    to_push = sorted(
        rel_path for rel_path, info in current_files.items()
        if not is_unchanged(manifest.get(rel_path), info)
    )
    skipped_count = len(current_files) - len(to_push)

    pushed_count = 0
    failed_count = 0
    created_dirs = set()

    if not to_push:
        print(f"\nNothing new or changed. {skipped_count} file(s) already pushed, 0 to push.")
    else:
        print(f"\n{len(to_push)} new/changed file(s) to push, {skipped_count} already up to date.\n")
        for index, rel_path in enumerate(to_push, start=1):
            remote_dir = "/".join([REMOTE_ROOT] + rel_path.split("/")[:-1]) if "/" in rel_path else REMOTE_ROOT
            local_path = os.path.join(LOCAL_ROOT, *rel_path.split("/"))
            remote_path = f"{REMOTE_ROOT}/{rel_path}"
            print(f"[{index}/{len(to_push)}] {rel_path} ...", end=" ", flush=True)
            try:
                if remote_dir not in created_dirs:
                    ssh_mkdir_p(remote_dir)
                    created_dirs.add(remote_dir)
                scp_push(local_path, remote_path)
            except RuntimeError as exc:
                print(f"FAILED ({exc})")
                failed_count += 1
                continue

            manifest[rel_path] = current_files[rel_path]
            pushed_count += 1
            print("OK")

            if pushed_count % MANIFEST_SAVE_EVERY == 0:
                save_manifest(manifest)

    # Prune manifest entries for files no longer present locally (e.g. disposable
    # test images removed after a run) -- Render's copy is never touched here,
    # this only keeps local bookkeeping honest so a re-added file is treated as new.
    removed = [rel_path for rel_path in manifest if rel_path not in current_files]
    for rel_path in removed:
        del manifest[rel_path]
    save_manifest(manifest)

    print(f"\n{'=' * 60}")
    print(f"Summary: pushed {pushed_count}, skipped {skipped_count}, failed {failed_count}"
          + (f", removed from manifest {len(removed)}" if removed else ""))
    print(f"{'=' * 60}")

    if pushed_count > 0:
        if not (RESCAN_TRIGGER_URL and RESCAN_TRIGGER_TOKEN):
            print("\n[WARN] Files were pushed, but RESCAN_TRIGGER_URL/RESCAN_TRIGGER_TOKEN "
                  "are not both set in .env -- skipping the remote rescan trigger.")
        else:
            trigger_remote_rescan()
    else:
        print("\nNo files were pushed -- skipping the remote rescan trigger.")

    return 1 if failed_count else 0


if __name__ == "__main__":
    started = datetime.now()
    exit_code = main()
    print(f"\nDone in {(datetime.now() - started).total_seconds():.1f}s.")
    sys.exit(exit_code)
