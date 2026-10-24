"""Image order and file names when sharing a car's designs.

Display order, share order and file-name order must be the same thing for any number of images:
  * server: _build_design_payload() sorts by (category rank, source position); uncategorised and
    stale-category designs go last, in source order;
  * browser: the SHARE-ORDER block of templates/index.html (run here under node, unchanged) names files by
    slot, pads to max(3, digits of the total), fetches with a concurrency limit but stores results by slot,
    and batches by consecutive ranges of the intended order.
A "phone" is simulated two ways: each batch's share sheet on its own, and every batch landing in one folder
/ gallery sorted by file name (the case that used to interleave batches). Nothing here touches data/.
"""
import json
import os
import re
import shutil
import subprocess
import unittest
from unittest import mock

from tests import support

ROOT = support.ROOT
HARNESS = os.path.join(ROOT, "tests", "share_order_harness.js")
INDEX_HTML = os.path.join(ROOT, "templates", "index.html")
COUNTS = (9, 10, 99, 100, 101, 150, 250)
CATEGORIES = ["Pearl", "Pearl Designer", "Pearl Deluxe", "Saka", "Ruby", "Napa Deluxe", "Napa Designer", "Zebra", "Opal"]


def run_node(scenarios):
    node = shutil.which("node")
    if not node:
        raise AssertionError("node is required for tests/test_share_order.py (the browser ordering code is executed, not just scanned)")
    done = subprocess.run([node, HARNESS, INDEX_HTML], input=json.dumps({"scenarios": scenarios}), capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError("harness failed:\n" + done.stderr)
    return json.loads(done.stdout)["results"]


def first_diff(expected, got):
    """First 1-based position where two id lists differ (None = identical)."""
    for position, (a, b) in enumerate(zip(expected, got), start=1):
        if a != b:
            return position, a, b
    return None if len(expected) == len(got) else (min(len(expected), len(got)) + 1, "length", "length")


def synthetic_car(n_designs, seed=7):
    """n designs for one car: 9 categories interleaved in source order, ~1 in 7 uncategorised, 2 with a category
    that no longer exists, and a few designs sharing one image (Bulk Match). Returns (children, categories, image_of)."""
    children, cat_of, image_of = [], {}, {}
    for i in range(n_designs):
        name = f"BALENO DESIGN {i:04d}"
        children.append({"design": name, "qty": 1 + i % 5})
        if i % 7 == 3:
            cat_of[name] = None
        elif i in (11, 12):
            cat_of[name] = "Deleted Category"
        else:
            cat_of[name] = CATEGORIES[(i * 5 + i // 9) % len(CATEGORIES)]
        image_of[name] = 900000 + (i if i % 25 != 24 else i - 1)       # every 25th design shares its neighbour's image
    return children, [{"name": c} for c in CATEGORIES], image_of


def build_payload(children, categories, cat_of_name, image_of):
    app_module, db = support.bootstrap()
    key = app_module._normalize_lookup_key
    cats = {key(n): c for n, c in cat_of_name.items() if c}
    maps = {key(n): {"image_id": image_of[n], "confidence": 1.0} for n in image_of}
    with mock.patch.object(db, "get_all_categories", return_value=categories), \
            mock.patch.object(db, "get_categories_for_stock_items", return_value=cats), \
            mock.patch.object(db, "get_mappings_for_stock_items", return_value=maps):
        return app_module._build_design_payload(children)


class ServerOrder(unittest.TestCase):
    def test_category_rank_then_source_position_uncategorised_last(self):
        children, categories, _ = synthetic_car(160)
        cat_of = {}
        for i, child in enumerate(children):
            cat_of[child["design"]] = None if i % 7 == 3 else ("Deleted Category" if i in (11, 12) else CATEGORIES[(i * 5 + i // 9) % 9])
        image_of = {c["design"]: 900000 + i for i, c in enumerate(children)}
        payload = build_payload(children, categories, cat_of, image_of)
        self.assertEqual(len(payload), 160)
        rank = {n: r for r, n in enumerate(CATEGORIES)}
        source_pos = {c["design"]: i for i, c in enumerate(children)}
        expected = sorted(children, key=lambda c: (rank.get(cat_of[c["design"]], len(CATEGORIES)), source_pos[c["design"]]))
        self.assertEqual([p["design"] for p in payload], [c["design"] for c in expected])
        # category groups are contiguous and in rank order, uncategorised + stale category are the tail
        seen = []
        for item in payload:
            group = rank.get(item["category"], len(CATEGORIES))
            if not seen or seen[-1] != group:
                seen.append(group)
        self.assertEqual(seen, sorted(set(seen)), "a category group was split or out of order")
        tail = [p for p in payload if p["category"] not in rank]
        self.assertEqual(payload[-len(tail):], tail)
        self.assertTrue(len(tail) >= 23 + 2)
        self.assertEqual([source_pos[p["design"]] for p in tail], sorted(source_pos[p["design"]] for p in tail))

    def test_order_does_not_depend_on_sort_stability_or_repeat_calls(self):
        children, categories, _ = synthetic_car(150)
        cat_of = {c["design"]: CATEGORIES[i % 9] if i % 4 else None for i, c in enumerate(children)}
        image_of = {c["design"]: 1000 + i for i, c in enumerate(children)}
        first = build_payload(children, categories, cat_of, image_of)
        second = build_payload(children, categories, cat_of, image_of)
        self.assertEqual(first, second)
        # reversing the source reverses the order INSIDE each category (source position is the tie-breaker)
        rev = build_payload(list(reversed(children)), categories, cat_of, image_of)
        for name in CATEGORIES:
            fwd = [p["design"] for p in first if p["category"] == name]
            bwd = [p["design"] for p in rev if p["category"] == name]
            self.assertEqual(fwd, list(reversed(bwd)))


class BrowserShareOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        children, categories, _ = synthetic_car(270)
        cat_of = {c["design"]: None if i % 7 == 3 else CATEGORIES[(i * 5 + i // 9) % 9] for i, c in enumerate(children)}
        image_of = {c["design"]: 900000 + (i if i % 25 != 24 else i - 1) for i, c in enumerate(children)}
        payload = build_payload(children, categories, cat_of, image_of)
        # what the page does: one card per design in payload order; share = unique image ids in that order
        seen, cls.ids = set(), []
        for item in payload:
            if item["image_id"] not in seen:
                seen.add(item["image_id"])
                cls.ids.append(f"img{item['image_id']}")

    def ids_for(self, n):
        return self.ids[:n]

    def check(self, result, with_failures):
        label = result["name"]
        expected = result["expected"]
        self.assertEqual(first_diff(expected, result["sentIds"]), None, f"{label}: shared order != intended order")
        self.assertEqual(first_diff(expected, result["sheetView"]), None, f"{label}: per-sheet view differs")
        self.assertEqual(first_diff(expected, result["folderView"]), None, f"{label}: one-folder view (sorted by name) differs")
        self.assertTrue(result["uniqueNames"], f"{label}: duplicate file names across batches")
        self.assertTrue(result["namesSortedEqualsSent"], f"{label}: names do not sort in sent order")
        self.assertGreaterEqual(result["width"], 3)
        self.assertEqual(result["width"], max(3, len(str(result["count"]))))
        self.assertTrue(all(size <= 10 for size in result["batchSizes"]), f"{label}: a batch exceeds 10 files")
        self.assertTrue(all(size == 10 for size in result["batchSizes"][:-1]), f"{label}: batches are not full consecutive ranges")
        if not with_failures:
            self.assertEqual(result["batches"], -(-result["count"] // 10))
        if result["count"] > 12:
            self.assertTrue(result["completionWasShuffled"], f"{label}: random delays did not shuffle completion (test has no teeth)")

    def test_counts_without_failures(self):
        results = run_node([{"name": str(n), "ids": self.ids_for(n), "seed": n} for n in COUNTS])
        self.assertEqual([r["count"] for r in results], list(COUNTS))
        for result in results:
            self.check(result, False)
            self.assertEqual(result["missing"], "")

    def test_counts_with_failures_keep_their_slots(self):
        scenarios = []
        for n in COUNTS[2:]:
            ids = self.ids_for(n)
            scenarios.append({"name": f"{n} fail first/middle/last", "ids": ids, "fail": [0, n // 2, n - 1], "seed": n + 1})
            scenarios.append({"name": f"{n} fail a whole batch", "ids": ids, "fail": list(range(10, 20)), "seed": n + 2})
        for result, scenario in zip(run_node(scenarios), scenarios):
            self.check(result, True)
            # a failed image keeps its slot: the others keep the number they would have had
            fail = scenario["fail"]
            self.assertEqual(len(result["sentIds"]), len(scenario["ids"]) - len(fail))
            width = result["width"]
            wanted = [f"image_{slot + 1:0{width}d}" for slot in range(len(scenario["ids"])) if slot not in fail]
            self.assertEqual([n.split(".")[0] for n in result["sentNames"]], wanted)
            self.assertEqual(result["missing"].count("image_") + (0 if " more" not in result["missing"] else int(re.search(r"and (\d+) more", result["missing"]).group(1))), len(fail))

    def test_all_failed_and_tiny_selections(self):
        results = run_node([
            {"name": "all fail", "ids": self.ids_for(12), "fail": list(range(12)), "seed": 3},
            {"name": "one", "ids": self.ids_for(1), "seed": 4},
            {"name": "mixed types", "ids": self.ids_for(25), "types": {self.ids[i]: ("image/png" if i % 3 == 0 else "image/webp") for i in range(25)}, "seed": 5},
        ])
        self.assertEqual(results[0]["sentIds"], [])
        self.assertEqual(results[1]["sentNames"], ["image_001.jpeg"])
        self.check(results[2], False)       # different extensions must not change the order

    def test_pad_width_follows_the_total_not_a_constant(self):
        ids = [f"x{i}" for i in range(1000)]
        results = run_node([{"name": "999", "ids": ids[:999], "seed": 1}, {"name": "1000", "ids": ids, "seed": 2}])
        self.assertEqual([r["width"] for r in results], [3, 4])
        self.assertEqual(results[0]["lastName"], "image_999.jpeg")
        self.assertEqual(results[1]["firstName"], "image_0001.jpeg")
        for result in results:
            self.check(result, False)

    def test_page_uses_the_shared_helpers_on_every_share_path(self):
        html = open(INDEX_HTML, encoding="utf-8").read()
        body = html[html.index("async function copySelectedImages"):]
        body = body[:body.index("async function copyModalImage")]
        self.assertIn("fetchInIntendedOrder(", body)
        self.assertNotIn("Promise.all(imageIds.map", body, "unbounded parallel fetch is back")
        self.assertEqual(len(re.findall(r"await navigator\.share\(", html)), 1, "a second share path appeared: route it through the SHARE-ORDER helpers")
        for call in re.findall(r"buildShareFiles\([^)]*\)", html.split("SHARE-ORDER-END")[1]):
            self.assertIn("slotTotal", call)
        self.assertNotIn("link.download = `image_${i + 1}`", html)
        self.assertNotIn("String(SHARE_BATCH_SIZE).length", html)


if __name__ == "__main__":
    unittest.main()
