"""Regression test for the cloud-backup connection leak (found 2026-10-18).

Every backup run used to leave 16 more sockets open (15 listing-pool Drive clients + the main one) because
nothing closed the googleapiclient services. This runs the REAL cloud_backup code with the REAL
googleapiclient / httplib2 / google-auth libraries against a local fake Drive HTTP server (keep-alive
connections, 15 threads) for several runs in one process, and requires the process to hold ZERO sockets to
the server after every run. Never talks to real Drive; localhost only."""
import gc
import logging
import os
import random
import shutil
import sqlite3
import tempfile
import unittest

from tests import support
from tests.support import ROOT

import cloud_backup as cb
from config import Config
from tests.fakedrive_http import start_server


def client_sockets(port):
    import psutil
    proc = psutil.Process()
    getter = getattr(proc, "net_connections", None) or proc.connections
    return sum(1 for c in getter(kind="tcp") if c.raddr and c.raddr.port == port)


class BackupSocketsAreClosed(unittest.TestCase):
    RUNS = 5

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.mkdtemp(prefix="sockets_")
        data = os.path.join(self.tmp, "data"); os.makedirs(os.path.join(data, "S.S IMAGE"))
        self._saved = (cb.DATA_DIR, cb.IMAGE_ROOT, cb.MANIFEST_PATH, cb.STATUS_PATH, cb.SNAPSHOT_DIR, cb.is_configured,
                       cb._load_saved_credentials, cb.MIN_CALL_INTERVAL_SECONDS, Config.DB_PATH, Config.GDRIVE_BACKUP_FOLDER_ID)
        cb.DATA_DIR = data; cb.IMAGE_ROOT = os.path.join(data, "S.S IMAGE"); cb.MANIFEST_PATH = os.path.join(data, ".m.json")
        cb.STATUS_PATH = os.path.join(data, ".s.json"); cb.SNAPSHOT_DIR = os.path.join(data, ".snap")
        Config.DB_PATH = os.path.join(data, "mappings.db"); Config.GDRIVE_BACKUP_FOLDER_ID = "ROOT"
        cb.is_configured = lambda: True; cb.MIN_CALL_INTERVAL_SECONDS = 0
        self.server, self.store, self.port = start_server(); self.store.latency = 0.02   # slow enough that all 15 threads are used
        from google.oauth2.credentials import Credentials
        cb._load_saved_credentials = lambda: Credentials(token="fake", refresh_token="r", token_uri=f"http://127.0.0.1:{self.port}/token",
                                                       client_id="c", client_secret="s")
        import googleapiclient.discovery as discovery
        import httplib2
        self._real_build, self._real_request = discovery.build, httplib2.Http.request
        port = self.port
        discovery.build = lambda *a, **k: self._real_build(*a, client_options={"api_endpoint": f"http://127.0.0.1:{port}/"}, **k)
        httplib2.Http.request = lambda s, uri, *a, **k: self._real_request(s, uri.replace(f"https://127.0.0.1:{port}", f"http://127.0.0.1:{port}"), *a, **k)
        self._discovery, self._httplib2 = discovery, httplib2
        rnd = random.Random(1)
        for f in range(60):                     # 60 car folders so every pool thread gets work
            d = os.path.join(cb.IMAGE_ROOT, f"CAR {f}"); os.makedirs(d)
            for i in range(3):
                with open(os.path.join(d, f"i{i}.jpg"), "wb") as handle: handle.write(rnd.randbytes(2000 + f + i))
        for name in cb.INCLUDED_JSON_FILES:
            with open(os.path.join(data, name), "w") as handle: handle.write("{}")
        c = sqlite3.connect(Config.DB_PATH); c.execute("create table t(x)"); c.commit(); c.close()

    def tearDown(self):
        self._discovery.build, self._httplib2.Http.request = self._real_build, self._real_request
        (cb.DATA_DIR, cb.IMAGE_ROOT, cb.MANIFEST_PATH, cb.STATUS_PATH, cb.SNAPSHOT_DIR, cb.is_configured, cb._load_saved_credentials,
         cb.MIN_CALL_INTERVAL_SECONDS, Config.DB_PATH, Config.GDRIVE_BACKUP_FOLDER_ID) = self._saved
        self.server.shutdown(); shutil.rmtree(self.tmp, ignore_errors=True); logging.disable(logging.CRITICAL)

    def run_once(self, n):
        with open(os.path.join(cb.IMAGE_ROOT, "CAR 0", "i0.jpg"), "ab") as handle: handle.write(b"x" * n)   # something to update each run
        return cb.run_sync("test")["last_run"]

    def test_sockets_are_zero_after_every_run_even_without_garbage_collection(self):
        real_collect = cb.gc.collect
        cb.gc.collect = lambda *a, **k: 0       # prove the explicit close() works, not luck from the collector
        gc.disable()
        try:
            for run in range(1, self.RUNS + 1):
                result = self.run_once(run)
                self.assertEqual(result["status"], "success", result.get("error"))
                self.assertEqual(client_sockets(self.port), 0, f"run {run}: sockets left open")
                self.assertEqual(self.store.open_connections, 0, f"run {run}: server still sees open connections")
        finally:
            gc.enable(); cb.gc.collect = real_collect
        self.assertGreaterEqual(self.store.max_open_connections, 10, "the 15-thread pool was not exercised")

    def test_sockets_and_memory_stay_flat_over_consecutive_runs(self):
        import psutil
        proc = psutil.Process(); rss = []
        for run in range(1, self.RUNS + 4):
            self.assertEqual(self.run_once(run)["status"], "success")
            self.assertEqual(client_sockets(self.port), 0)
            rss.append(proc.memory_info().rss / 1048576)
        self.assertLess(rss[-1] - rss[2], 25, f"memory keeps growing run after run: {[round(r) for r in rss]}")

    def test_clients_are_closed_even_when_a_run_fails(self):
        real = cb._list_drive_tree
        def boom(*a, **k):
            real(*a, **k); raise RuntimeError("simulated failure after the listing")
        cb._list_drive_tree = boom
        try:
            result = self.run_once(1)
        finally:
            cb._list_drive_tree = real
        self.assertIn("Drive listing failed", result.get("error") or "")
        self.assertEqual(client_sockets(self.port), 0)

    def test_the_finish_line_reports_before_and_after_and_warns_on_a_leak(self):
        logging.disable(logging.NOTSET)
        records = []
        class Grab(logging.Handler):
            def emit(self, record): records.append((record.levelname, record.getMessage()))
        handler = Grab(); cb.logger.addHandler(handler); old = cb.logger.level; cb.logger.setLevel(logging.INFO)
        try:
            self.run_once(1)
            line = [m for _l, m in records if "sync finished (" in m]
            self.assertTrue(line and "open fds" in line[0] and "sockets" in line[0] and "RSS" in line[0] and "->" in line[0], line)
            self.assertFalse([1 for lvl, _m in records if lvl == "WARNING" and "connection leak" in _m])
            records.clear(); real = cb._close_services
            cb._close_services = lambda kind=None: 0           # simulate a regression
            try:
                self.run_once(2); self.run_once(3)
            finally:
                cb._close_services = real; cb._close_services()
            self.assertTrue([1 for lvl, m in records if lvl == "WARNING" and "possible connection leak" in m], "a leak must be reported in plain words")
        finally:
            cb.logger.removeHandler(handler); cb.logger.setLevel(old)


if __name__ == "__main__":
    unittest.main()
