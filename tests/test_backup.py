"""Cloud backup: consistent DB snapshot, Drive Trash instead of delete, mass-deletion
guard, restore/recovery behaviour -- all against an in-memory FAKE Drive (fakedrive.py).
Never talks to real Drive. Run through tests/check_all.py (which runs it in a scratch copy
of the code, so the real data/ folder and .env are never touched)."""
import os, sys, shutil, sqlite3, tempfile, random, logging, threading, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.environ.get("CHECK_ALL_SCRATCH") != "1":
    sys.exit("Run this through tests/check_all.py (it uses a scratch copy so data/ and .env are never touched).")
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_backup as cb
from config import Config
from fakedrive import FakeDrive
logging.disable(logging.CRITICAL)


class BackupCase(unittest.TestCase):
    N_IMAGES = 100

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bk_")
        data = os.path.join(self.tmp, "data"); os.makedirs(os.path.join(data, "S.S IMAGE"))
        self._saved = (cb.DATA_DIR, cb.IMAGE_ROOT, cb.MANIFEST_PATH, cb.STATUS_PATH, cb.SNAPSHOT_DIR, cb.is_configured,
                       cb.build_drive_service, cb._get_thread_local_drive_service, cb.MIN_CALL_INTERVAL_SECONDS,
                       Config.DB_PATH, Config.GDRIVE_BACKUP_FOLDER_ID, Config.BACKUP_MAX_DELETIONS)
        cb.DATA_DIR = data; cb.IMAGE_ROOT = os.path.join(data, "S.S IMAGE"); cb.MANIFEST_PATH = os.path.join(data, ".m.json")
        cb.STATUS_PATH = os.path.join(data, ".s.json"); cb.SNAPSHOT_DIR = os.path.join(data, ".snap")
        Config.DB_PATH = os.path.join(data, "mappings.db"); Config.GDRIVE_BACKUP_FOLDER_ID = "ROOT"; Config.BACKUP_MAX_DELETIONS = None
        cb.is_configured = lambda: True; cb.MIN_CALL_INTERVAL_SECONDS = 0
        self.drive = FakeDrive(); svc = self.drive.service()
        cb.build_drive_service = lambda: svc; cb._get_thread_local_drive_service = lambda: svc
        rnd = random.Random(1)
        for i in range(self.N_IMAGES):
            d = os.path.join(cb.IMAGE_ROOT, f"CAR {i % 4}'D"); os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"img_{i:03d}.jpg"), "wb").write(rnd.randbytes(1500 + i))
        for name in cb.INCLUDED_JSON_FILES: open(os.path.join(data, name), "w").write('{"x": 1}')
        c = sqlite3.connect(Config.DB_PATH); c.execute("PRAGMA journal_mode=WAL"); c.execute("create table t(id integer primary key, v text)")
        c.executemany("insert into t(v) values (?)", [(f"row{i}",) for i in range(200)]); c.commit(); c.close()

    def tearDown(self):
        (cb.DATA_DIR, cb.IMAGE_ROOT, cb.MANIFEST_PATH, cb.STATUS_PATH, cb.SNAPSHOT_DIR, cb.is_configured, cb.build_drive_service,
         cb._get_thread_local_drive_service, cb.MIN_CALL_INTERVAL_SECONDS, Config.DB_PATH, Config.GDRIVE_BACKUP_FOLDER_ID,
         Config.BACKUP_MAX_DELETIONS) = self._saved
        self.assertEqual(self.drive.calls["PERMANENT_DELETE"], 0, "a permanent Drive delete happened")
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def run_sync(self): return cb.run_sync("test")["last_run"]
    def ops(self, before): return {k: self.drive.calls[k] - before.get(k, 0) for k in self.drive.calls if self.drive.calls[k] - before.get(k, 0) and k != "list"}
    def images(self): return sorted(os.path.join(r, f) for r, _d, fs in os.walk(cb.IMAGE_ROOT) for f in fs)
    def q(self, path, sql):
        c = sqlite3.connect(path)
        try: return c.execute(sql).fetchone()[0]
        finally: c.close()
    def stash(self):
        dest = os.path.join(self.tmp, "stash"); shutil.copytree(cb.IMAGE_ROOT, dest); return dest
    def restore(self, stash):
        shutil.rmtree(cb.IMAGE_ROOT); shutil.copytree(stash, cb.IMAGE_ROOT)   # new mtimes, same bytes

    # ---- scenarios
    def test_first_run_no_change_and_db_row(self):
        r = self.run_sync()
        self.assertEqual((r["status"], r["added"], r["deleted"]), ("success", self.N_IMAGES + 4, 0))
        before = self.drive.snapshot_calls(); r = self.run_sync()
        self.assertEqual((r["status"], r["added"], r["updated"], r["skipped"]), ("success", 0, 0, self.N_IMAGES + 4))
        self.assertEqual(self.ops(before), {}, "an unchanged second run must not touch Drive")
        c = sqlite3.connect(Config.DB_PATH); c.execute("insert into t(v) values ('new')"); c.commit(); c.close()
        before = self.drive.snapshot_calls(); r = self.run_sync()
        self.assertEqual((r["updated"], r["added"]), (1, 0)); self.assertEqual(self.ops(before), {"update_media": 1})
        before = self.drive.snapshot_calls(); r = self.run_sync()
        self.assertEqual(self.ops(before), {}, "an unchanged database must not be re-uploaded")

    def test_three_deleted_go_to_trash_not_permanent(self):
        self.run_sync(); imgs = self.images()
        for p in imgs[:3]: os.remove(p)
        before = self.drive.snapshot_calls(); r = self.run_sync()
        self.assertEqual((r["status"], r["deleted"]), ("success", 3)); self.assertEqual(self.ops(before), {"trash": 3})
        self.assertEqual(len(self.drive.trashed_files()), 3); self.assertEqual(self.drive.calls["PERMANENT_DELETE"], 0)
        self.assertTrue(r["verification"]["ok"])

    def test_guard_limit_20_allowed_21_held(self):
        self.run_sync()
        for p in self.images()[:20]: os.remove(p)
        r = self.run_sync(); self.assertEqual((r["status"], r["deleted"], r["deletions_held_back"]), ("success", 20, 0))
        for p in self.images()[:21]: os.remove(p)
        before = self.drive.snapshot_calls(); r = self.run_sync()
        self.assertEqual((r["status"], r["deleted"], r["deletions_held_back"]), ("partial", 0, 21))
        self.assertEqual(self.ops(before), {}); self.assertIn("21 deletions held back", r["error"])

    def test_empty_folder_held_back_and_clean_recovery(self):
        self.run_sync(); stash = self.stash(); live_before = len(self.drive.live_files())
        shutil.rmtree(cb.IMAGE_ROOT); os.makedirs(cb.IMAGE_ROOT)
        before = self.drive.snapshot_calls(); r = self.run_sync()
        self.assertEqual((r["status"], r["deleted"], r["deletions_held_back"]), ("partial", 0, self.N_IMAGES))
        self.assertEqual(self.ops(before), {}); self.assertEqual(len(self.drive.live_files()), live_before)
        self.assertEqual(len(self.drive.trashed_files()), 0)
        self.assertEqual(len(cb.load_manifest()["files"]), live_before, "held-back entries must stay in the manifest")
        r = self.run_sync(); self.assertEqual((r["status"], r["deletions_held_back"]), ("partial", self.N_IMAGES))   # still empty next night
        self.restore(stash); before = self.drive.snapshot_calls(); r = self.run_sync()
        self.assertEqual((r["status"], r["added"], r["updated"], r["deleted"]), ("success", 0, 0, 0))
        self.assertEqual(self.ops(before), {}, "returning files with new mtimes must not be re-uploaded")
        before = self.drive.snapshot_calls(); self.run_sync(); self.assertEqual(self.ops(before), {})

    def test_half_deleted_held_back(self):
        self.run_sync()
        for p in self.images()[:50]: os.remove(p)
        r = self.run_sync(); self.assertEqual((r["status"], r["deleted"], r["deletions_held_back"]), ("partial", 0, 50))
        self.assertEqual(len(self.drive.trashed_files()), 0)

    def test_override_allows_mass_deletion_to_trash(self):
        self.run_sync(); shutil.rmtree(cb.IMAGE_ROOT); os.makedirs(cb.IMAGE_ROOT)
        Config.BACKUP_MAX_DELETIONS = 500
        r = self.run_sync(); self.assertEqual((r["status"], r["deleted"], r["deletions_held_back"]), ("success", self.N_IMAGES, 0))
        self.assertEqual(len(self.drive.trashed_files()), self.N_IMAGES)

    def test_restore_from_trash_adopts_without_reupload(self):
        self.run_sync(); stash = self.stash()
        shutil.rmtree(cb.IMAGE_ROOT); os.makedirs(cb.IMAGE_ROOT); Config.BACKUP_MAX_DELETIONS = 500; self.run_sync(); Config.BACKUP_MAX_DELETIONS = None
        self.restore(stash)
        for n in self.drive.trashed_files()[:5]: n["trashed"] = False
        before = self.drive.snapshot_calls(); r = self.run_sync()
        self.assertEqual(r["reconciliation"]["adopted"], 5); self.assertEqual(r["reconciliation"]["duplicates"], [])
        self.assertEqual(self.ops(before), {"create_media": self.N_IMAGES - 5}, "only the 95 not restored should upload")

    def test_restored_with_no_local_copy_is_left_alone_and_flagged(self):
        self.run_sync(); x = self.images()[0]; os.remove(x); self.run_sync()
        node = next(n for n in self.drive.trashed_files() if n["name"] == os.path.basename(x)); node["trashed"] = False
        before = self.drive.snapshot_calls(); r = self.run_sync()
        self.assertEqual(self.ops(before), {}); self.assertFalse(node["trashed"])
        self.assertFalse(r["verification"]["ok"]); self.assertEqual(r["status"], "partial")

    def test_duplicate_path_reporting_unchanged(self):
        self.run_sync(); y = self.images()[1]; keep = open(y, "rb").read(); os.remove(y); self.run_sync()
        open(y, "wb").write(keep); self.run_sync()
        old = next(n for n in self.drive.trashed_files() if n["name"] == os.path.basename(y)); old["trashed"] = False
        r = self.run_sync(); self.assertEqual(len(r["reconciliation"]["duplicates"]), 1)

    def test_snapshot_failure_keeps_old_drive_copy(self):
        self.run_sync(); db_id = next(n for n in self.drive.live_files() if n["name"] == "mappings.db")["id"]
        orig = cb._snapshot_database; cb._snapshot_database = lambda p: (_ for _ in ()).throw(RuntimeError("disk full"))
        try: r = self.run_sync()
        finally: cb._snapshot_database = orig
        self.assertEqual(r["status"], "partial"); self.assertIn("database snapshot failed", r["error"])
        self.assertFalse(self.drive.nodes[db_id]["trashed"]); self.assertIn("mappings.db", cb.load_manifest()["files"])
        self.assertFalse(os.path.exists(cb.SNAPSHOT_DIR), "snapshot scratch dir must be cleaned up")

    def test_snapshot_sees_wal_commits_and_is_deterministic(self):
        wc = sqlite3.connect(Config.DB_PATH); wc.execute("PRAGMA wal_autocheckpoint=0")
        wc.executemany("insert into t(v) values (?)", [(f"late{i}",) for i in range(50)]); wc.commit()
        raw = os.path.join(self.tmp, "raw.db"); shutil.copy(Config.DB_PATH, raw)
        snap = cb._snapshot_database(Config.DB_PATH)
        self.assertEqual(self.q(raw, "select count(*) from t"), 200, "a raw file copy misses the WAL commits (the bug this fixes)")
        self.assertEqual(self.q(snap, "select count(*) from t"), 250)
        self.assertEqual(self.q(snap, "pragma integrity_check"), "ok"); self.assertEqual(self.q(snap, "pragma journal_mode"), "delete")
        h1 = cb._content_hash(snap); h2 = cb._content_hash(cb._snapshot_database(Config.DB_PATH)); wc.close()
        self.assertEqual(h1, h2, "snapshots of an unchanged database must be byte-identical")

    def test_snapshot_under_concurrent_writer(self):
        stop = [False]
        def writer():
            c = sqlite3.connect(Config.DB_PATH, timeout=30)
            while not stop[0]: c.execute("insert into t(v) values ('w')"); c.commit()
            c.close()
        th = threading.Thread(target=writer); th.start()
        try:
            for _ in range(15):
                s = cb._snapshot_database(Config.DB_PATH); self.assertEqual(self.q(s, "pragma integrity_check"), "ok")
        finally: stop[0] = True; th.join()


class SchedulingUnchanged(unittest.TestCase):
    """The scheduling / catch-up decision function is outside the backup-safety change; pin its behaviour."""
    def test_startup_plan(self):
        from datetime import datetime, timedelta
        plan = cb._compute_startup_plan; slot = cb._parse_daily_time("02:00")
        day = datetime(2026, 10, 18, 8, 0)                                    # 13:30 IST, daytime (naive UTC, like _now())
        delay, kind, nxt = plan(day, (day - timedelta(hours=5)).isoformat(), None, slot)
        self.assertEqual(kind, "slot"); self.assertAlmostEqual(delay, 12.5 * 3600, delta=1)
        self.assertEqual(cb._to_ist(nxt).strftime("%H:%M"), "02:00")
        night = datetime(2026, 10, 18, 17, 30)                                # 23:00 IST, inside the 20:00-07:00 catch-up window
        delay, kind, _n = plan(night, (night - timedelta(hours=40)).isoformat(), None, slot)
        self.assertEqual(kind, "catchup"); self.assertEqual(delay, float(cb.CATCHUP_DELAY_SECONDS))
        delay, kind, _n = plan(night, (night - timedelta(hours=40)).isoformat(), (night - timedelta(minutes=30)).isoformat(), slot)
        self.assertEqual(kind, "slot", "a catch-up already attempted this window must not re-arm")
        delay, kind, _n = plan(night, (night - timedelta(hours=5)).isoformat(), None, slot)
        self.assertEqual(kind, "slot", "a recent success needs no catch-up")


if __name__ == "__main__":
    unittest.main()
