"""Feeder intake + the data-version hash that drives the "Stock updated" notice."""
import copy
import json
import os
import threading
import time
import unittest

from tests import support
from tests.support import PAYLOAD, INTAKE_HEADERS


class DataVersion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        support.restore_state(self.snapshot)
        self.dealer = support.client_for("dealer")
        self.admin = support.client_for("admin_locked")

    def push(self, payload=PAYLOAD, headers=INTAKE_HEADERS):
        return support.client_for("anonymous").post("/admin/intake/sync_data", json=payload, headers=headers)

    def version(self, client=None):
        return (client or self.dealer).get("/last_update").get_json()["data_version"]

    # ---- intake basics
    def test_intake_requires_the_token_and_valid_json(self):
        anon = support.client_for("anonymous")
        self.assertEqual(anon.post("/admin/intake/sync_data", json=PAYLOAD).status_code, 403)
        self.assertEqual(anon.post("/admin/intake/sync_data", json=PAYLOAD, headers={"X-Intake-Token": "wrong"}).status_code, 403)
        self.assertEqual(anon.post("/admin/intake/sync_data", data="not json", headers=INTAKE_HEADERS).status_code, 400)
        bad = copy.deepcopy(PAYLOAD); bad["item_stock"] = [{"item_name": "x"}]
        self.assertEqual(self.push(bad).status_code, 400)

    # ---- version hash behaviour
    def test_identical_push_keeps_the_version_but_heartbeat_moves(self):
        before = self.dealer.get("/last_update").get_json(); time.sleep(0.05)
        self.assertEqual(self.push().status_code, 200)
        after = self.dealer.get("/last_update").get_json()
        self.assertEqual(before["data_version"], after["data_version"])
        self.assertNotEqual(before["timestamp"], after["timestamp"])

    def test_real_change_changes_the_version_and_reorder_does_not(self):
        base = self.version()
        changed = copy.deepcopy(PAYLOAD); changed["item_stock"][0]["qty"] += 1
        self.push(changed); bumped = self.version(); self.assertNotEqual(base, bumped)
        self.push(PAYLOAD); self.assertEqual(self.version(), base, "reverting the change returns the same version")
        shuffled = copy.deepcopy(PAYLOAD); shuffled["item_stock"].reverse()
        for row in shuffled["main_hierarchy"]:
            pass
        self.push(shuffled); self.assertEqual(self.version(), base, "Tally returning stock rows in another order is not a change")

    def test_admin_changes_bump_the_version_activity_does_not(self):
        base = self.version()
        self.dealer.post("/api/activity", json={"action": "copy_image", "detail": "x"})
        self.dealer.get("/designs?car=ALTO"); self.dealer.get("/last_update")
        self.assertEqual(self.version(), base, "activity tracking must never change the version")
        self.assertEqual(support.client_for("admin_unlocked").post("/admin/categories/add", json={"name": "ZZ TEST"}).status_code, 200)
        self.assertNotEqual(self.version(), base)

    def test_fields_visible_per_role(self):
        dealer = self.dealer.get("/last_update").get_json(); admin = self.admin.get("/last_update").get_json()
        self.assertIn("data_version", dealer); self.assertNotIn("admin_version", dealer); self.assertNotIn("feeder_outage", dealer)
        self.assertIn("admin_version", admin)

    def test_version_is_derived_from_stored_content_so_it_survives_a_restart(self):
        base = self.version()
        self.app_module._feeder_file_state.clear(); self.app_module._feeder_dirty = True
        self.db._dealer_dirty = self.db._accounts_dirty = True       # what a fresh process starts with
        self.assertEqual(self.version(), base)

    # ---- persisted last-seen time
    def test_last_seen_time_is_persisted_and_bad_files_fail_safe(self):
        self.push()
        path = self.app_module.FEEDER_LAST_SEEN_FILE
        self.assertAlmostEqual(json.load(open(path))["last_push_epoch"], time.time(), delta=30)
        load = self.app_module._load_feeder_last_seen
        self.assertIsNotNone(load())
        for content in ("{not json", "{}", '{"last_push_epoch": "soon"}', json.dumps({"last_push_epoch": time.time() + 86400}), ""):
            open(path, "w").write(content); self.assertIsNone(load(), content)
        os.remove(path); self.assertIsNone(load())
        json.dump({"last_push_epoch": time.time() - 3 * 3600}, open(path, "w"))
        age = (self.app_module.datetime.now() - load()).total_seconds()
        self.assertGreater(age, 3 * 3600 - 60, "a restart after an outage must keep the real (stale) age")

    # ---- intake lock
    def test_second_push_waits_then_gets_503_if_the_first_never_finishes(self):
        mod = self.app_module; old = mod._INTAKE_LOCK_TIMEOUT_SECONDS; mod._INTAKE_LOCK_TIMEOUT_SECONDS = 1
        mod._INTAKE_APPLY_LOCK.acquire()
        try:
            started = time.time(); response = self.push()
            self.assertEqual(response.status_code, 503); self.assertGreaterEqual(time.time() - started, 0.9)
            polls = time.time(); self.dealer.get("/last_update")
            self.assertLess(time.time() - polls, 1.0, "polls must never wait on the intake lock")
        finally:
            mod._INTAKE_APPLY_LOCK.release(); mod._INTAKE_LOCK_TIMEOUT_SECONDS = old
        self.assertEqual(self.push().status_code, 200, "the lock is not stuck afterwards")


if __name__ == "__main__":
    unittest.main()
