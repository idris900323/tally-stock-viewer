"""update_feeder.bat (the safe way to update the shop PC): its role / name / pre-flight checks.

Windows only. Runs the real .bat in a throw-away folder that is NOT a git repo, with a dummy venv python:
every refusal happens before any git or scheduled-task action, and the "passing" cases stop at the
harmless pre-flight line, so this can never change a real task or repository."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.support import ROOT

BAT = os.path.join(ROOT, "update_feeder.bat")
TOKEN = b"CLOUD_SYNC_TOKEN=SUPERSECRET123\r\n"
NOSUCH = b"FEEDER_TASK_NAME=ZZ_check_all_no_such_task\r\n"      # keeps the "passing" cases from ever finding a real task

CASES = [  # (label, .env bytes or None, expected: "pass" = reaches the pre-flight line, or the refusal text)
    ("clean role line, CRLF", b"A=1\r\nMACHINE_ROLE=feeder\r\n" + NOSUCH + TOKEN, "pass"),
    ("clean role line, trailing spaces", b"MACHINE_ROLE=feeder   \r\n" + NOSUCH + TOKEN, "pass"),
    ("lowercase key", b"machine_role=feeder\r\n" + NOSUCH + TOKEN, "pass"),
    ("a commented-out line plus the real one", b"# MACHINE_ROLE=full\r\nMACHINE_ROLE=feeder\r\n" + NOSUCH + TOKEN, "pass"),
    ("custom task name is used", b"MACHINE_ROLE=feeder\r\nFEEDER_TASK_NAME=Shop Feeder Task\r\n" + TOKEN, "pass"),
    ("BOM on the role line", b"\xef\xbb\xbfMACHINE_ROLE=feeder\r\n" + TOKEN, "exactly one MACHINE_ROLE line; found 0"),
    ("quoted value", b'MACHINE_ROLE="feeder"\r\n' + TOKEN, "not exactly  MACHINE_ROLE=feeder"),
    ("spaces around =", b"MACHINE_ROLE = feeder\r\n" + TOKEN, "not exactly  MACHINE_ROLE=feeder"),
    ("only a commented-out line", b"#MACHINE_ROLE=feeder\r\n" + TOKEN, "exactly one MACHINE_ROLE line; found 0"),
    ("indented line", b"  MACHINE_ROLE=feeder\r\n" + TOKEN, "exactly one MACHINE_ROLE line; found 0"),
    ("duplicate identical lines", b"MACHINE_ROLE=feeder\r\nMACHINE_ROLE=feeder\r\n" + TOKEN, "found 2"),
    ("conflicting duplicate lines", b"MACHINE_ROLE=feeder\r\nMACHINE_ROLE=full\r\n" + TOKEN, "found 2"),
    ("role is full", b"MACHINE_ROLE=full\r\n" + TOKEN, "not exactly  MACHINE_ROLE=feeder"),
    ("empty value", b"MACHINE_ROLE=\r\n" + TOKEN, "not exactly  MACHINE_ROLE=feeder"),
    ("inline comment", b"MACHINE_ROLE=feeder # shop\r\n" + TOKEN, "not exactly  MACHINE_ROLE=feeder"),
    ("no role line", b"A=1\r\n" + TOKEN, "exactly one MACHINE_ROLE line; found 0"),
    ("no .env at all", None, "No .env here"),
    ("task name with shell characters", b"MACHINE_ROLE=feeder\r\nFEEDER_TASK_NAME=a&calc\r\n" + TOKEN, "FEEDER_TASK_NAME in .env has characters"),
]


@unittest.skipUnless(sys.platform == "win32", "update_feeder.bat is Windows-only")
class UpdateFeederBatChecks(unittest.TestCase):
    def run_bat(self, env_bytes, venv=True):
        work = tempfile.mkdtemp(prefix="upd_")
        try:
            shutil.copy(BAT, os.path.join(work, "update_feeder.bat"))
            if venv:
                os.makedirs(os.path.join(work, ".venv", "Scripts")); open(os.path.join(work, ".venv", "Scripts", "python.exe"), "wb").close()
            if env_bytes is not None:
                open(os.path.join(work, ".env"), "wb").write(env_bytes)
            env = {k: v for k, v in os.environ.items() if k.upper() not in ("MACHINE_ROLE",)}
            out = subprocess.run(["cmd.exe", "/c", ".\\update_feeder.bat"], cwd=work, env=env, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL).stdout
            return out.replace("\r", "")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_role_and_name_checks(self):
        for label, env_bytes, expected in CASES:
            with self.subTest(label):
                out = self.run_bat(env_bytes)
                self.assertNotIn("SUPERSECRET123", out, "the sync token must never be printed")
                if expected == "pass":
                    self.assertTrue("[INFO] Folder:" in out, f"should pass the role+name checks:\n{out[-400:]}")
                    self.assertNotIn("Nothing was changed.\n[ERROR]", out)
                else:
                    self.assertIn(expected, out)
                    self.assertNotIn("[INFO] Folder:", out, "must refuse before doing anything")

    def test_requires_the_venv_python(self):
        out = self.run_bat(b"MACHINE_ROLE=feeder\r\n" + NOSUCH, venv=False)
        self.assertIn("python.exe not found", out)

    def test_an_unknown_task_stops_before_any_git_action(self):
        out = self.run_bat(b"MACHINE_ROLE=feeder\r\n" + NOSUCH)
        self.assertIn("No scheduled task named", out); self.assertIn("Nothing was changed", out)
        self.assertNotIn("Fetching", out)


GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]


def _git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(sys.platform == "win32", "update_feeder.bat is Windows-only")
class UpdateFeederRollback(unittest.TestCase):
    """Real .bat, real git, real venv (with pip), a fake schtasks that only logs its arguments, and a pip index
    pointing at nothing (so no network is used): a failed package install or import check must put the
    previous commit back and restart the task; a good update must stay on the new commit."""

    def update(self, new_requirements, new_feeder):
        root = tempfile.mkdtemp(prefix="upr", dir=os.environ.get("CHECK_ALL_SHORT_TMP") or None)
        try:
            origin, seed, work = (os.path.join(root, n) for n in ("origin.git", "seed", "shop"))
            _git(root, "init", "-q", "--bare", "-b", "main", origin); _git(root, "clone", "-q", origin, seed)

            def commit(msg, reqs, feeder):
                shutil.copy(BAT, seed)
                open(os.path.join(seed, "requirements-feeder.txt"), "w").write(reqs)
                open(os.path.join(seed, "feeder.py"), "w").write(feeder)
                _git(seed, "add", "-A"); _git(seed, "commit", "-q", "-m", msg)
                return _git(seed, "rev-parse", "HEAD")
            old = commit("old", "# nothing to install
", "VERSION = 'old'
"); _git(seed, "push", "-q", "origin", "HEAD:main")
            _git(root, "clone", "-q", origin, work)
            new = commit("new", new_requirements, new_feeder); _git(seed, "push", "-q", "origin", "HEAD:main")
            open(os.path.join(work, ".env"), "w").write("MACHINE_ROLE=feeder
FEEDER_TASK_NAME=TestTask
CLOUD_SYNC_TOKEN=SUPERSECRET123
")
            subprocess.run([sys.executable, "-m", "venv", os.path.join(work, ".venv")], check=True, capture_output=True)
            bindir, log = os.path.join(root, "bin"), os.path.join(root, "schtasks.log"); os.makedirs(bindir)
            open(os.path.join(bindir, "schtasks.cmd"), "w").write('@echo %* >> "%FAKELOG%"
@exit /b 0
')
            env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"], FAKELOG=log, PIP_INDEX_URL="http://127.0.0.1:9/simple",
                       PIP_RETRIES="0", PIP_TIMEOUT="3", PIP_DISABLE_PIP_VERSION_CHECK="1")
            env.pop("MACHINE_ROLE", None)
            r = subprocess.run(["cmd.exe", "/c", os.path.join(work, "update_feeder.bat")], cwd=root, env=env, input="
", capture_output=True, text=True, timeout=300)
            head = _git(work, "rev-parse", "HEAD")
            calls = [c.strip() for c in (open(log).read().split("
") if os.path.exists(log) else []) if c.strip()]
            return r, head, old, new, calls
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_good_update_stays_on_the_new_commit(self):
        r, head, old, new, calls = self.update("# nothing to install
", "VERSION = 'new'
")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(head, new)
        self.assertIn("[OK] Feeder updated", r.stdout)
        self.assertEqual(calls, ['/Query /TN "TestTask"', '/End /TN "TestTask"', '/Run /TN "TestTask"'])

    def test_a_pip_failure_restores_the_old_commit_and_restarts_the_task(self):
        r, head, old, new, calls = self.update("this-package-does-not-exist-zz9==1.0
", "VERSION = 'new'
")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(head, old, "the previous commit must be put back")
        self.assertIn("[ROLLBACK] pip could not install", r.stdout)
        self.assertEqual(calls[-2:], ['/End /TN "TestTask"', '/Run /TN "TestTask"'])
        self.assertNotIn("SUPERSECRET123", r.stdout + r.stderr)

    def test_an_import_failure_restores_the_old_commit_and_restarts_the_task(self):
        r, head, old, new, calls = self.update("# nothing to install
", "raise ImportError('broken')
")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(head, old)
        self.assertIn("[ROLLBACK] feeder.py no longer loads", r.stdout)
        self.assertEqual(calls[-2:], ['/End /TN "TestTask"', '/Run /TN "TestTask"'])


class FeederRequirementsFile(unittest.TestCase):
    """requirements-feeder.txt is loose ranges only (an exact pin can need a newer Python than the shop PC's 3.11)."""

    def lines(self):
        txt = open(os.path.join(ROOT, "requirements-feeder.txt"), encoding="utf-8").read()
        return [l.strip() for l in txt.splitlines() if l.strip() and not l.lstrip().startswith("#")]

    def test_no_exact_pins_and_the_runtime_packages_are_listed(self):
        lines = self.lines()
        self.assertFalse([l for l in lines if "==" in l], "exact pins are not allowed in requirements-feeder.txt")
        names = {l.split(">")[0].split("<")[0].split("=")[0].strip().lower() for l in lines}
        for needed in ("flask", "requests", "pillow", "psutil", "pandas", "openpyxl", "xlrd", "python-dotenv"):
            self.assertIn(needed, names)
        for not_needed in ("waitress", "pystray", "google-api-python-client", "cryptography"):
            self.assertNotIn(not_needed, names)

    def test_every_shop_pc_entry_point_uses_it(self):
        for rel in ("update_feeder.bat", os.path.join("docs", "RUNBOOK.md"), "FEEDER_SETUP.md"):
            txt = open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace").read()
            self.assertIn("requirements-feeder.txt", txt, rel)
            self.assertNotIn("pip install -r requirements.txt", txt, rel + " must not tell the shop PC to install the full list")


if __name__ == "__main__":
    unittest.main()
