"""Characterization ("golden") tests: lock what the site does TODAY so an accidental change shows up.

 * the full Flask url_map (rule, methods, endpoint) -- activity tracking classifies by endpoint name
 * every fetch()/url_for()/ajax URL used by templates and static JS
 * every route, with every method, requested as anonymous / dealer / locked admin / unlocked admin
Update the goldens deliberately with:  python tests/check_all.py --update
"""
import glob
import os
import re
import unittest

from tests import support
from tests.support import ROOT, ROLES

SAMPLE_QUERY = {"designs": {"car": "ALTO"}, "search.search_cars": {"q": "a"}, "get_all_items_for_car": {"car": "ALTO"},
                "search.get_stock_items_for_car": {"car": "ALTO"}, "get_stock_image": {"stock_item": "ALTO 7D MAT BLACK"},
                "train": {"car": "ALTO"}, "get_current_mapping_image": {"stock_item": "ALTO 7D MAT BLACK"},
                "search_all_stock_items": {"q": "alto"}, "admin_user_activity_stats": {"days": "7"}}


def concrete_url(rule, ids):
    def repl(match):
        name = match.group(2)
        return str(ids.get(name, "x"))
    url = re.sub(r"<(?:[a-z_]+:)?(\w+)>", lambda m: str(ids.get(m.group(1), "shared.css" if m.group(1) == "filename" else "x")), rule.rule)
    return url


class UrlMapSnapshot(unittest.TestCase):
    def test_url_map_unchanged(self):
        app_module, _db = support.bootstrap()
        rules = sorted(f"{r.rule}|{','.join(sorted(r.methods - {'HEAD', 'OPTIONS'}))}|{r.endpoint}" for r in app_module.app.url_map.iter_rules())
        support.compare_golden(self, "url_map.json", {"rules": rules})


class TemplateAndScriptReferences(unittest.TestCase):
    FETCH = re.compile(r"""fetch\(\s*(['"`])((?:\\.|(?!\1).)*?)\1""", re.S)
    URL_FOR = re.compile(r"""url_for\(\s*['"]([\w.]+)['"]""")
    AJAX = re.compile(r"""\burl:\s*['"](/[^'"]*)['"]""")

    def scan(self):
        found, dynamic = {}, {}
        files = sorted(glob.glob(os.path.join(ROOT, "templates", "*.html")) + glob.glob(os.path.join(ROOT, "static", "*.js")))
        for path in files:
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            text = open(path, encoding="utf-8", errors="replace").read()
            urls = sorted({re.sub(r"\$\{[^}]*\}", "{}", m.group(2)).split("?")[0] for m in self.FETCH.finditer(text)})
            dynamic[rel] = len(re.findall(r"fetch\(\s*[A-Za-z_]", text))
            found[rel] = {"fetch": urls, "url_for": sorted(set(self.URL_FOR.findall(text))),
                          "ajax": sorted(set(self.AJAX.findall(text))), "fetch_with_variable_url": dynamic[rel]}
        return found

    def test_references_unchanged(self):
        support.compare_golden(self, "template_js_refs.json", self.scan())

    def test_every_url_for_endpoint_exists_and_every_static_fetch_resolves(self):
        app_module, _db = support.bootstrap()
        endpoints = {r.endpoint for r in app_module.app.url_map.iter_rules()}
        adapter = app_module.app.url_map.bind("localhost")
        missing_endpoints, unresolved = [], []
        for rel, refs in self.scan().items():
            for endpoint in refs["url_for"]:
                if endpoint not in endpoints:
                    missing_endpoints.append(f"{rel}: url_for('{endpoint}')")
            for url in refs["fetch"] + refs["ajax"]:
                if not url.startswith("/"):
                    continue
                path = url.replace("{}", "1")
                if not any(self._matches(adapter, path, method) for method in ("GET", "POST")):
                    unresolved.append(f"{rel}: {url}")
        self.assertEqual(missing_endpoints, [], "templates call url_for() on endpoints that don't exist")
        support.compare_golden(self, "unresolved_fetch_targets.json", {"unresolved": sorted(unresolved)})

    @staticmethod
    def _matches(adapter, path, method):
        from werkzeug.exceptions import HTTPException
        from werkzeug.routing import RequestRedirect
        try:
            adapter.match(path, method=method)
            return True
        except RequestRedirect:
            return True
        except HTTPException:
            return False


class RouteSnapshot(unittest.TestCase):
    def test_every_route_every_role(self):
        app_module, db = support.bootstrap()
        image_id = db.get_all_images()[0]["id"]
        ids = {"image_id": image_id, "user_id": support._state["users"]["dealer"]["id"], "report_id": 1, "version": 1,
               "name": db.get_all_categories()[0]["name"].replace(" ", "%20"), "filename": "shared.css"}
        snapshot = support.snapshot_state()
        results = {}
        try:
            with support.blocked_side_effects():
                for rule in sorted(app_module.app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
                    path = concrete_url(rule, ids)
                    for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                        for role in ROLES:
                            support.restore_state(snapshot)
                            client = support.client_for(role)
                            kwargs = {"query_string": SAMPLE_QUERY.get(rule.endpoint, {})}
                            if method != "GET":
                                kwargs["json"] = {}
                            response = client.open(path, method=method, **kwargs)
                            results[f"{role} {method} {rule.rule}"] = support.normalize_response(response, path)
                            response.close()   # send_file keeps the image open until the response is closed
        finally:
            support.restore_state(snapshot)
        self.assertGreater(len(results), 400)
        support.compare_golden(self, "routes.json", results)


if __name__ == "__main__":
    unittest.main()
