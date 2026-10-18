"""The image-push tool (tools/push_images): its own safety rules, independence from the web app, and the
proof that its state lives OUTSIDE the git folder (so git checkout / git clean can never wipe it).
Uses a fake remote (a local folder); never touches the network or the real Render."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import support
from tests.support import ROOT

TOOL_DIR = os.path.join(ROOT, "tools", "push_images")
TOOL = os.path.join(TOOL_DIR, "push_images.py")


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("MACHINE_ROLE", "LOCALAPPDATA", "PUSH_IMAGES_HOME") and k not in support.TEST_ENV}
    env.update(PYTHONUNBUFFERED="1", **extra)
    return env


def run_tool(*args, cwd=None, env=None, stdin=subprocess.DEVNULL):
    return subprocess.run([sys.executable, TOOL, *args], cwd=cwd or TOOL_DIR, env=env or clean_env(), capture_output=True, text=True, timeout=120, stdin=stdin)


def put(root, rel, data):
    path = os.path.join(root, *rel.split("/")); os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle: handle.write(data)


def sha(path):
    with open(path, "rb") as handle: return hashlib.sha256(handle.read()).hexdigest()


class PushToolSafety(unittest.TestCase):
    def test_builtin_selftest_passes_with_no_network(self):
        result = run_tool("selftest")
        match = re.search(r"Self-test: (\d+)/(\d+) passed", result.stdout)
        self.assertTrue(match and match.group(1) == match.group(2), result.stdout[-600:] + result.stderr[-300:])
        self.assertEqual(result.returncode, 0)

    def test_works_when_the_machine_role_is_feeder(self):
        result = run_tool("selftest", env=clean_env(MACHINE_ROLE="feeder"))
        self.assertEqual(result.returncode, 0, result.stdout[-400:])

    def test_imports_nothing_from_the_web_app_and_never_opens_an_env_file(self):
        code = (
            "import os, sys, runpy\n"
            "opened = []\n"
            "def hook(ev, args):\n"
            "    if ev == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):\n"
            "        name = os.path.basename(os.fsdecode(args[0]))\n"
            "        if name == '.env' or name.startswith('.env.'): opened.append(name)\n"
            "sys.addaudithook(hook)\n"
            "sys.argv = ['push_images.py', 'selftest']\n"
            "try: runpy.run_path(%r, run_name='__main__')\n"
            "except SystemExit: pass\n"
            "bad = sorted(m for m in sys.modules if m in ('app','database','config','cloud_backup','machine_role','feeder','image_scanner','matcher','serve'))\n"
            "print('ENV_OPENS', len(opened), 'WEBAPP_MODULES', bad)\n") % TOOL
        decoy = tempfile.mkdtemp(prefix="decoy_")
        try:
            with open(os.path.join(decoy, ".env"), "w") as handle: handle.write("SECRET=1\n")
            result = subprocess.run([sys.executable, "-c", code], cwd=decoy, env=clean_env(), capture_output=True, text=True, timeout=120)
            self.assertIn("ENV_OPENS 0 WEBAPP_MODULES []", result.stdout, result.stdout[-300:] + result.stderr[-300:])
        finally:
            shutil.rmtree(decoy, ignore_errors=True)

    def test_fresh_run_is_check_only_and_nothing_is_ever_deleted(self):
        work = tempfile.mkdtemp(prefix="push_"); home = os.path.join(work, "home")
        try:
            local, remote = os.path.join(work, "local"), os.path.join(work, "remote")
            for i in range(3): put(local, f"CAR/img{i}.jpg", b"x" * (50 + i))
            put(remote, "CAR/img0.jpg", b"x" * 50); put(remote, "OLD/server_only.jpg", b"keep")
            env = clean_env(PUSH_IMAGES_HOME=home)
            run_tool("setup", "--ssh-target", "fake", "--local-root", local, "--remote-root", remote, env=env)
            before = sorted(os.listdir(os.path.join(remote, "CAR")))
            first = run_tool("--fake-remote", remote, "--yes", env=env)             # default (guided) command, first run
            self.assertEqual(first.returncode, 0, first.stdout[-500:])
            self.assertEqual(sorted(os.listdir(os.path.join(remote, "CAR"))), before, "the first run must upload nothing")
            second = run_tool("push", "--fake-remote", remote, "--yes", env=env)
            self.assertEqual(second.returncode, 0, second.stdout[-500:])
            self.assertEqual(sorted(os.listdir(os.path.join(remote, "CAR"))), ["img0.jpg", "img1.jpg", "img2.jpg"])
            self.assertTrue(os.path.exists(os.path.join(remote, "OLD", "server_only.jpg")), "a file only on the server must never be touched")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_stops_safely_when_not_set_up_or_the_folder_is_missing(self):
        work = tempfile.mkdtemp(prefix="push_")
        try:
            env = clean_env(PUSH_IMAGES_HOME=os.path.join(work, "home"))
            result = run_tool("push", "--fake-remote", os.path.join(work, "r"), "--yes", env=env)
            self.assertEqual(result.returncode, 2); self.assertIn("Not set up yet", result.stdout)
            run_tool("setup", "--ssh-target", "fake", "--local-root", os.path.join(work, "nope"), "--remote-root", os.path.join(work, "r"), env=env)
            result = run_tool("check", "--fake-remote", os.path.join(work, "r"), env=env)
            self.assertEqual(result.returncode, 2); self.assertIn("STOPPED", result.stdout)
        finally:
            shutil.rmtree(work, ignore_errors=True)


class PushToolStateSurvivesGit(unittest.TestCase):
    """The tool's settings and manifest live in %LOCALAPPDATA%\\SuperSeatings\\push_images. Prove that
    `git checkout -B main origin/main` and `git clean -fd` (even -fdx) leave them untouched, using a
    real throw-away git clone that contains the tool, and a fake remote."""

    def git(self, cwd, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"git {' '.join(args)}: {result.stderr[-300:]}")
        return result.stdout

    def test_state_is_outside_the_repo_and_survives_checkout_and_clean(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        work = tempfile.mkdtemp(prefix="state_")
        try:
            origin, clone = os.path.join(work, "origin.git"), os.path.join(work, "clone")
            seed = os.path.join(work, "seed"); os.makedirs(os.path.join(seed, "tools"))
            shutil.copytree(TOOL_DIR, os.path.join(seed, "tools", "push_images"), ignore=shutil.ignore_patterns("__pycache__"))
            self.git(work, "init", "-q", "--bare", "-b", "main", origin)
            self.git(seed, "init", "-q", "-b", "main"); self.git(seed, "add", "-A"); self.git(seed, "commit", "-q", "-m", "seed")
            self.git(seed, "push", "-q", origin, "main"); self.git(work, "clone", "-q", origin, clone)
            tool = os.path.join(clone, "tools", "push_images", "push_images.py")
            lad = os.path.join(work, "LocalAppData"); local, remote = os.path.join(work, "photos"), os.path.join(work, "server")
            for i in range(3): put(local, f"CAR/img{i}.jpg", b"y" * (30 + i))
            put(remote, "CAR/img0.jpg", b"y" * 30)
            env = clean_env(LOCALAPPDATA=lad)            # the DEFAULT location logic, not the test override
            def run(*args): return subprocess.run([sys.executable, tool, *args], cwd=os.path.dirname(tool), env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertEqual(run("setup", "--ssh-target", "fake", "--local-root", local, "--remote-root", remote).returncode, 0)
            self.assertEqual(run("check", "--fake-remote", remote).returncode, 0)
            state = os.path.join(lad, "SuperSeatings", "push_images")
            files = {n: sha(os.path.join(state, n)) for n in ("config.json", "manifest.json")}
            self.assertFalse(os.path.commonpath([state, clone]) == clone, "state must not be inside the git folder")
            # hostile git housekeeping inside the clone
            junk = os.path.join(clone, "junk_untracked.txt"); open(junk, "w").write("x")
            self.git(clone, "checkout", "-q", "-B", "main", "origin/main"); self.git(clone, "clean", "-fd"); self.git(clone, "clean", "-fdx")
            self.assertFalse(os.path.exists(junk), "git clean really ran")
            self.assertEqual({n: sha(os.path.join(state, n)) for n in files}, files, "settings/manifest changed or vanished")
            # and the tool carries on from where it was: not a first run any more, so it uploads the 2 new files
            result = run("push", "--fake-remote", remote, "--yes")
            self.assertEqual(result.returncode, 0, result.stdout[-400:])
            self.assertEqual(sorted(os.listdir(os.path.join(remote, "CAR"))), ["img0.jpg", "img1.jpg", "img2.jpg"])
        finally:
            shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(sys.platform == "win32", "the .bat launcher is Windows-only")
class PushBatLauncher(unittest.TestCase):
    def test_bat_never_closes_before_showing_the_result(self):
        text = open(os.path.join(TOOL_DIR, "push_images.bat"), encoding="utf-8").read().lower()
        self.assertNotIn("exit", text.replace("exit code", ""), "no early exit: every path must reach pause")
        self.assertTrue(text.rstrip().splitlines()[-2].strip() in ("pause", "endlocal") or "pause" in text.split(":end")[-1])

    def test_bat_runs_the_selftest_and_pauses(self):
        work = tempfile.mkdtemp(prefix="bat_")
        try:
            shutil.copytree(TOOL_DIR, os.path.join(work, "tool"), ignore=shutil.ignore_patterns("__pycache__"))
            result = subprocess.run(["cmd.exe", "/c", ".\\push_images.bat", "selftest"], cwd=os.path.join(work, "tool"),
                                    env=clean_env(), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertIn("Self-test:", result.stdout); self.assertIn("Finished OK", result.stdout)
            self.assertIn("Press any key", result.stdout, "the window would have closed before the result could be read")
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
