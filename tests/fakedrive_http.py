"""A local fake of the Google Drive v3 HTTP API (the slice cloud_backup.py uses), served over HTTP/1.1
with PERSISTENT connections, so the REAL googleapiclient / httplib2 / google-auth client code runs against it.
Never touches the network beyond 127.0.0.1."""
import hashlib, itertools, json, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

FOLDER = "application/vnd.google-apps.folder"


class Store:
    def __init__(self):
        self.lock = threading.RLock(); self.nodes = {}; self.seq = itertools.count(1); self.ids = itertools.count(1)
        self.latency = 0.0; self.sessions = {}; self.open_connections = 0; self.max_open_connections = 0; self.total_connections = 0
        self.calls = {"list": 0, "create": 0, "upload": 0, "patch": 0, "delete": 0}

    def new_id(self): return "F%05d" % next(self.ids)

    def add(self, name, parents, mime, content=b""):
        nid = self.new_id()
        self.nodes[nid] = {"id": nid, "name": name, "parents": list(parents), "mimeType": mime, "trashed": False, "content": content, "seq": next(self.seq)}
        return nid


def make_handler(store, port):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"        # keep-alive
        def log_message(self, *a): pass

        def setup(self):
            super().setup()
            with store.lock:
                store.open_connections += 1; store.total_connections += 1
                store.max_open_connections = max(store.max_open_connections, store.open_connections)

        def finish(self):
            try: super().finish()
            finally:
                with store.lock: store.open_connections -= 1

        def _send(self, code, payload=None, headers=None):
            body = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=UTF-8"); self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0)); return self.rfile.read(n) if n else b""

        def do_GET(self):
            url = urlparse(self.path); q = parse_qs(url.query)
            if url.path.endswith("/files") and "/upload" not in url.path:
                if store.latency: time.sleep(store.latency)          # slow responses keep all pool threads busy, like real Drive
                with store.lock:
                    store.calls["list"] += 1
                    expr = q.get("q", [""])[0]
                    parent = re.search(r"'([^']+)' in parents", expr).group(1)
                    name = None
                    nm = re.match(r"name = '((?:\\.|[^'\\])*)'", expr)
                    if nm: name = re.sub(r"\\(.)", r"\1", nm.group(1))
                    out = []
                    for n in store.nodes.values():
                        if parent not in n["parents"] or n["trashed"]: continue
                        if name is not None and n["name"] != name: continue
                        if f"mimeType = '{FOLDER}'" in expr and n["mimeType"] != FOLDER: continue
                        item = {"id": n["id"], "name": n["name"], "mimeType": n["mimeType"], "createdTime": "2026-01-01T00:00:%09d" % n["seq"]}
                        if n["mimeType"] != FOLDER:
                            item["size"] = str(len(n["content"])); item["md5Checksum"] = hashlib.md5(n["content"]).hexdigest()
                        out.append(item)
                return self._send(200, {"files": out})
            return self._send(404, {"error": {"code": 404, "message": "not found"}})

        def do_POST(self):
            url = urlparse(self.path); q = parse_qs(url.query); body = self._body()
            if url.path.endswith("/files") and "/upload" in url.path and q.get("uploadType") == ["resumable"]:
                meta = json.loads(body or b"{}")
                with store.lock:
                    sid = "S%d" % next(store.ids); store.sessions[sid] = ("create", meta)
                return self._send(200, {}, {"Location": f"http://127.0.0.1:{port}/session/{sid}"})
            if url.path.endswith("/files"):
                meta = json.loads(body or b"{}")
                with store.lock:
                    if not all(p in store.nodes or p == "ROOT" for p in meta.get("parents", [])):
                        return self._send(404, {"error": {"code": 404, "message": "parent not found"}})
                    store.calls["create"] += 1
                    nid = store.add(meta["name"], meta.get("parents", []), meta.get("mimeType", "application/octet-stream"))
                return self._send(200, {"id": nid})
            return self._send(404, {"error": {"code": 404}})

        def do_PUT(self):
            m = re.match(r"/session/(S\d+)", urlparse(self.path).path); body = self._body()
            if not m: return self._send(404, {})
            with store.lock:
                kind, meta = store.sessions.pop(m.group(1), (None, None))
                if kind == "create":
                    store.calls["upload"] += 1
                    nid = store.add(meta["name"], meta.get("parents", []), "application/octet-stream", body)
                elif kind == "update":
                    store.calls["upload"] += 1; nid = meta["id"]; store.nodes[nid]["content"] = body
                else:
                    return self._send(404, {})
            return self._send(200, {"id": nid})

        def do_PATCH(self):
            url = urlparse(self.path); q = parse_qs(url.query); body = self._body()
            m = re.search(r"/files/([^/?]+)$", url.path)
            if not m: return self._send(404, {})
            fid = m.group(1)
            with store.lock:
                node = store.nodes.get(fid)
                if node is None: return self._send(404, {"error": {"code": 404, "message": "File not found"}})
                if "/upload" in url.path and q.get("uploadType") == ["resumable"]:
                    sid = "S%d" % next(store.ids); store.sessions[sid] = ("update", {"id": fid})
                    return self._send(200, {}, {"Location": f"http://127.0.0.1:{port}/session/{sid}"})
                store.calls["patch"] += 1
                meta = json.loads(body or b"{}")
                if "trashed" in meta: node["trashed"] = bool(meta["trashed"])
            return self._send(200, {"id": fid})

        def do_DELETE(self):
            fid = urlparse(self.path).path.rsplit("/", 1)[-1]
            with store.lock:
                store.calls["delete"] += 1; store.nodes.pop(fid, None)
            self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
    return Handler


def start_server():
    store = Store()
    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: None)
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(store, port)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, store, port
