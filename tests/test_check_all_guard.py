"""The safety rails of tests/check_all.py: it must refuse to run against the real data/ folder, there is no
override switch, the test modules refuse to run outside the temporary copy, nothing is ever copied back into
the project, and an interrupted run removes only its own temporary folder.

Everything here happens in throw-away "fake project" folders under the system temp folder."""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import ROOT
import copy_guard

DUMMY_TEST = '''import os, sys, time, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from copy_guard import require_scratch
class T(unittest.TestCase):
    def test_it(self):
        require_scratch(ROOT)
        os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
        open(os.path.join(ROOT, "data", "written_by_test.txt"), "w").write("x")
        flag = os.environ.get("DUMMY_STARTED_FLAG")
        if flag:
            open(flag, "w").write("started")
            time.sleep(120)
'''


def tree_hash(folder):
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')   # Python's own bytecode cache is not project content
        for name in sorted(files):
            path = os.path.join(base, name)
            digest.update(os.path.relpath(path, folder).encode())
            with open(path, "rb") as fh:
                digest.update(fh.read())
    return digest.hexdigest()


def scratch_dirs():
    return {d for d in os.listdir(tempfile.gettempdir()) if d.startswith("check_all_") and not d.startswith("check_all_goldens_")}


class CheckAllGuard(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="guardtest_")
        self.proj = os.path.join(self.base, "proj")
        os.makedirs(os.path.join(self.proj, "tests"))
        os.makedirs(os.path.join(self.proj, "data"))
        for name in ("check_all.py", "copy_guard.py", "support.py"):
            shutil.copy2(os.path.join(ROOT, "tests", name), os.path.join(self.proj, "tests", name))
        open(os.path.join(self.proj, "tests", "__init__.py"), "w").close()
        with open(os.path.join(self.proj, "tests", "test_dummy.py"), "w") as fh:
            fh.write(DUMMY_TEST)
        with open(os.path.join(self.proj, "data", "mappings.db"), "w") as fh:
            fh.write("REAL DATA")
        self.data_before = tree_hash(os.path.join(self.proj, "data"))
        self.dirs_before = scratch_dirs()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CHECK_ALL")}
        env.update(extra)
        return env

    def run_runner(self, **extra):
        return subprocess.run([sys.executable, os.path.join(self.proj, "tests", "check_all.py"), "-k", "dummy"], cwd=self.proj,
                              env=self.env(**extra), capture_output=True, text=True, timeout=300)

    def test_a_normal_run_passes_and_leaves_the_project_untouched(self):
        project_before = tree_hash(self.proj)
        r = self.run_runner()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(tree_hash(self.proj), project_before, "the project folder must not change at all")
        self.assertEqual(scratch_dirs(), self.dirs_before, "the temporary copy must be removed")

    def test_it_refuses_when_a_data_path_points_into_the_real_data_folder(self):
        cases = (("DB_PATH", os.path.join(self.proj, "data", "mappings.db")),
                 ("LOG_FILE", os.path.join(self.proj, "data", "x.log")),
                 ("IMAGE_SCAN_ROOT", os.path.join(self.proj, "data", "S.S IMAGE")),
                 ("DB_PATH", os.path.join(self.proj, "tests", "..", "data", "t.db")))
        for setting, value in cases:
            with self.subTest(setting=setting, value=value):
                r = self.run_runner(**{setting: value})
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("REFUSED TO START", r.stderr)
                self.assertEqual(tree_hash(os.path.join(self.proj, "data")), self.data_before)
                self.assertNotIn("test_it", r.stdout + r.stderr, "no test may have run")
                self.assertEqual(scratch_dirs(), self.dirs_before)

    def test_there_is_no_override_switch(self):
        for extra in ({"CHECK_ALL_SCRATCH": "1"}, {"CHECK_ALL_NONCE": "anything", "CHECK_ALL_SCRATCH": "1"}):
            r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'tests'); import support"], cwd=self.proj,
                               env=self.env(**extra), capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("Refusing to run the tests here", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.proj, "data", "test.db")))
        self.assertEqual(tree_hash(os.path.join(self.proj, "data")), self.data_before)

    def test_a_forged_marker_is_not_enough(self):
        with open(os.path.join(self.proj, copy_guard.MARKER), "w") as fh:
            json.dump({"nonce": "abc", "source": self.proj}, fh)
        self.assertTrue(copy_guard.scratch_problems(self.proj, {"CHECK_ALL_NONCE": "abc"}), "a project folder outside check_all_* must be refused")
        self.assertTrue(copy_guard.scratch_problems(self.proj, {}), "no nonce from the runner: refused")

    def test_cleanup_only_ever_deletes_its_own_folder(self):
        mine = tempfile.mkdtemp(prefix=copy_guard.PREFIX)
        decoy = tempfile.mkdtemp(prefix=copy_guard.PREFIX)
        try:
            with open(os.path.join(mine, copy_guard.MARKER), "w") as fh:
                json.dump({"nonce": "n1", "source": self.proj}, fh)
            self.assertTrue(copy_guard.safe_to_delete(mine, "n1"))
            self.assertFalse(copy_guard.safe_to_delete(mine, "other-nonce"))
            self.assertFalse(copy_guard.safe_to_delete(decoy, "n1"), "a folder without our marker is never deleted")
            self.assertFalse(copy_guard.safe_to_delete(tempfile.gettempdir(), "n1"))
            self.assertFalse(copy_guard.safe_to_delete(self.proj, "n1"))
        finally:
            shutil.rmtree(mine, ignore_errors=True)
            shutil.rmtree(decoy, ignore_errors=True)

    def test_an_interrupted_run_removes_only_its_own_temporary_folder(self):
        before = scratch_dirs()
        decoy = tempfile.mkdtemp(prefix=copy_guard.PREFIX)          # someone else's folder with the same prefix
        with open(os.path.join(decoy, "keep.txt"), "w") as fh:
            fh.write("not yours")
        flag = os.path.join(self.base, "started.flag")
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}
        proc = subprocess.Popen([sys.executable, os.path.join(self.proj, "tests", "check_all.py"), "-k", "dummy"], cwd=self.proj,
                                env=self.env(DUMMY_STARTED_FLAG=flag), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **flags)
        try:
            for _ in range(300):
                if os.path.exists(flag) or proc.poll() is not None:
                    break
                time.sleep(0.2)
            self.assertTrue(os.path.exists(flag), "the test run never started")
            self.assertGreater(len(scratch_dirs() - before), 0, "the runner should be working in a temporary copy now")
            proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
            proc.communicate(timeout=120)
            self.assertEqual(scratch_dirs() - before, {os.path.basename(decoy)}, "only the decoy may remain")
            self.assertTrue(os.path.exists(os.path.join(decoy, "keep.txt")), "someone else's folder was deleted")
            self.assertEqual(tree_hash(os.path.join(self.proj, "data")), self.data_before)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            shutil.rmtree(decoy, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
