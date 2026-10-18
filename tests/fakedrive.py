"""In-memory fake of the slice of the Google Drive v3 API that cloud_backup.py uses.
NEVER talks to the network. Tracks permanent deletes so tests can assert there are none."""
import hashlib, itertools, re, threading

FOLDER_MIME = "application/vnd.google-apps.folder"


class FakeHttpError(Exception):
    pass


def make_http_error(status):
    from googleapiclient.errors import HttpError
    import httplib2
    return HttpError(httplib2.Response({"status": str(status)}), b"{}")


class Req:
    def __init__(self, fn): self.fn = fn
    def execute(self, **kw): return self.fn()


class FakeFiles:
    def __init__(self, drive): self.d = drive

    def list(self, q=None, fields=None, pageSize=1000, pageToken=None, spaces=None):
        def run():
            with self.d.lock:
                self.d.calls["list"] += 1
                m = re.search(r"'([^']+)' in parents", q)
                parent = m.group(1)
                want_trashed = False if "trashed = false" in q else None
                name = None
                nm = re.match(r"name = '((?:\\.|[^'\\])*)'", q)
                if nm: name = re.sub(r"\\(.)", r"\1", nm.group(1))
                out = []
                for n in self.d.nodes.values():
                    if parent not in n["parents"]: continue
                    if want_trashed is False and n["trashed"]: continue
                    if name is not None and n["name"] != name: continue
                    if "mimeType = '%s'" % FOLDER_MIME in q and n["mimeType"] != FOLDER_MIME: continue
                    out.append({"id": n["id"], "name": n["name"], "mimeType": n["mimeType"],
                                "size": str(len(n["content"])) if n["mimeType"] != FOLDER_MIME else None,
                                "md5Checksum": hashlib.md5(n["content"]).hexdigest() if n["mimeType"] != FOLDER_MIME else None,
                                "createdTime": "2026-01-01T00:00:%09d" % n["seq"]})
                return {"files": out}
        return Req(run)

    def create(self, body=None, media_body=None, fields=None):
        def run():
            with self.d.lock:
                if not all(p in self.d.nodes or p == self.d.root for p in body.get("parents", [])):
                    raise make_http_error(404)
                nid = self.d.new_id()
                self.d.nodes[nid] = {"id": nid, "name": body["name"], "parents": list(body.get("parents", [])),
                                     "mimeType": body.get("mimeType", "application/octet-stream"), "trashed": False,
                                     "content": media_body.getbytes(0, media_body.size()) if media_body is not None else b"",
                                     "seq": next(self.d.seq)}
                self.d.calls["create_media" if media_body is not None else "create_folder"] += 1
                return {"id": nid}
        return Req(run)

    def update(self, fileId=None, body=None, media_body=None, fields=None):
        def run():
            with self.d.lock:
                n = self.d.nodes.get(fileId)
                if n is None: raise make_http_error(404)
                if media_body is not None:
                    n["content"] = media_body.getbytes(0, media_body.size()); self.d.calls["update_media"] += 1
                if body and "trashed" in body:
                    n["trashed"] = bool(body["trashed"]); self.d.calls["trash" if body["trashed"] else "untrash"] += 1
                return {"id": fileId}
        return Req(run)

    def delete(self, fileId=None):
        def run():
            with self.d.lock:
                self.d.calls["PERMANENT_DELETE"] += 1
                self.d.nodes.pop(fileId, None)
                return {}
        return Req(run)


class FakeService:
    def __init__(self, drive): self.d = drive
    def files(self): return FakeFiles(self.d)


class FakeDrive:
    def __init__(self):
        self.root = "ROOT"; self.nodes = {}; self.lock = threading.RLock(); self.seq = itertools.count(1)
        self._ids = itertools.count(1)
        self.calls = {k: 0 for k in ("list", "create_media", "create_folder", "update_media", "trash", "untrash", "PERMANENT_DELETE")}
    def new_id(self): return "F%05d" % next(self._ids)
    def service(self): return FakeService(self)
    def live_files(self): return [n for n in self.nodes.values() if n["mimeType"] != FOLDER_MIME and not n["trashed"]]
    def trashed_files(self): return [n for n in self.nodes.values() if n["mimeType"] != FOLDER_MIME and n["trashed"]]
    def snapshot_calls(self): return dict(self.calls)
