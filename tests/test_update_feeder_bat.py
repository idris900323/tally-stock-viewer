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


if __name__ == "__main__":
    unittest.main()
