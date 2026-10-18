"""The checks that must hold before anything is deployed to Render (run as separate processes, in
throw-away copies of the code, so nothing here can touch the real data/ folder or .env):

 * Render-like boot: no .env, no MACHINE_ROLE, cloud mode, JSON caches present at boot
 * the MACHINE_ROLE guard: every web-app entry point refuses on a feeder machine; .env parsing fails closed
 * feeder.py against a mock Tally and a mock intake
"""
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests import support
from tests.support import ROOT, PAYLOAD

SKIP = shutil.ignore_patterns("data", "logs", ".state_snapshot", "__pycache__", "tests", ".git", ".env", ".venv")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k.upper() != "MACHINE_ROLE" and not k.startswith(("GDRIVE", "CLOUD_SYNC", "TEST_")) and k not in support.TEST_ENV}
    env.update(PYTHONUNBUFFERED="1", **extra)
    return env


def make_copy(prefix):
    dest = tempfile.mkdtemp(prefix=prefix)
    shutil.rmtree(dest); shutil.copytree(ROOT, dest, ignore=SKIP); os.makedirs(os.path.join(dest, "data"))
    return dest


def http(method, url, data=None, headers=None, cookies=None):
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k): return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


class RenderLikeBoot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = make_copy("boot_")
        cls.port = free_port(); cls.base = f"http://127.0.0.1:{cls.port}"
        secrets = os.path.join(cls.dir, "fake_client_secrets.json"); open(secrets, "w").write("{}")
        cls.env = clean_env(PORT=str(cls.port), DISABLE_TALLY_SCHEDULING="1", INTAKE_SYNC_TOKEN="tok123", DB_PATH="data/t.db",
                            FLASK_SECRET_KEY="k", INITIAL_IMAGE_SCAN="0", LOG_FILE="logs/app.log", GDRIVE_OAUTH_CLIENT_SECRETS_PATH=secrets,
                            GDRIVE_OAUTH_TOKEN_PATH=os.path.join(cls.dir, "fake_token.json"), GDRIVE_BACKUP_FOLDER_ID="fakefolder")
        assert not os.path.exists(os.path.join(cls.dir, ".env"))
        cls.proc = cls.serve(); cls.hdr = {"X-Intake-Token": "tok123", "Content-Type": "application/json"}
        cls.seeded = http("POST", cls.base + "/admin/intake/sync_data", json.dumps(PAYLOAD).encode(), cls.hdr)[0] == 200
        cls.stop(); cls.proc = cls.serve()      # restart: the JSON caches are now present at boot, like a Render redeploy

    @classmethod
    def serve(cls):
        out = open(os.path.join(cls.dir, "server.out"), "ab")
        proc = subprocess.Popen([sys.executable, "serve.py"], cwd=cls.dir, env=cls.env, stdout=out, stderr=out)
        for _ in range(100):
            try:
                if http("GET", cls.base + "/health")[0]: break
            except Exception: time.sleep(0.3)
        return proc

    @classmethod
    def stop(cls):
        cls.proc.terminate()
        try: cls.proc.wait(10)
        except Exception: cls.proc.kill()

    @classmethod
    def tearDownClass(cls):
        cls.stop(); shutil.rmtree(cls.dir, ignore_errors=True)

    def login(self, user, code):
        data = f"username={user}&access_code={code}".encode()
        status, _b, headers = http("POST", self.base + "/login", data, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn(status, (302, 303))
        return {"Cookie": headers["Set-Cookie"].split(";")[0]}

    def test_core_requests_work(self):
        self.assertTrue(self.seeded)
        self.assertEqual(http("GET", self.base + "/health")[0], 200)
        self.assertEqual(http("GET", self.base + "/static/shared.css")[0], 200, "Render's health check path")
        self.assertEqual(http("GET", self.base + "/login")[0], 200)
        cookie = self.login("star", "111")
        status, body, _h = http("GET", self.base + "/last_update", headers=cookie)
        self.assertIn("data_version", json.loads(body))
        status, body, _h = http("GET", self.base + "/designs?car=ALTO", headers=cookie)
        self.assertEqual(len(json.loads(body)), 2)
        self.assertEqual(http("POST", self.base + "/api/activity", json.dumps({"action": "copy_image", "detail": "x"}).encode(), dict(cookie, **{"Content-Type": "application/json"}))[0], 204)
        self.assertEqual(http("POST", self.base + "/admin/intake/sync_data", json.dumps(PAYLOAD).encode(), self.hdr)[0], 200)
        rows = dict(sqlite3.connect(os.path.join(self.dir, "data", "t.db")).execute("select action,count(*) from user_activity group by 1").fetchall())
        self.assertGreaterEqual(rows.get("login", 0), 1); self.assertEqual(rows.get("copy_image"), 1); self.assertGreaterEqual(rows.get("view_car", 0), 1)

    def test_logs_show_no_guard_and_the_backup_schedule(self):
        log = open(os.path.join(self.dir, "logs", "app.log"), encoding="utf-8", errors="replace").read()
        self.assertFalse(re.search(r"machine_role|guard|feeder-only|MACHINE_ROLE", log, re.I), "the guard must be invisible on Render")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "logs", "role_guard.log")))
        self.assertRegex(log, r"Cloud backup: next scheduled run at .* 02:00 IST")
        self.assertNotRegex(log, r"Traceback| ERROR ")

    def test_env_file_is_never_opened_after_startup(self):
        code = (
            "import os, sys\n"
            "os.environ.update(DISABLE_TALLY_SCHEDULING='1', DB_PATH='data/a.db', INITIAL_IMAGE_SCAN='0', LOG_FILE='logs/a.log', INTAKE_SYNC_TOKEN='tok')\n"
            "opens=[]; phase=['import']\n"
            "def hook(ev,args):\n"
            "    if ev=='open' and args and isinstance(args[0],(str,bytes,os.PathLike)) and os.path.basename(os.fsdecode(args[0]))=='.env': opens.append(phase[0])\n"
            "sys.addaudithook(hook)\n"
            "import app, database\n"
            "phase[0]='after'\n"
            "c=app.app.test_client(); c.post('/login',data={'username':'star','access_code':'111'})\n"
            "for i in range(300): c.get(('/last_update','/cars','/api/categories')[i%3])\n"
            "for _ in range(300):\n"
            "    with database.get_connection() as conn: conn.execute('select 1')\n"
            "print('ENV_OPENS_AFTER', opens.count('after'))\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=self.dir, env=clean_env(), capture_output=True, text=True, timeout=120)
        self.assertIn("ENV_OPENS_AFTER 0", result.stdout, result.stderr[-500:])


class GuardRefusals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = make_copy("guard_")
        open(os.path.join(cls.dir, ".env"), "w", newline="").write("MACHINE_ROLE=feeder\r\nCLOUD_SYNC_TOKEN=tok123\r\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_every_web_app_entry_point_refuses_before_doing_anything(self):
        before = set(os.listdir(self.dir))
        for script in ("serve.py", "launcher.pyw", "relaunch_helper.py", "app.py"):
            result = subprocess.run([sys.executable, script], cwd=self.dir, env=clean_env(), capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 3, script)
            self.assertIn("feeder-only", result.stderr, script)
        result = subprocess.run([sys.executable, "-c", "import app"], cwd=self.dir, env=clean_env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 3)
        result = subprocess.run([sys.executable, "-c", "import database; database._connect()"], cwd=self.dir, env=clean_env(), capture_output=True, text=True, timeout=60)
        self.assertIn("refused", result.stderr)
        created = {n for n in set(os.listdir(self.dir)) - before if n != "__pycache__"}
        self.assertEqual(created, set(), "a refusal must not create data/, logs/, a database, a pid file ...")

    def test_environment_variable_overrides_env_file_and_full_boots(self):
        result = subprocess.run([sys.executable, "-c", "import machine_role as m; print(m.get_role())"], cwd=self.dir,
                                env=clean_env(MACHINE_ROLE="full"), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "full")

    def test_env_parsing_fails_closed(self):
        cases = {  # content -> web app allowed?
            b"MACHINE_ROLE=feeder\r\n": False, b"\xef\xbb\xbfMACHINE_ROLE=feeder\r\nA=1\r\n": False, b"MACHINE_ROLE=feeder   \r\n": False,
            b'MACHINE_ROLE="feeder"\r\n': False, b"MACHINE_ROLE=Feeder\r\n": False, b"machine_role=feeder\r\n": False,
            b"MACHINE_ROLE = feeder\r\n": False, b"export MACHINE_ROLE=feeder\r\n": False, b"MACHINE_ROLE=feeder # shop\r\n": False,
            b"MACHINE_ROLE=feeder\r\nMACHINE_ROLE=feeder\r\n": False,
            b"MACHINE_ROLE=feeder\r\nMACHINE_ROLE=full\r\n": False, b"MACHINE_ROLE=full\r\nMACHINE_ROLE=feeder\r\n": False,
            b"MACHINE_ROLE=\r\n": False, b"MACHINE_ROLE=feder\r\n": False, b'MACHINE_ROLE="feeder\r\n': False,
            b'MACHINE_ROLE="feeder" junk\r\n': False, "MACHINE_ROLE=feeder\r\n".encode("utf-16"): False,
            b"MACHINE_ROLE=full\r\n": True, b"#MACHINE_ROLE=feeder\r\nA=1\r\n": True, b"A=1\r\n": True, None: True,
        }
        for content, allowed in cases.items():
            d = tempfile.mkdtemp(prefix="role_")
            try:
                shutil.copy(os.path.join(ROOT, "machine_role.py"), d)
                if content is not None: open(os.path.join(d, ".env"), "wb").write(content)
                result = subprocess.run([sys.executable, "-c", "import machine_role as m; m.refuse_if_not_full('x')"], cwd=d, env=clean_env(), capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode == 0, allowed, f"{content!r} -> exit {result.returncode}")
            finally:
                shutil.rmtree(d, ignore_errors=True)


class FeederAgainstMocks(unittest.TestCase):
    def test_feeder_pushes_tally_data_to_the_intake(self):
        cars = ["ALTO", "SWIFT"]; items = {"ALTO": [("ALTO 7D MAT BLACK", 3), ("ALTO 9D MAT RED", 2)], "SWIFT": [("SWIFT 7D MAT GREY", 4)]}
        def tally_reply(b):
            if "List of Stock Groups" in b: return "<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(f'<STOCKGROUP NAME="{c}"/>' for c in cars) + "</COLLECTION></DATA></BODY></ENVELOPE>"
            if "List of Stock Items with Parent" in b: return "<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(f'<STOCKITEM NAME="{n}" PARENT="{p}"/>' for p, k in items.items() for n, _ in k) + "</COLLECTION></DATA></BODY></ENVELOPE>"
            if "Stock Summary" in b: return "<ENVELOPE><BODY><DATA>" + "".join(f"<DSPACCNAME><DSPDISPNAME>{n}</DSPDISPNAME></DSPACCNAME><DSPSTKINFO><DSPSTKCL><DSPCLQTY>{q} pcs</DSPCLQTY></DSPSTKCL></DSPSTKINFO>" for k in items.values() for n, q in k) + "</DATA></BODY></ENVELOPE>"
            if "Item Names With Closing" in b: return "<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(f'<STOCKITEM NAME="{n}"><CLOSINGBALANCE>{q} pcs</CLOSINGBALANCE></STOCKITEM>' for k in items.values() for n, q in k) + "</COLLECTION></DATA></BODY></ENVELOPE>"
            return "<ENVELOPE><LINEERROR>x</LINEERROR></ENVELOPE>"
        got = []
        class Tally(BaseHTTPRequestHandler):
            def do_POST(self):
                out = tally_reply(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "replace")).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass
        class Intake(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.headers.get("X-Intake-Token"), json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))))
                out = b'{"ok": true}'; self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass
        tp, ip = free_port(), free_port()
        servers = [HTTPServer(("127.0.0.1", tp), Tally), HTTPServer(("127.0.0.1", ip), Intake)]
        for s in servers: threading.Thread(target=s.serve_forever, daemon=True).start()
        d = make_copy("feeder_")
        open(os.path.join(d, ".env"), "w", newline="").write(f"MACHINE_ROLE=feeder\r\nTALLY_URL=http://127.0.0.1:{tp}\r\nCLOUD_SYNC_URL=http://127.0.0.1:{ip}/admin/intake/sync_data\r\nCLOUD_SYNC_TOKEN=tok123\r\n")
        proc = subprocess.Popen([sys.executable, "feeder.py"], cwd=d, env=clean_env(TALLY_RETRY_ATTEMPTS="1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            started = time.time()
            while time.time() - started < 60 and not got: time.sleep(0.4)
            time.sleep(0.5)
        finally:
            proc.terminate()
            try: proc.wait(10)
            except Exception: proc.kill()
            for s in servers: s.shutdown()
        try:
            self.assertTrue(got, "feeder never pushed")
            token, body = got[0]
            self.assertEqual(token, "tok123"); self.assertEqual(len(body["car_master"]), 2); self.assertEqual(len(body["item_stock"]), 3)
            log = open(os.path.join(d, "logs", "feeder.log"), encoding="utf-8", errors="replace").read()
            self.assertIn("Cloud sync push succeeded", log); self.assertIn("machine role=feeder", log)
            self.assertEqual([f for _r, _d, fs in os.walk(d) for f in fs if f.endswith(".db")], [], "the feeder must never create mappings.db")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
