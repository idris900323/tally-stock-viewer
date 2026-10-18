#!/usr/bin/env python3
"""Push new design images from this PC to the live site (Render) over SSH.

SELF-CONTAINED: standard library only; imports nothing from the web app
(app.py, database.py, config.py ...); never reads or prints any .env file;
works on a PC whose MACHINE_ROLE is "feeder". All of its state lives OUTSIDE
the git folder, in  %LOCALAPPDATA%\\SuperSeatings\\push_images\\  (config.json,
manifest.json, logs\\), so `git pull` / `git checkout` / `git clean` can never
touch or wipe it.

Safety rules (enforced in code, covered by `selftest`):
  * The first run is CHECK-ONLY: it compares this PC's folder with the
    server's folder, uploads nothing, and records what is already there.
  * Nothing is ever deleted on the server. There is no delete code at all.
  * A file that already exists on the server is never overwritten unless its
    SIZE differs AND you pass --overwrite-changed.
  * More than 200 uploads in one run is refused unless you pass
    --allow-more-than-200.
  * If anything needed is missing (ssh, the config, the image folder, the
    connection) it stops before changing anything.

Commands:  (no command) | setup | check | push | status | selftest
"""
import argparse
import ast
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

APP_NAME = "SuperSeatings"
TOOL_DIR_NAME = "push_images"
MAX_UPLOADS_DEFAULT = 200
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif"}
TEMP_SUFFIX = ".partupload"
DEFAULT_REMOTE_ROOT = "/opt/render/project/src/data/S.S IMAGE"
DEFAULT_RESCAN_URL = "https://superseatings.carxone.com/admin/system/trigger_rescan"


class ToolError(Exception):
    """A fail-closed stop: message is shown to the user, exit code 2."""


# ---------------------------------------------------------------------------
# State folder (outside the repo)
# ---------------------------------------------------------------------------
def state_dir():
    override = os.environ.get("PUSH_IMAGES_HOME")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_NAME, TOOL_DIR_NAME)


def _path(name):
    return os.path.join(state_dir(), name)


def log(message):
    """Append to the state-folder log. Never logs the rescan token."""
    try:
        os.makedirs(_path("logs"), exist_ok=True)
        with open(_path(os.path.join("logs", "push_images.log")), "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def say(message=""):
    print(message, flush=True)
    log(message.replace("\n", " | "))


def load_json(name, default=None):
    try:
        with open(_path(name), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ToolError(f"{name} in {state_dir()} is damaged ({exc}). Fix or remove just that file and run again.")


def save_json(name, data):
    os.makedirs(state_dir(), exist_ok=True)
    tmp = _path(name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, _path(name))


# ---------------------------------------------------------------------------
# Local scan
# ---------------------------------------------------------------------------
def scan_local(root):
    """relative posix path -> {"size", "mtime"} for image files only."""
    files = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            if os.path.splitext(name)[1].lower() not in IMAGE_EXTENSIONS:
                continue
            full = os.path.join(folder, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            files[os.path.relpath(full, root).replace("\\", "/")] = {"size": st.st_size, "mtime": st.st_mtime}
    return files


# ---------------------------------------------------------------------------
# Transports: the real one (ssh) and a fake one (a local folder) for tests
# ---------------------------------------------------------------------------
class SshTransport:
    def __init__(self, config):
        self.target = config["ssh_target"]
        self.remote_root = config["remote_root"].rstrip("/")
        self.key = config.get("ssh_key") or ""

    def _ssh_base(self):
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new"]
        if self.key:
            cmd += ["-i", self.key]
        return cmd + [self.target]

    def _run(self, remote_cmd, stdin=None, timeout=300):
        proc = subprocess.run(self._ssh_base() + [remote_cmd], stdin=stdin, capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def check_connection(self):
        code, out, err = self._run("echo connected", timeout=40)
        if code != 0 or "connected" not in out:
            raise ToolError("Could not log in to the server over SSH.\n  " + (err.strip() or out.strip() or f"exit code {code}")
                            + "\n  See SETUP_GUIDE.md, steps 3-5 (the SSH key).")

    def list_remote(self):
        code, out, err = self._run(f"find {shlex.quote(self.remote_root)} -type f -printf '%P\\t%s\\n'", timeout=180)
        if code != 0:
            raise ToolError("Could not list the server's image folder.\n  " + (err.strip() or f"exit code {code}"))
        files = {}
        for line in out.splitlines():
            rel, _tab, size = line.rpartition("\t")
            if rel and size.isdigit() and os.path.splitext(rel)[1].lower() in IMAGE_EXTENSIONS:
                files[rel] = int(size)
        return files

    def ensure_dir(self, rel_dir):
        target = f"{self.remote_root}/{rel_dir}" if rel_dir else self.remote_root
        code, _out, err = self._run("mkdir -p " + shlex.quote(target), timeout=60)
        if code != 0:
            raise ToolError(f"could not create folder on server: {err.strip()}")

    def upload(self, local_path, rel):
        tmp = f"{self.remote_root}/{rel}{TEMP_SUFFIX}"
        with open(local_path, "rb") as handle:
            code, _out, err = self._run("cat > " + shlex.quote(tmp), stdin=handle, timeout=600)
        if code != 0:
            raise ToolError(f"upload failed: {err.strip() or code}")

    def finalize(self, rel, size, overwrite):
        tmp = shlex.quote(f"{self.remote_root}/{rel}{TEMP_SUFFIX}")
        final = shlex.quote(f"{self.remote_root}/{rel}")
        check = f'[ "$(stat -c %s {tmp})" = {int(size)} ]'
        move = f"mv -f {tmp} {final}" if overwrite else f"[ ! -e {final} ] && mv {tmp} {final}"
        code, _out, err = self._run(f"{check} && {move}", timeout=60)
        if code != 0:
            raise ToolError("server rejected the upload (size mismatch, or the file already exists)")


class FakeTransport:
    """Same behaviour as SshTransport but on a local folder -- for `selftest`,
    the tests, and a safe rehearsal (`--fake-remote DIR`)."""
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def check_connection(self):
        pass

    def list_remote(self):
        out = {}
        for folder, _d, names in os.walk(self.root):
            for n in names:
                if os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS:
                    out[os.path.relpath(os.path.join(folder, n), self.root).replace("\\", "/")] = os.path.getsize(os.path.join(folder, n))
        return out

    def ensure_dir(self, rel_dir):
        os.makedirs(os.path.join(self.root, *rel_dir.split("/")) if rel_dir else self.root, exist_ok=True)

    def upload(self, local_path, rel):
        shutil.copyfile(local_path, os.path.join(self.root, *rel.split("/")) + TEMP_SUFFIX)

    def finalize(self, rel, size, overwrite):
        tmp = os.path.join(self.root, *rel.split("/")) + TEMP_SUFFIX
        final = os.path.join(self.root, *rel.split("/"))
        if os.path.getsize(tmp) != size:
            raise ToolError("server rejected the upload (size mismatch)")
        if os.path.exists(final) and not overwrite:
            raise ToolError("server rejected the upload (the file already exists)")
        os.replace(tmp, final)


def make_transport(config, fake_remote=None):
    if fake_remote:
        return FakeTransport(fake_remote)
    return SshTransport(config)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(require=True):
    config = load_json("config.json")
    if config is None:
        if require:
            raise ToolError("Not set up yet. Run:  push_images.bat setup   (see SETUP_GUIDE.md)")
        return None
    for key in ("ssh_target", "remote_root", "local_root"):
        if not config.get(key):
            raise ToolError(f"config.json is missing '{key}'. Run setup again.")
    return config


def require_prerequisites(config, fake_remote):
    if not os.path.isdir(config["local_root"]):
        raise ToolError(f"The local image folder does not exist: {config['local_root']}")
    if not scan_local(config["local_root"]):
        raise ToolError(f"No image files found in {config['local_root']}. Refusing to continue (wrong folder?).")
    if not fake_remote:
        if shutil.which("ssh") is None:
            raise ToolError("'ssh' was not found. On Windows 10/11 turn on Settings > Apps > Optional features > OpenSSH Client.")
        key = config.get("ssh_key")
        if key and not os.path.isfile(key):
            raise ToolError(f"The SSH key file is missing: {key}")


def cmd_setup(args):
    existing = load_config(require=False) or {}
    def ask(label, key, default=""):
        current = existing.get(key) or default
        value = args_get(args, key)
        if value is None:
            value = input(f"{label} [{current}]: ").strip() if sys.stdin and sys.stdin.isatty() else ""
        return value or current
    config = {
        "ssh_target": ask("Server SSH address (from Render > Connect > SSH, like srv-xxxx@ssh.singapore.render.com)", "ssh_target"),
        "remote_root": ask("Server image folder", "remote_root", DEFAULT_REMOTE_ROOT),
        "local_root": ask("Image folder on THIS PC", "local_root"),
        "ssh_key": ask("SSH private key file (blank = use the default key)", "ssh_key"),
        "rescan_url": ask("Rescan URL (blank = don't trigger a rescan)", "rescan_url", DEFAULT_RESCAN_URL),
        "rescan_token": existing.get("rescan_token", ""),
    }
    token = args_get(args, "rescan_token")
    if token is None and sys.stdin and sys.stdin.isatty():
        token = input("Rescan token (blank = keep current / none; it is never shown again): ").strip()
    if token:
        config["rescan_token"] = token
    missing = [k for k in ("ssh_target", "local_root") if not config.get(k)]
    if missing:
        raise ToolError("Setup incomplete, missing: " + ", ".join(missing) + ". Nothing was saved.")
    config["max_uploads"] = MAX_UPLOADS_DEFAULT
    save_json("config.json", config)
    say(f"Saved settings to {_path('config.json')}")
    say("Next: run  push_images.bat  - the first run only COMPARES, it uploads nothing.")
    return 0


def args_get(args, key):
    return getattr(args, key, None)


# ---------------------------------------------------------------------------
# Compare / check / push
# ---------------------------------------------------------------------------
def compare(local, remote):
    """-> dict(same, new, differ, remote_only) of relative paths."""
    same, new, differ = [], [], []
    for rel, info in local.items():
        if rel not in remote:
            new.append(rel)
        elif remote[rel] == info["size"]:
            same.append(rel)
        else:
            differ.append(rel)
    return {"same": sorted(same), "new": sorted(new), "differ": sorted(differ),
            "remote_only": sorted(set(remote) - set(local))}


def describe(result):
    say(f"  Already on the server and identical : {len(result['same'])}")
    say(f"  New on this PC (would be uploaded)    : {len(result['new'])}")
    say(f"  Same name but DIFFERENT size          : {len(result['differ'])}  (never overwritten unless you ask)")
    say(f"  Only on the server (never touched)    : {len(result['remote_only'])}")
    for label, key in (("new", "new"), ("different size", "differ")):
        for rel in result[key][:10]:
            say(f"      {label}: {rel}")
        if len(result[key]) > 10:
            say(f"      ... and {len(result[key]) - 10} more {label}")


def write_manifest(local, result, source):
    manifest = {"version": 1, "updated": datetime.now().isoformat(timespec="seconds"), "source": source,
                "files": {rel: local[rel] for rel in result["same"]}}
    save_json("manifest.json", manifest)


def cmd_check(args, config=None):
    config = config or load_config()
    fake = args.fake_remote
    require_prerequisites(config, fake)
    transport = make_transport(config, fake)
    say("Checking the connection ...")
    transport.check_connection()
    local = scan_local(config["local_root"])
    say(f"This PC has {len(local)} image file(s). Asking the server what it has ...")
    result = compare(local, transport.list_remote())
    describe(result)
    first = load_json("manifest.json") is None
    write_manifest(local, result, "check")
    say("\nNothing was uploaded." + (" (This was the first run: it only compared.)" if first else ""))
    if result["new"] or result["differ"]:
        say("To upload the new files run:  push_images.bat push")
    return 0


def cmd_push(args, config=None):
    config = config or load_config()
    fake = args.fake_remote
    if load_json("manifest.json") is None:
        say("This is the first run, so it only compares (uploads nothing).")
        return cmd_check(args, config)
    require_prerequisites(config, fake)
    transport = make_transport(config, fake)
    say("Checking the connection ...")
    transport.check_connection()
    local = scan_local(config["local_root"])
    result = compare(local, transport.list_remote())
    describe(result)
    to_upload = list(result["new"])
    if result["differ"]:
        if args.overwrite_changed:
            to_upload += result["differ"]
            say(f"\n--overwrite-changed given: {len(result['differ'])} changed-size file(s) WILL be replaced.")
        else:
            say(f"\n{len(result['differ'])} file(s) have a different size on the server. They are left alone (add --overwrite-changed to replace them).")
    limit = config.get("max_uploads", MAX_UPLOADS_DEFAULT)
    if len(to_upload) > limit and not args.allow_more_than_200:
        raise ToolError(f"{len(to_upload)} uploads is more than {limit}. That is unusual - check the folder is the right one. "
                        f"If it really is intended, run again with --allow-more-than-200. Nothing was uploaded.")
    if not to_upload:
        say("\nNothing to upload.")
        write_manifest(local, result, "push")
        return 0
    if not args.yes:
        if not (sys.stdin and sys.stdin.isatty()):
            raise ToolError("Not an interactive window, and --yes was not given. Nothing was uploaded.")
        if input(f"\nUpload {len(to_upload)} file(s)? Type yes to continue: ").strip().lower() != "yes":
            say("Cancelled. Nothing was uploaded.")
            return 0
    uploaded, failed = [], []
    made_dirs = set()
    overwrite_set = set(result["differ"]) if args.overwrite_changed else set()
    for index, rel in enumerate(to_upload, 1):
        say(f"[{index}/{len(to_upload)}] {rel} ...")
        try:
            rel_dir = rel.rpartition("/")[0]
            if rel_dir not in made_dirs:
                transport.ensure_dir(rel_dir)
                made_dirs.add(rel_dir)
            transport.upload(os.path.join(config["local_root"], *rel.split("/")), rel)
            transport.finalize(rel, local[rel]["size"], overwrite=rel in overwrite_set)
            uploaded.append(rel)
        except (ToolError, OSError, subprocess.SubprocessError) as exc:
            say(f"    FAILED: {exc}")
            failed.append(rel)
    say(f"\nUploaded {len(uploaded)}, failed {len(failed)}.")
    fresh = compare(local, transport.list_remote())
    write_manifest(local, fresh, "push")
    if uploaded:
        trigger_rescan(config, fake)
    if failed:
        say("Some files failed (partial copies named *" + TEMP_SUFFIX + " may remain on the server; they are ignored and nothing was deleted).")
        return 1
    return 0


def trigger_rescan(config, fake):
    if fake:
        say("(fake remote: rescan skipped)")
        return
    url, token = config.get("rescan_url"), config.get("rescan_token")
    if not (url and token):
        say("Uploaded files will appear after the server's next scan, or press 'Scan images' in the site's Training Mode.")
        return
    say("Asking the site to rescan its images ...")
    request = urllib.request.Request(url, method="POST", headers={"X-Rescan-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            say(f"  Site answered HTTP {response.status}.")
    except (urllib.error.URLError, OSError) as exc:
        say(f"  Could not trigger the rescan ({type(exc).__name__}); the files are uploaded and will appear after the next scan.")


def cmd_status(args):
    config = load_config(require=False)
    say(f"State folder : {state_dir()}")
    if not config:
        say("Settings     : not set up yet")
        return 0
    say(f"Server       : {config['ssh_target']}  ->  {config['remote_root']}")
    say(f"This PC      : {config['local_root']}")
    say(f"Rescan       : {'configured' if config.get('rescan_url') and config.get('rescan_token') else 'not configured'}  (token never shown)")
    manifest = load_json("manifest.json")
    say("Manifest     : " + (f"{len(manifest['files'])} file(s) recorded, updated {manifest['updated']}" if manifest else "none yet (first run will only compare)"))
    return 0


def cmd_guided(args):
    config = load_config(require=False)
    if config is None:
        say("First time here - let's set things up.\n")
        cmd_setup(args)
        config = load_config()
    return cmd_push(args, config)


# ---------------------------------------------------------------------------
# Self-test (no network, no real files touched)
# ---------------------------------------------------------------------------
FORBIDDEN_IMPORTS = {"app", "database", "config", "cloud_backup", "machine_role", "feeder", "image_scanner", "matcher", "serve", "launcher"}


def cmd_selftest(args):
    results = []
    import contextlib, io

    def quiet(fn, *a, **k):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a, **k)

    def check(name, condition):
        results.append(bool(condition))
        print(("PASS  " if condition else "FAIL  ") + name, flush=True)

    here = os.path.abspath(__file__)
    tree = ast.parse(open(here, encoding="utf-8").read())
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | \
               {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    check("imports nothing from the web app", not (imported & FORBIDDEN_IMPORTS))
    # No delete code outside this self-test: look at every call/string in all other functions.
    delete_calls, rm_strings = [], []
    for func in [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name != "cmd_selftest"] +                 [m for c in tree.body if isinstance(c, ast.ClassDef) for m in c.body if isinstance(m, ast.FunctionDef)]:
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"remove", "unlink", "rmtree", "rmdir", "removedirs"}:
                delete_calls.append(f"{func.name}:{node.func.attr}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and (" rm " in f" {node.value} " or "--remove" in node.value):
                rm_strings.append(f"{func.name}:{node.value[:30]}")
    check("contains no delete code (no os.remove/unlink/rmtree, no remote rm)", not delete_calls and not rm_strings)

    old_home = os.environ.get("PUSH_IMAGES_HOME")
    work = tempfile.mkdtemp(prefix="push_images_selftest_")
    os.environ["PUSH_IMAGES_HOME"] = os.path.join(work, "state")
    try:
        local = os.path.join(work, "local"); remote = os.path.join(work, "remote")
        def put(root, rel, data):
            path = os.path.join(root, *rel.split("/")); os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle: handle.write(data)
        for i in range(5): put(local, f"CAR A/img{i}.jpg", b"x" * (100 + i))
        put(remote, "CAR A/img0.jpg", b"x" * 100); put(remote, "OLD/only_on_server.jpg", b"keep me")
        config = {"ssh_target": "fake", "remote_root": remote, "local_root": local, "max_uploads": MAX_UPLOADS_DEFAULT}
        save_json("config.json", config)
        ns = lambda **k: argparse.Namespace(**{"fake_remote": remote, "yes": True, "overwrite_changed": False, "allow_more_than_200": False, **k})

        before = sorted(FakeTransport(remote).list_remote().items())
        quiet(cmd_check, ns())
        check("first run is check-only: nothing uploaded", sorted(FakeTransport(remote).list_remote().items()) == before)
        check("first run wrote a manifest", load_json("manifest.json") is not None)
        os.remove(_path("manifest.json"))
        quiet(cmd_push, ns())
        check("push before any manifest exists only compares", sorted(FakeTransport(remote).list_remote().items()) == before)
        quiet(cmd_push, ns())
        got = FakeTransport(remote).list_remote()
        check("second run uploads exactly the 4 new files", len(got) == 6 and all(f"CAR A/img{i}.jpg" in got for i in range(5)))
        check("a server-only file is never touched", "OLD/only_on_server.jpg" in got)
        check("no leftover temp files", not [r for r, _d, fs in os.walk(remote) for f in fs if f.endswith(TEMP_SUFFIX)])

        put(local, "CAR A/img1.jpg", b"y" * 999)   # same name, different size
        quiet(cmd_push, ns())
        check("a same-name file with a different size is NOT overwritten by default", got["CAR A/img1.jpg"] == FakeTransport(remote).list_remote()["CAR A/img1.jpg"] == 101)
        quiet(cmd_push, ns(overwrite_changed=True))
        check("it IS replaced with --overwrite-changed", FakeTransport(remote).list_remote()["CAR A/img1.jpg"] == 999)

        for i in range(210): put(local, f"BIG/f{i}.jpg", b"z" * 10)
        try:
            quiet(cmd_push, ns()); refused = False
        except ToolError:
            refused = True
        check("210 uploads are refused without --allow-more-than-200", refused and not any(k.startswith("BIG/") for k in FakeTransport(remote).list_remote()))
        quiet(cmd_push, ns(allow_more_than_200=True))
        check("...and allowed with --allow-more-than-200", sum(1 for k in FakeTransport(remote).list_remote() if k.startswith("BIG/")) == 210)

        shutil.rmtree(os.path.join(local, "BIG"))
        before = FakeTransport(remote).list_remote(); quiet(cmd_push, ns())
        check("deleting files locally never deletes anything on the server", FakeTransport(remote).list_remote() == before)

        save_json("config.json", dict(config, local_root=os.path.join(work, "nope")))
        try:
            quiet(cmd_push, ns()); stopped = False
        except ToolError:
            stopped = True
        check("fails closed when the image folder is missing", stopped)
    finally:
        if old_home is None: os.environ.pop("PUSH_IMAGES_HOME", None)
        else: os.environ["PUSH_IMAGES_HOME"] = old_home
        shutil.rmtree(work, ignore_errors=True)
    print(f"\nSelf-test: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(prog="push_images", description="Push new design images to the live site.")
    parser.add_argument("command", nargs="?", default="guided", choices=["guided", "setup", "check", "push", "status", "selftest"])
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation before uploading")
    parser.add_argument("--overwrite-changed", action="store_true", help="replace server files whose SIZE differs (never done by default)")
    parser.add_argument("--allow-more-than-200", action="store_true", help="allow a run with more than 200 uploads")
    parser.add_argument("--fake-remote", metavar="DIR", help="TEST ONLY: use a local folder instead of the server")
    for key in ("ssh_target", "remote_root", "local_root", "ssh_key", "rescan_url", "rescan_token"):
        parser.add_argument("--" + key.replace("_", "-"), dest=key, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    started = time.time()
    try:
        handler = {"guided": cmd_guided, "setup": cmd_setup, "check": cmd_check, "push": cmd_push,
                   "status": cmd_status, "selftest": cmd_selftest}[args.command]
        code = handler(args)
    except ToolError as exc:
        say(f"\nSTOPPED (nothing was changed on the server): {exc}")
        code = 2
    except KeyboardInterrupt:
        say("\nCancelled.")
        code = 130
    if args.command != "selftest":
        say(f"\nDone in {time.time() - started:.1f}s (exit code {code}). Log: {_path(os.path.join('logs', 'push_images.log'))}")
    return code


if __name__ == "__main__":
    sys.exit(main())
