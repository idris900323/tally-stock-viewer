"""User-activity tracking: what counts as a person acting, and what must never be recorded.
Activity classifies by Flask ENDPOINT NAME, so renaming an endpoint silently breaks it -- hence the name checks."""
import re
import unittest

from tests import support


class ActivityTracking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        support.restore_state(self.snapshot)
        with self.app_module._activity_lock:
            self.app_module._activity_last_touch.clear(); self.app_module._activity_last_collapsed.clear()
            self.app_module._activity_beacon_window.clear()
        self.dealer = support.client_for("dealer")
        self.dealer_id = support._state["users"]["dealer"]["id"]

    def rows(self):
        with self.db.get_connection() as conn:
            return [tuple(r) for r in conn.execute("select action, coalesce(detail,'') from user_activity where user_id=? order by id", (self.dealer_id,))]

    def last_active(self):
        with self.db.get_connection() as conn:
            return conn.execute("select last_active_at from users where id=?", (self.dealer_id,)).fetchone()[0]

    def test_polling_never_counts_as_activity(self):
        before_rows, before_active = self.rows(), self.last_active()
        for _ in range(30):
            self.assertEqual(self.dealer.get("/last_update").status_code, 200)
            self.dealer.get("/refresh_status"); self.dealer.get("/api/popup"); self.dealer.get("/api/banner")
        self.assertEqual(self.rows(), before_rows)
        self.assertEqual(self.last_active(), before_active)

    def test_opening_a_car_searching_sharing_and_reporting_are_recorded(self):
        self.dealer.get("/designs?car=ALTO"); self.dealer.get("/designs?car=ALTO")            # reopen collapses into one row
        self.dealer.get("/api/search_cars?q=al&page=1")
        self.dealer.post("/api/activity", json={"action": "share_image", "detail": "ALTO"})
        self.dealer.post("/api/activity", json={"action": "not_a_real_action"})                # ignored
        self.dealer.post("/api/report_item", json={"car": "ALTO", "car_key": "alto", "details": "x", "item": "ALTO 7D MAT BLACK"})
        actions = [a for a, _d in self.rows()]
        self.assertEqual(actions.count("view_car"), 1, "reopening the same car within the window is one row")
        for expected in ("search", "share_image", "report"):
            self.assertIn(expected, actions)
        self.assertNotIn("not_a_real_action", actions)
        self.assertIsNotNone(self.last_active())

    def test_admin_actions_are_recorded_for_the_admin(self):
        admin = support.client_for("admin_unlocked"); admin_id = support._state["users"]["admin"]["id"]
        admin.post("/admin/categories/add", json={"name": "ZZ ACT"})
        with self.db.get_connection() as conn:
            actions = [r[0] for r in conn.execute("select action from user_activity where user_id=?", (admin_id,))]
        self.assertIn("category_add", actions)

    def test_endpoint_names_the_tracker_depends_on_all_exist(self):
        endpoints = {r.endpoint for r in self.app_module.app.url_map.iter_rules()}
        named = set(self.app_module._ACTIVITY_COUNTED_GET_ENDPOINTS) | set(self.app_module._ACTIVITY_EXCLUDED_ENDPOINTS)
        import inspect
        source = inspect.getsource(self.app_module._activity_row_for_request)
        named |= set(re.findall(r'endpoint == "([\w.]+)"', source))
        self.assertEqual(sorted(n for n in named if n not in endpoints), [], "tracker refers to endpoint names that no longer exist")
        for poller in ("last_update", "refresh_status", "api_popup", "api_banner", "api_customer_reports", "mapping_stats"):
            self.assertNotIn(poller, self.app_module._ACTIVITY_COUNTED_GET_ENDPOINTS)


if __name__ == "__main__":
    unittest.main()
