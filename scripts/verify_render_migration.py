#!/usr/bin/env python3
"""Read-only check that the data migration to Render really completed.

Compares the LOCAL copy against what is now on Render, one check at a time:

  * row counts of the images, mappings, design_categories and users tables
    in mappings.db
  * the number of files and the total bytes in the "S.S IMAGE" folder

Each check prints its own PASS/FAIL with the exact difference, e.g.
    FAIL  images (table rows): local 6582, Render 6579 -- Render is missing 3
The exit code is 0 only if every check passes, 1 if any fails, 2 if the
remote side could not be read at all.

It changes nothing on either side: SQLite is opened read-only and the image
folders are only listed.

Where "Render" comes from (pick one):

  A) A downloaded copy (simplest) -- download Render's data folder, e.g. with
     scp/rsync or Render's shell, then point at it:
         python scripts/verify_render_migration.py --remote-dir C:\\downloads\\render_data
     (that folder should contain mappings.db and "S.S IMAGE")

  B) Straight over SSH -- Render web services offer SSH. The script pipes a
     small read-only inspection program to the remote python3:
         python scripts/verify_render_migration.py ^
             --remote-cmd "ssh srv-XXXX@ssh.oregon.render.com python3 -" ^
             --remote-data-dir /opt/render/project/src/data
     (--remote-data-dir is where mappings.db and "S.S IMAGE" live on Render's disk;
      match your DB_PATH / IMAGE_SCAN_ROOT there.)

Local defaults are the project's data folder; override with --local-dir.
"""
import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys

TABLES = ["images", "mappings", "design_categories", "users"]
IMAGE_FOLDER_NAME = "S.S IMAGE"
DB_FILE_NAME = "mappings.db"

# Runs on the remote machine (read-only) -- also imported and used locally so
# both sides are measured by the exact same code.
INSPECT_SOURCE = r'''
import json, os, sqlite3, sys

TABLES = %(tables)r

def inspect(data_dir, db_name, image_folder):
    out = {"tables": {}, "image_files": None, "image_bytes": None, "errors": []}
    db_path = os.path.join(data_dir, db_name)
    if not os.path.isfile(db_path):
        out["errors"].append("database not found: " + db_path)
    else:
        try:
            # A database with no -wal file next to it has nothing pending, so open it
            # immutable: that guarantees SQLite creates no -shm/-wal side files. A live
            # database (its -wal exists) is opened plain read-only so committed-but-
            # not-yet-checkpointed rows are still counted.
            extra = "" if os.path.exists(db_path + "-wal") else "&immutable=1"
            uri = "file:" + db_path.replace("\\", "/") + "?mode=ro" + extra
            conn = sqlite3.connect(uri, uri=True)
            for table in TABLES:
                try:
                    out["tables"][table] = conn.execute('SELECT COUNT(*) FROM "%%s"' %% table).fetchone()[0]
                except sqlite3.Error as exc:
                    out["errors"].append("table %%s: %%s" %% (table, exc))
            conn.close()
        except sqlite3.Error as exc:
            out["errors"].append("cannot open database read-only: %%s" %% exc)
    img_dir = os.path.join(data_dir, image_folder)
    if not os.path.isdir(img_dir):
        out["errors"].append("image folder not found: " + img_dir)
    else:
        files = size = 0
        for folder, _dirs, names in os.walk(img_dir):
            for name in names:
                try:
                    size += os.path.getsize(os.path.join(folder, name))
                    files += 1
                except OSError:
                    out["errors"].append("unreadable file: " + os.path.join(folder, name))
        out["image_files"], out["image_bytes"] = files, size
    return out

if __name__ == "__main__":
    print(json.dumps(inspect(sys.argv[1], sys.argv[2], sys.argv[3])))
''' % {"tables": TABLES}

_namespace = {}
exec(compile(INSPECT_SOURCE.replace('if __name__ == "__main__":', "if False:"), "<inspect>", "exec"), _namespace)
inspect_local = _namespace["inspect"]


def inspect_remote(remote_cmd, data_dir):
    """Pipes the inspection program to `remote_cmd` (e.g. 'ssh host python3 -')."""
    cmd = shlex.split(remote_cmd, posix=(os.name != "nt")) + [data_dir, DB_FILE_NAME, IMAGE_FOLDER_NAME]
    proc = subprocess.run(cmd, input=INSPECT_SOURCE, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"remote command failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    lines = [line for line in proc.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise RuntimeError(f"remote command printed no result: {proc.stdout.strip()[:300]}")
    return json.loads(lines[-1])


def compare(label, local, remote, unit="", missing_word="missing"):
    """One check -> (passed, text). Numbers may be None if a side couldn't be read."""
    if local is None or remote is None:
        return False, f"FAIL  {label}: could not be read (local: {local}, Render: {remote})"
    if local == remote:
        return True, f"PASS  {label}: local {local:,}{unit}, Render {remote:,}{unit} -- match"
    diff = abs(local - remote)
    unit_diff = " file" if (unit == " files" and diff == 1) else unit
    if remote < local:
        note = f"Render is {missing_word} {diff:,}{unit_diff}"
    else:
        note = f"Render has {diff:,}{unit_diff} EXTRA"
    return False, f"FAIL  {label}: local {local:,}{unit}, Render {remote:,}{unit} -- {note}"


def run_checks(local, remote):
    results = []
    for table in TABLES:
        results.append(compare(
            f"{table} (table rows)", local["tables"].get(table), remote["tables"].get(table), missing_word="missing",
        ))
    results.append(compare("S.S IMAGE file count", local["image_files"], remote["image_files"], " files", "missing"))
    results.append(compare("S.S IMAGE total size", local["image_bytes"], remote["image_bytes"], " bytes", "missing"))
    return results


def main(argv=None):
    project_data = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    parser = argparse.ArgumentParser(description="Read-only local-vs-Render migration check.")
    parser.add_argument("--local-dir", default=project_data,
                        help=f"local folder with {DB_FILE_NAME} and '{IMAGE_FOLDER_NAME}' (default: {project_data})")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--remote-dir", help="a downloaded copy of Render's data folder")
    source.add_argument("--remote-cmd", help="command that runs python on Render reading a script on stdin, "
                                             "e.g. \"ssh srv-XXXX@ssh.oregon.render.com python3 -\"")
    parser.add_argument("--remote-data-dir", help="data folder on Render's disk (required with --remote-cmd)")
    args = parser.parse_args(argv)

    if args.remote_cmd and not args.remote_data_dir:
        parser.error("--remote-cmd needs --remote-data-dir")

    local = inspect_local(args.local_dir, DB_FILE_NAME, IMAGE_FOLDER_NAME)
    print(f"Local : {args.local_dir}")
    try:
        if args.remote_dir:
            print(f"Render: {args.remote_dir} (downloaded copy)")
            remote = inspect_local(args.remote_dir, DB_FILE_NAME, IMAGE_FOLDER_NAME)
        else:
            print(f"Render: {args.remote_data_dir} via `{args.remote_cmd}`")
            remote = inspect_remote(args.remote_cmd, args.remote_data_dir)
    except Exception as exc:  # could not read Render at all
        print(f"\nCould not read the Render side: {exc}")
        return 2

    for side, data in (("local", local), ("Render", remote)):
        for error in data["errors"]:
            print(f"  note ({side}): {error}")
    print()

    results = run_checks(local, remote)
    for _passed, text in results:
        print(text)
    failed = [text for passed, text in results if not passed]
    print()
    if failed:
        print(f"RESULT: {len(failed)} of {len(results)} checks FAILED -- migration is NOT verified.")
        return 1
    print(f"RESULT: all {len(results)} checks passed -- local and Render match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
