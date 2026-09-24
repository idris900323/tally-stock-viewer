"""Incremental, verified backup of mappings.db + S.S IMAGE + small JSON
caches to a Google Drive folder, authenticated as a real Google account
via OAuth (installed-app flow) -- not a Service Account. A Service
Account has zero personal Drive storage quota of its own: it can create
folders (metadata-only, no storage cost) but every real file upload fails
with a "Service Accounts do not have storage quota" 403 -- confirmed for
real, not just from docs. See build_drive_service() below and
GDRIVE_OAUTH_* in config.py for the one-time setup this requires.

Design mirrors existing conventions elsewhere in this codebase rather than
inventing new ones:
  - Change detection uses the same cheap (mtime, size) fingerprint idea as
    app.py's _file_fingerprint(), not a full-content hash on every run.
  - Backup file naming/pruning philosophy matches database.py's
    _backup_database_file() (timestamped, safe to call repeatedly).
  - Scheduling is a self-rescheduling threading.Timer, the same pattern as
    app.py's schedule_item_export().
  - "Not configured" is a clean no-op (log once, never raise, never
    schedule), the same fallback discipline as SYSTEM_ACCESS_TOKEN /
    ACCOUNTS_ACCESS_PASSWORD in config.py.

Manifest file (data/.cloud_backup_manifest.json) is the local source of
truth for what Drive is believed to hold: relative_path -> {drive_file_id,
size, mtime_ns, hash (optional, lazily filled)}. It is saved after every
single file operation succeeds, so an interrupted run never has to redo
work it already finished.
"""
import concurrent.futures
import hashlib
import json
import logging
import os
import random
import threading
import time
from datetime import datetime

from config import Config

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
IMAGE_ROOT = os.path.join(DATA_DIR, "S.S IMAGE")
MANIFEST_PATH = os.path.join(DATA_DIR, ".cloud_backup_manifest.json")

# Small JSON caches -- see cloud_backup investigation: these are cheap to
# include and complete the "restore everything exactly as it was" story,
# even though they're technically re-derivable from Tally on the next Full
# Refresh. Paths match the exact filenames app.py already uses.
INCLUDED_JSON_FILES = [
    "car_master.json",
    "main_hierarchy.json",
    "item stock list.auto.json",
]
INCLUDED_DB_FILE = "mappings.db"

DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Pacing: a floor between consecutive Drive API calls so a large run doesn't
# fire requests as fast as possible. Well under Drive's per-user limits.
MIN_CALL_INTERVAL_SECONDS = 0.15
MAX_RETRY_ATTEMPTS = 6
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 60.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503}

SYNC_LOCK = threading.Lock()  # same acquire-in-caller / release-in-finally convention as app.py's FULL_REFRESH_LOCK/EXPORT_LOCK
_timer = None
_last_call_time = 0.0
_last_call_lock = threading.Lock()

_status = {
    "running": False,
    "last_run": None,  # filled after first run: dict, see _build_run_summary()
}
_status_lock = threading.Lock()

# Scheduled-run skip tracking, surfaced on the System panel instead of only
# ever landing in the log file -- a skip (not yet authorized, or a sync
# already running at that exact moment) never touches _status["last_run"]
# (no run actually happened), so without this an admin checking the panel
# would just see whatever the previous real run's result was, with no hint
# that today's scheduled attempt never ran at all. In-memory only, same as
# _status above -- doesn't survive an app restart, which is an accepted
# limitation shared with the rest of this module's status tracking.
_SKIPPED_RUNS_CAPACITY = 20
_skipped_runs_lock = threading.Lock()
_skipped_runs = []  # most recent last; each: {"timestamp", "reason", "message"}


def _record_skipped_run(reason, message):
    with _skipped_runs_lock:
        _skipped_runs.append({
            "timestamp": datetime.now().isoformat(),
            "reason": reason,  # "not_authorized" | "already_running"
            "message": message,
        })
        if len(_skipped_runs) > _SKIPPED_RUNS_CAPACITY:
            del _skipped_runs[0]


def get_skipped_runs():
    with _skipped_runs_lock:
        return list(_skipped_runs)

# Cooperative cancellation for the "Stop Sync" button (Part 3): checked
# before starting each new file/folder operation, never mid-write, so a
# stopped run always leaves the manifest reflecting only genuinely
# completed items and is safe to resume on the next Sync Now / scheduled run.
_cancel_event = threading.Event()

# Live progress for the System panel (Part 4) -- same lightweight
# in-memory-dict + polling-endpoint shape as full_refresh_status /
# _BADGE_REGEN_STATUS in app.py. Updating this dict is cheap (no disk I/O),
# so it's safe to touch on every single file without slowing the sync down.
_PROGRESS_LOG_CAPACITY = 25  # most-recent action lines kept for the live log
_progress_lock = threading.Lock()
_progress = {
    "running": False,
    "stop_requested": False,
    "current": None,
    "total": 0,
    "processed": 0,
    "uploaded": 0,
    "updated": 0,
    "deleted": 0,
    "skipped": 0,
    "failed": 0,
    "log": [],
}


def _progress_reset(total, skipped):
    with _progress_lock:
        _progress.update({
            "running": True,
            "stop_requested": False,
            "current": None,
            "total": total,
            "processed": 0,
            "uploaded": 0,
            "updated": 0,
            "deleted": 0,
            "skipped": skipped,
            "failed": 0,
            "log": [],
        })


def _progress_log_line(line):
    with _progress_lock:
        _progress["current"] = line
        _progress["log"].append(line)
        if len(_progress["log"]) > _PROGRESS_LOG_CAPACITY:
            del _progress["log"][0]


def _progress_note(action, relative_path):
    _progress_log_line(f"{action}: {relative_path}")


def _progress_note_failure(relative_path, short_error):
    """Appends a second, outcome-bearing line after the item's initial
    "Uploading: X" / "Updating: X" / "Deleting: X" line, so the live log on
    the System panel reads as a real transcript instead of leaving a failed
    item looking identical to one still in flight. short_error is the same
    text also folded into the run summary's error string -- see
    _short_error_text() -- so the reason is visible without needing to open
    the log file."""
    _progress_log_line(f"Failed: {relative_path} -- {short_error}")


def _short_error_text(exc, limit=180):
    """A one-line, UI-safe rendering of a real exception -- collapsed
    whitespace (HttpError's str() often spans multiple lines) and capped
    length, prefixed with the exception's type since the message alone is
    sometimes just a bare code (e.g. some HttpError bodies)."""
    text = " ".join(str(exc).split()) or repr(exc)
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return f"{type(exc).__name__}: {text}"


def _progress_finish_item(counter_key):
    with _progress_lock:
        _progress["processed"] += 1
        _progress[counter_key] += 1


def _progress_stop():
    with _progress_lock:
        _progress["running"] = False
        _progress["current"] = None


def get_progress():
    with _progress_lock:
        return dict(_progress, log=list(_progress["log"]))


def request_stop():
    """Signals the in-progress sync, if any, to stop cleanly at its next
    safe checkpoint (see _cancel_event usage in run_sync below). No-op if
    nothing is currently running."""
    with _progress_lock:
        if not _progress["running"]:
            return {"stopping": False, "running": False}
        _progress["stop_requested"] = True
    _cancel_event.set()
    return {"stopping": True, "running": True}


def is_configured():
    """Only checks that the OAuth client secrets file and a token path/
    backup folder are set -- GDRIVE_OAUTH_TOKEN_PATH itself is allowed to
    not exist yet (that's exactly the first-run state build_drive_service()
    handles by opening the one-time browser consent flow)."""
    client_secrets_path = Config.GDRIVE_OAUTH_CLIENT_SECRETS_PATH
    token_path = Config.GDRIVE_OAUTH_TOKEN_PATH
    folder_id = Config.GDRIVE_BACKUP_FOLDER_ID
    if not client_secrets_path or not token_path or not folder_id:
        return False
    if not os.path.isfile(client_secrets_path):
        logger.warning("GDRIVE_OAUTH_CLIENT_SECRETS_PATH is set but file does not exist: %s", client_secrets_path)
        return False
    return True


def _log_not_configured_once():
    logger.info(
        "Cloud backup is not configured (GDRIVE_OAUTH_CLIENT_SECRETS_PATH / "
        "GDRIVE_OAUTH_TOKEN_PATH / GDRIVE_BACKUP_FOLDER_ID missing or invalid) -- "
        "skipping, feature disabled."
    )


def get_status():
    with _status_lock:
        snapshot = dict(_status)
        snapshot["last_run"] = dict(_status["last_run"]) if _status["last_run"] else None
    snapshot["configured"] = is_configured()
    snapshot["progress"] = get_progress()
    snapshot["skipped_runs"] = get_skipped_runs()
    return snapshot


# ---------------------------------------------------------------------------
# File enumeration (Part 1 include list)
# ---------------------------------------------------------------------------

def _relative_path(full_path):
    return os.path.relpath(full_path, DATA_DIR).replace("\\", "/")


def _iter_included_files():
    """Yields (relative_path, absolute_path) for every file this backup
    includes. relative_path uses forward slashes and is relative to data/,
    e.g. "mappings.db" or "S.S IMAGE/7'D MAT/0.jpg"."""
    db_path = Config.DB_PATH if os.path.isabs(Config.DB_PATH) else os.path.join(BASE_DIR, Config.DB_PATH)
    if os.path.isfile(db_path):
        yield INCLUDED_DB_FILE, db_path

    for name in INCLUDED_JSON_FILES:
        full_path = os.path.join(DATA_DIR, name)
        if os.path.isfile(full_path):
            yield name, full_path

    if os.path.isdir(IMAGE_ROOT):
        for root, _dirs, files in os.walk(IMAGE_ROOT):
            for filename in files:
                full_path = os.path.join(root, filename)
                if not os.path.isfile(full_path):
                    continue
                yield _relative_path(full_path), full_path


def _file_stat_signature(full_path):
    stats = os.stat(full_path)
    return stats.st_size, stats.st_mtime_ns


def _content_hash(full_path):
    hasher = hashlib.sha256()
    with open(full_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _empty_manifest():
    return {"version": 1, "files": {}, "folders": {}}


def load_manifest():
    if not os.path.isfile(MANIFEST_PATH):
        return _empty_manifest()
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return _empty_manifest()
        data.setdefault("files", {})
        data.setdefault("folders", {})
        return data
    except Exception:
        logger.exception("Failed to load cloud backup manifest, starting fresh: %s", MANIFEST_PATH)
        return _empty_manifest()


def save_manifest(manifest):
    """Atomic write (temp file + os.replace) so a crash mid-write never
    corrupts the manifest that already-completed file operations rely on."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = f"{MANIFEST_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, MANIFEST_PATH)


# ---------------------------------------------------------------------------
# Change classification (Part 3)
# ---------------------------------------------------------------------------

def classify_changes(manifest, current_files):
    """current_files: dict of relative_path -> absolute_path (from
    _iter_included_files, materialized so it can be scanned twice).

    Returns (new_paths, changed_paths, deleted_paths, unchanged_count,
    computed_hashes). Only touches disk for size+mtime (cheap stat), except
    for files whose size is unchanged but mtime differs -- an ambiguous case
    (e.g. a benign touch/copy that preserved size) where a real content hash
    decides whether it's actually a re-upload-worthy change, rather than
    trusting mtime alone or, worse, hashing every file on every run.

    When an ambiguous file turns out unchanged, its manifest entry's
    mtime_ns/hash are updated in place immediately (caller saves the
    manifest) so the *same* touched-but-identical file doesn't get re-hashed
    on every future run forever. When it turns out genuinely changed, the
    hash is returned in computed_hashes so the upload step can persist it
    without hashing the (possibly large) file a second time."""
    manifest_files = manifest.get("files", {})
    new_paths = []
    changed_paths = []
    unchanged_count = 0
    computed_hashes = {}

    for relative_path, full_path in current_files.items():
        try:
            size, mtime_ns = _file_stat_signature(full_path)
        except OSError:
            logger.warning("Skipping unreadable file during classification: %s", full_path)
            continue

        entry = manifest_files.get(relative_path)
        if entry is None:
            new_paths.append(relative_path)
            continue

        if entry.get("size") != size:
            changed_paths.append(relative_path)
            continue

        if entry.get("mtime_ns") == mtime_ns:
            unchanged_count += 1
            continue

        # Ambiguous: same size, different mtime. Fall back to a real hash
        # instead of assuming either way.
        current_hash = _content_hash(full_path)
        if entry.get("hash") == current_hash:
            unchanged_count += 1
            entry["mtime_ns"] = mtime_ns
            entry["hash"] = current_hash
        else:
            changed_paths.append(relative_path)
            computed_hashes[relative_path] = current_hash

    deleted_paths = [rel for rel in manifest_files if rel not in current_files]
    return new_paths, changed_paths, deleted_paths, unchanged_count, computed_hashes


# ---------------------------------------------------------------------------
# Drive API plumbing (rate-limit aware)
# ---------------------------------------------------------------------------

class NotAuthorizedError(Exception):
    """Raised by build_drive_service() when there's no usable OAuth token.

    Deliberately never triggers a browser flow itself -- that used to live
    inline here, and a closed/abandoned browser tab during a background
    Sync Now left the sync thread frozen forever inside the unbounded
    wait for the OAuth callback (SYNC_LOCK held, Stop Sync unreachable
    since the thread never got as far as its cancellation checkpoints).
    The one-time authorization now lives entirely in authorize() below,
    completely separate from SYNC_LOCK and run_sync()."""


def _load_saved_credentials():
    """Loads a previously-saved token if present and parseable. No network
    calls, no browser -- just a local file read, so this is always fast
    and safe to call from anywhere (status checks included)."""
    token_path = Config.GDRIVE_OAUTH_TOKEN_PATH
    if not token_path or not os.path.isfile(token_path):
        return None
    from google.oauth2.credentials import Credentials

    try:
        return Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception:
        logger.warning("Cloud backup: OAuth token file at %s is unreadable", token_path)
        return None


def _save_credentials(credentials):
    token_path = Config.GDRIVE_OAUTH_TOKEN_PATH
    os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
    with open(token_path, "w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())


def is_authorized():
    """No network calls -- true if there's a saved token that's either
    still valid or has a refresh_token to try. Doesn't guarantee a refresh
    will actually succeed (e.g. access was revoked) -- that surfaces as a
    normal sync failure through the existing error-logging path, same as
    any other Drive API error."""
    credentials = _load_saved_credentials()
    if credentials is None:
        return False
    return bool(credentials.valid or credentials.refresh_token)


def build_drive_service():
    """Authenticates as the actual dedicated Google account via OAuth,
    which has normal 15GB personal Drive storage -- unlike a Service
    Account (see module docstring). Only ever loads a previously-saved
    token and, if it's expired, refreshes it via the refresh token (a
    bounded network call, not a browser interaction) -- it NEVER opens a
    browser itself. If there's no usable token at all, raises
    NotAuthorizedError immediately instead of attempting the one-time
    consent flow inline -- see authorize() for that, and run_sync()'s
    NotAuthorizedError handling for how a sync fails fast and cleanly
    when this happens."""
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    credentials = _load_saved_credentials()
    if credentials is None:
        raise NotAuthorizedError(
            "Google Drive is not authorized yet -- click 'Authorize Google Drive' on the System panel first."
        )

    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())  # bounded (120s default), not an indefinite wait
            _save_credentials(credentials)
        else:
            raise NotAuthorizedError(
                "Google Drive authorization is no longer valid and can't be refreshed -- "
                "click 'Authorize Google Drive' again."
            )

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


# ---------------------------------------------------------------------------
# One-time authorization (dedicated action, separate from SYNC_LOCK/run_sync)
# ---------------------------------------------------------------------------

AUTH_LOCK = threading.Lock()  # same acquire-in-caller/release-in-finally convention as SYNC_LOCK
AUTHORIZE_TIMEOUT_SECONDS = 180

_auth_status_lock = threading.Lock()
_auth_status = {
    "in_progress": False,
    "last_result": None,  # {"ok": bool, "error": str|None, "finished_at": iso}
}


def get_auth_status():
    with _auth_status_lock:
        snapshot = dict(_auth_status)
        snapshot["last_result"] = dict(_auth_status["last_result"]) if _auth_status["last_result"] else None
    snapshot["authorized"] = is_authorized()
    snapshot["configured"] = is_configured()
    return snapshot


def authorize(timeout_seconds=AUTHORIZE_TIMEOUT_SECONDS):
    """Dedicated, standalone one-time OAuth consent flow -- the only place
    in this module that opens a browser. Never touches SYNC_LOCK and is
    never called from run_sync()/build_drive_service(), so an abandoned
    browser tab here can never block a sync or leave that lock stuck.

    Caller is responsible for holding AUTH_LOCK for the duration of this
    call (same convention as SYNC_LOCK -- see
    system_cloud_backup_authorize() in app.py), which just prevents two
    concurrent authorize() attempts, not sync/authorize interference.

    Hard-times-out after timeout_seconds (default 3 minutes): if consent
    isn't completed in time -- tab closed, ignored, whatever -- the local
    callback server stops listening on its own (google_auth_oauthlib's
    built-in timeout_seconds support) and this returns a clean
    {"ok": False, "error": "...timed out..."} result instead of hanging
    forever, with the listening socket properly closed either way."""
    from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError

    with _auth_status_lock:
        _auth_status["in_progress"] = True

    result = {"ok": False, "error": "Unknown error"}
    try:
        client_secrets_path = Config.GDRIVE_OAUTH_CLIENT_SECRETS_PATH
        if not client_secrets_path or not os.path.isfile(client_secrets_path):
            result = {"ok": False, "error": "GDRIVE_OAUTH_CLIENT_SECRETS_PATH is not set or the file does not exist."}
            return result

        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
        logger.info("Cloud backup: starting authorization (opening browser, %ss timeout)", timeout_seconds)
        try:
            credentials = flow.run_local_server(port=0, timeout_seconds=timeout_seconds)
        except WSGITimeoutError:
            logger.warning(
                "Cloud backup: authorization timed out after %ss (browser tab closed or never completed)",
                timeout_seconds,
            )
            result = {"ok": False, "error": "Authorization timed out -- try again."}
            return result
        except Exception as exc:
            logger.exception("Cloud backup: authorization attempt failed")
            result = {"ok": False, "error": f"Authorization failed: {_short_error_text(exc)}"}
            return result

        _save_credentials(credentials)
        logger.info("Cloud backup: authorization completed successfully")
        result = {"ok": True}
        return result
    finally:
        with _auth_status_lock:
            _auth_status["in_progress"] = False
            _auth_status["last_result"] = dict(result, finished_at=datetime.now().isoformat())


def _pace():
    global _last_call_time
    with _last_call_lock:
        now = time.monotonic()
        wait = MIN_CALL_INTERVAL_SECONDS - (now - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.monotonic()


def call_with_backoff(request_factory, **execute_kwargs):
    """request_factory: zero-arg callable returning a fresh googleapiclient
    request object (must be fresh per attempt -- request objects are
    single-use). Retries with exponential backoff + jitter specifically for
    429/5xx responses; anything else raises immediately."""
    from googleapiclient.errors import HttpError

    delay = RETRY_BASE_DELAY_SECONDS
    last_error = None
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        _pace()
        try:
            return request_factory().execute(**execute_kwargs)
        except HttpError as exc:
            status = exc.resp.status if getattr(exc, "resp", None) is not None else None
            last_error = exc
            if status in RETRYABLE_STATUS_CODES and attempt < MAX_RETRY_ATTEMPTS:
                sleep_for = min(delay, RETRY_MAX_DELAY_SECONDS) + random.uniform(0, 0.5)
                logger.warning(
                    "Drive API call failed with status %s (attempt %s/%s) -- retrying in %.1fs",
                    status, attempt, MAX_RETRY_ATTEMPTS, sleep_for,
                )
                time.sleep(sleep_for)
                delay *= 2
                continue
            raise
    raise last_error


def _escape_drive_query_value(value):
    """Drive query string literals are single-quoted; a literal backslash or
    single quote inside must be backslash-escaped (per Drive API query
    syntax) -- folder names here can contain either (e.g. "7'D MAT")."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _get_or_create_folder(service, manifest, relative_dir):
    """relative_dir uses forward slashes, e.g. "S.S IMAGE/7'D MAT", or ""
    for the backup root itself. Mirrors the local data/ structure inside
    Drive (Part 3.3) so the backup is human-browsable, not a flat dump.
    Folder IDs are cached in manifest["folders"] so repeat runs never
    re-query Drive for folders they already know about.

    A cache hit is trusted without a verifying round-trip (that would cost
    an extra Drive call per folder on every run for a case that's normally
    never true) -- staleness is instead caught where it actually surfaces:
    a 404 from the create call below when this folder's contents turn out
    to have a since-deleted parent. See _create_folder_self_healing()."""
    if relative_dir in ("", "."):
        return Config.GDRIVE_BACKUP_FOLDER_ID

    folders = manifest.setdefault("folders", {})
    cached = folders.get(relative_dir)
    if cached:
        return cached

    parent_dir, folder_name = os.path.split(relative_dir)
    parent_id = _get_or_create_folder(service, manifest, parent_dir)

    query = (
        f"name = '{_escape_drive_query_value(folder_name)}' and '{parent_id}' in parents "
        f"and mimeType = '{DRIVE_FOLDER_MIME_TYPE}' and trashed = false"
    )
    existing = call_with_backoff(
        lambda: service.files().list(q=query, fields="files(id, name)", pageSize=1, spaces="drive")
    )
    matches = existing.get("files", [])
    if matches:
        folder_id = matches[0]["id"]
    else:
        folder_id = _create_folder_self_healing(service, manifest, relative_dir, parent_dir, parent_id, folder_name)
        logger.info("Created Drive folder for %s", relative_dir)

    folders[relative_dir] = folder_id
    save_manifest(manifest)
    return folder_id


def _create_folder_self_healing(service, manifest, relative_dir, parent_dir, parent_id, folder_name, allow_recovery=True):
    """Creates folder_name under parent_id. Drive rejects a create whose
    parents=[id] no longer exists with a 404 -- the exact shape of the bug
    this fixes (manifest["folders"][parent_dir] cached an id for a folder
    that was since deleted directly on Drive, e.g. by hand). On that
    specific failure, the stale cache entry is dropped and the parent is
    resolved fresh (which may itself recurse into the same recovery one
    level further up, if the drift goes deeper than one folder) before
    retrying once -- instead of aborting the whole run for one item."""
    from googleapiclient.errors import HttpError

    metadata = {"name": folder_name, "mimeType": DRIVE_FOLDER_MIME_TYPE, "parents": [parent_id]}
    try:
        created = call_with_backoff(lambda: service.files().create(body=metadata, fields="id"))
    except HttpError as exc:
        if allow_recovery and getattr(exc.resp, "status", None) == 404 and parent_dir not in ("", "."):
            logger.warning(
                "Cloud backup: manifest-cached folder id for %r is stale (404 from Drive) -- "
                "dropping it and recreating before retrying %r",
                parent_dir, relative_dir,
            )
            manifest.get("folders", {}).pop(parent_dir, None)
            save_manifest(manifest)
            fresh_parent_id = _get_or_create_folder(service, manifest, parent_dir)
            return _create_folder_self_healing(
                service, manifest, relative_dir, parent_dir, fresh_parent_id, folder_name, allow_recovery=False
            )
        raise
    return created["id"]


def _update_existing_file(service, drive_file_id, full_path):
    from googleapiclient.http import MediaFileUpload

    call_with_backoff(
        lambda: service.files().update(
            fileId=drive_file_id, media_body=MediaFileUpload(full_path, resumable=True)
        )
    )
    return drive_file_id


def _create_file_self_healing(service, manifest, relative_path, parent_dir, parent_id, full_path, allow_recovery=True):
    """Mirrors _create_folder_self_healing() for the file-create call: if
    parent_id is a manifest-cached folder id that Drive no longer recognizes
    (404 -- the parent folder was deleted directly on Drive after being
    cached, without any child folder create along the way to notice it
    sooner), drop that cache entry, resolve the parent fresh, and retry once."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    metadata = {"name": os.path.basename(relative_path), "parents": [parent_id]}
    try:
        created = call_with_backoff(
            lambda: service.files().create(
                body=metadata, media_body=MediaFileUpload(full_path, resumable=True), fields="id"
            )
        )
    except HttpError as exc:
        if allow_recovery and getattr(exc.resp, "status", None) == 404 and parent_dir not in ("", "."):
            logger.warning(
                "Cloud backup: manifest-cached folder id for %r is stale (404 from Drive) -- "
                "dropping it and recreating before retrying upload of %s",
                parent_dir, relative_path,
            )
            manifest.get("folders", {}).pop(parent_dir, None)
            save_manifest(manifest)
            fresh_parent_id = _get_or_create_folder(service, manifest, parent_dir)
            return _create_file_self_healing(
                service, manifest, relative_path, parent_dir, fresh_parent_id, full_path, allow_recovery=False
            )
        raise
    return created["id"]


def _upload_or_update_file(service, manifest, relative_path, full_path, content_hash=None):
    from googleapiclient.errors import HttpError

    parent_dir = os.path.dirname(relative_path)
    parent_id = _get_or_create_folder(service, manifest, parent_dir)

    # A fresh MediaFileUpload per attempt (not shared across retries) --
    # the underlying file stream shouldn't be reused after a failed attempt.
    entry = manifest["files"].get(relative_path)
    drive_file_id = None
    if entry and entry.get("drive_file_id"):
        try:
            drive_file_id = _update_existing_file(service, entry["drive_file_id"], full_path)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) != 404:
                raise
            # Stale manifest-cached file id -- e.g. the file was deleted (or
            # replaced) directly on Drive since the manifest last saw it.
            # Fall through to create a fresh file below instead of failing
            # this item outright (Part 1: self-healing against state drift).
            logger.warning(
                "Cloud backup: manifest-cached file id for %s is stale (404 from Drive) -- "
                "recreating instead of updating",
                relative_path,
            )

    if drive_file_id is None:
        drive_file_id = _create_file_self_healing(service, manifest, relative_path, parent_dir, parent_id, full_path)

    size, mtime_ns = _file_stat_signature(full_path)
    new_entry = {"drive_file_id": drive_file_id, "size": size, "mtime_ns": mtime_ns}
    if content_hash is not None:
        # Only ever set for files that have hit the ambiguous same-size/
        # different-mtime path in classify_changes -- lets a future touch
        # of this same file resolve from a stat alone next time round,
        # rather than plain files getting a hash field they'll never need.
        new_entry["hash"] = content_hash
    manifest["files"][relative_path] = new_entry
    save_manifest(manifest)


def _delete_file(service, manifest, relative_path):
    from googleapiclient.errors import HttpError

    entry = manifest["files"].get(relative_path)
    if entry and entry.get("drive_file_id"):
        try:
            call_with_backoff(lambda: service.files().delete(fileId=entry["drive_file_id"]))
        except HttpError as exc:
            if getattr(exc.resp, "status", None) != 404:
                raise
            # already gone on Drive's side -- fine, just drop it locally too
    del manifest["files"][relative_path]
    save_manifest(manifest)


# ---------------------------------------------------------------------------
# Verification (Part 5)
# ---------------------------------------------------------------------------

# Bounded concurrency for the verification listing below -- enough to keep
# several folder-listing calls' network round-trips overlapping instead of
# sitting idle between them, while _pace()'s own MIN_CALL_INTERVAL_SECONDS
# floor (unchanged, still respected by every single call) keeps the actual
# dispatch rate well under Drive's per-user limits regardless of thread count.
LIST_CONCURRENCY = 15

_thread_local = threading.local()


def _get_thread_local_drive_service():
    """A fresh googleapiclient service -- and therefore a fresh, unshared
    httplib2 transport -- per thread, used only by the concurrent
    verification listing below. Necessary because googleapiclient's http
    transport is NOT thread-safe: sharing one `service` object across
    threads was confirmed for real to corrupt the SSL connection
    (ssl.SSLError: DECRYPTION_FAILED_OR_BAD_RECORD_MAC), not just serialize
    or slow down. Cheap to build -- no network call, since cache_discovery
    is off but well-known APIs like Drive v3 ship a bundled static
    discovery doc -- and reuses the same already-valid/refreshed
    credentials already loaded from disk this run, so no extra OAuth
    traffic per thread."""
    service = getattr(_thread_local, "drive_service", None)
    if service is None:
        from googleapiclient.discovery import build

        credentials = _load_saved_credentials()
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        _thread_local.drive_service = service
    return service


def _list_one_folder(folder_id):
    """Lists every direct child of one folder (all pages). Returns
    (file_count, total_size, subfolder_ids) for just this folder's
    immediate children -- recursion is orchestrated by the caller so
    sibling folders can be dispatched concurrently instead of one at a
    time. Uses a thread-local service (see above), still through
    call_with_backoff for the same per-call retry/backoff behavior as
    every other Drive API call in this module."""
    service = _get_thread_local_drive_service()
    file_count = 0
    total_size = 0
    subfolder_ids = []
    page_token = None
    while True:
        response = call_with_backoff(
            lambda: service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, mimeType, size)",
                pageSize=1000,
                pageToken=page_token,
                spaces="drive",
            )
        )
        for item in response.get("files", []):
            if item.get("mimeType") == DRIVE_FOLDER_MIME_TYPE:
                subfolder_ids.append(item["id"])
            else:
                file_count += 1
                total_size += int(item.get("size") or 0)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return file_count, total_size, subfolder_ids


def _list_drive_files_recursive(service, root_folder_id):
    """Real files.list against the target folder, walked recursively and
    CONCURRENTLY (bounded to LIST_CONCURRENCY folders in flight at once).
    Every folder is still listed exactly once and every file still counted
    exactly once -- same guarantee as a sequential walk, only the dispatch
    is concurrent, not what gets checked. Returns (file_count,
    total_size_bytes). This is the ground truth Part 5 compares the
    manifest's claims against.

    `service` is accepted for call-site compatibility with the rest of this
    module (the apply phase's single-threaded calls all share it safely)
    but isn't used directly here -- each worker thread builds its own via
    _get_thread_local_drive_service() instead (see why above)."""
    file_count = 0
    total_size = 0
    pending_folder_ids = {root_folder_id}
    in_flight = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=LIST_CONCURRENCY) as pool:
        while pending_folder_ids or in_flight:
            while pending_folder_ids and len(in_flight) < LIST_CONCURRENCY:
                folder_id = pending_folder_ids.pop()
                future = pool.submit(_list_one_folder, folder_id)
                in_flight[future] = folder_id

            done, _pending = concurrent.futures.wait(in_flight.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                in_flight.pop(future)
                folder_file_count, folder_size, subfolder_ids = future.result()
                file_count += folder_file_count
                total_size += folder_size
                pending_folder_ids.update(subfolder_ids)

    return file_count, total_size


def _verify(service, manifest):
    manifest_files = manifest.get("files", {})
    manifest_count = len(manifest_files)
    manifest_size = sum(entry.get("size", 0) for entry in manifest_files.values())

    drive_count, drive_size = _list_drive_files_recursive(service, Config.GDRIVE_BACKUP_FOLDER_ID)

    ok = (drive_count == manifest_count) and (drive_size == manifest_size)
    discrepancy = None
    if not ok:
        discrepancy = (
            f"Manifest claims {manifest_count} file(s) / {manifest_size} bytes, "
            f"but Drive actually has {drive_count} file(s) / {drive_size} bytes."
        )
        logger.warning("Cloud backup verification MISMATCH: %s", discrepancy)
    else:
        logger.info("Cloud backup verification OK: %s file(s), %s bytes", drive_count, drive_size)

    return {
        "ok": ok,
        "manifest_file_count": manifest_count,
        "manifest_total_size_bytes": manifest_size,
        "drive_file_count": drive_count,
        "drive_total_size_bytes": drive_size,
        "discrepancy": discrepancy,
    }


# ---------------------------------------------------------------------------
# Sync run (Parts 3-5)
# ---------------------------------------------------------------------------

def _build_run_summary(started_at, status, added=0, updated=0, deleted=0, skipped=0, error=None, verification=None, phase_timing=None):
    return {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "status": status,  # "success" | "partial" | "failed" | "stopped" | "not_authorized"
        "added": added,
        "updated": updated,
        "deleted": deleted,
        "skipped": skipped,
        "error": error,
        "verification": verification,
        "phase_timing": phase_timing,
    }


def run_sync(triggered_by="schedule"):
    """Runs one full incremental sync. Safe to call from either the
    scheduled timer or the System panel's manual "Sync Now" button.

    Caller is responsible for holding SYNC_LOCK for the duration of this
    call (same convention as app.py's run_full_refresh_job() and
    FULL_REFRESH_LOCK) -- see schedule()'s _job below and
    system_cloud_backup_sync_now() in app.py for the two call sites. This
    keeps "is a sync already running" a single atomic acquire at the call
    site, rather than a check-then-act race between two triggers (a second
    tab, a race on page refresh, a scheduled run landing mid-manual-run).

    Cooperative cancellation: checks _cancel_event before starting each new
    file/folder operation (never mid-write) and stops cleanly at the next
    one if it's set, so a stopped run's manifest reflects only genuinely
    completed items and next run resumes the rest normally."""
    if not is_configured():
        _log_not_configured_once()
        return {"configured": False}

    started_at = datetime.now().isoformat()
    _cancel_event.clear()
    with _status_lock:
        _status["running"] = True
    with _progress_lock:
        # Set True here, not just inside _progress_reset() below -- that
        # call happens only after classify_changes()/save_manifest() have
        # already run, which for a large S.S IMAGE folder (thousands of
        # files to stat) can take real seconds. Without this, a Stop Sync
        # click during that enumeration window would find request_stop()
        # still seeing "running": False and silently drop the request.
        _progress["running"] = True
        _progress["stop_requested"] = False

    added = updated = deleted_count = 0
    errors = []
    cancelled = False
    verification = None
    try:
        logger.info("Cloud backup sync starting (triggered_by=%s)", triggered_by)
        service = build_drive_service()
        manifest = load_manifest()

        # Phase timing (Part 7): logged separately so a slow run's actual
        # bottleneck is visible without guessing -- change detection is
        # local disk I/O (stat every included file), the apply phase is
        # real Drive API calls for only what actually changed, verification
        # is a fresh recursive Drive listing regardless of how few files
        # changed. These can have very different costs at real scale.
        phase_started = time.monotonic()

        current_files = dict(_iter_included_files())
        new_paths, changed_paths, deleted_paths, unchanged_count, computed_hashes = classify_changes(
            manifest, current_files
        )
        # Persists any in-place mtime_ns/hash fixes classify_changes made
        # for ambiguous-but-actually-unchanged files, even if nothing below
        # ends up needing to run (e.g. every other file was also unchanged).
        save_manifest(manifest)

        change_detection_seconds = time.monotonic() - phase_started
        logger.info(
            "Cloud backup: %s new, %s changed, %s deleted, %s unchanged (change detection took %.2fs)",
            len(new_paths), len(changed_paths), len(deleted_paths), unchanged_count, change_detection_seconds,
        )
        _progress_reset(len(deleted_paths) + len(new_paths) + len(changed_paths), unchanged_count)

        phase_started = time.monotonic()

        for relative_path in deleted_paths:
            if _cancel_event.is_set():
                cancelled = True
                break
            _progress_note("Deleting", relative_path)
            try:
                _delete_file(service, manifest, relative_path)
                deleted_count += 1
                _progress_finish_item("deleted")
            except Exception as exc:
                logger.exception("Cloud backup: failed to delete %s from Drive", relative_path)
                short_error = _short_error_text(exc)
                errors.append(f"delete failed: {relative_path} ({short_error})")
                _progress_note_failure(relative_path, short_error)
                _progress_finish_item("failed")

        for relative_path in new_paths:
            if cancelled:
                break
            if _cancel_event.is_set():
                cancelled = True
                break
            _progress_note("Uploading", relative_path)
            try:
                _upload_or_update_file(service, manifest, relative_path, current_files[relative_path])
                added += 1
                _progress_finish_item("uploaded")
            except Exception as exc:
                logger.exception("Cloud backup: failed to upload %s", relative_path)
                short_error = _short_error_text(exc)
                errors.append(f"upload failed: {relative_path} ({short_error})")
                _progress_note_failure(relative_path, short_error)
                _progress_finish_item("failed")

        for relative_path in changed_paths:
            if cancelled:
                break
            if _cancel_event.is_set():
                cancelled = True
                break
            _progress_note("Updating", relative_path)
            try:
                _upload_or_update_file(
                    service, manifest, relative_path, current_files[relative_path],
                    content_hash=computed_hashes.get(relative_path),
                )
                updated += 1
                _progress_finish_item("updated")
            except Exception as exc:
                logger.exception("Cloud backup: failed to update %s", relative_path)
                short_error = _short_error_text(exc)
                errors.append(f"update failed: {relative_path} ({short_error})")
                _progress_note_failure(relative_path, short_error)
                _progress_finish_item("failed")

        apply_seconds = time.monotonic() - phase_started
        logger.info("Cloud backup: apply phase (upload/update/delete) took %.2fs", apply_seconds)

        verification_seconds = 0.0
        if cancelled:
            status = "stopped"
            logger.info("Cloud backup sync stopped by request (triggered_by=%s)", triggered_by)
        else:
            phase_started = time.monotonic()
            verification = _verify(service, manifest)
            verification_seconds = time.monotonic() - phase_started
            if errors:
                status = "partial"
            elif not verification["ok"]:
                status = "partial"
            else:
                status = "success"
            logger.info(
                "Cloud backup sync finished: %s (verification took %.2fs)",
                status, verification_seconds,
            )
        logger.info(
            "Cloud backup: phase timing -- change_detection=%.2fs apply=%.2fs verification=%.2fs total=%.2fs",
            change_detection_seconds, apply_seconds, verification_seconds,
            change_detection_seconds + apply_seconds + verification_seconds,
        )

        summary = _build_run_summary(
            started_at, status,
            added=added, updated=updated, deleted=deleted_count, skipped=unchanged_count,
            error="; ".join(errors) if errors else None,
            verification=verification,
            phase_timing={
                "change_detection_seconds": round(change_detection_seconds, 2),
                "apply_seconds": round(apply_seconds, 2),
                "verification_seconds": round(verification_seconds, 2),
            },
        )
    except NotAuthorizedError as exc:
        # build_drive_service() never opens a browser itself (see
        # authorize() for the dedicated, separate action that does) -- so
        # this is a fast, immediate failure, not a hang, whether triggered
        # manually or from the scheduled timer.
        logger.warning("Cloud backup sync aborted -- not authorized: %s", exc)
        summary = _build_run_summary(
            started_at, "not_authorized",
            added=added, updated=updated, deleted=deleted_count,
            error=str(exc),
        )
    except Exception as exc:
        logger.exception("Cloud backup sync failed")
        summary = _build_run_summary(
            started_at, "failed",
            added=added, updated=updated, deleted=deleted_count,
            error=str(exc),
        )
    finally:
        with _status_lock:
            _status["running"] = False
            _status["last_run"] = summary
        _progress_stop()
        _cancel_event.clear()

    return {"configured": True, "running": False, "last_run": summary}


# ---------------------------------------------------------------------------
# Scheduling (Part 6) -- same self-rescheduling threading.Timer pattern as
# app.py's schedule_item_export().
# ---------------------------------------------------------------------------

def schedule(initial_delay=0):
    """First run fires after initial_delay; every run after that reschedules
    itself CLOUD_BACKUP_INTERVAL seconds later (measured from when the run
    finishes, same as schedule_item_export)."""
    global _timer

    if not is_configured():
        _log_not_configured_once()
        return

    def _job():
        global _timer
        if not is_authorized():
            message = "Not yet authorized -- run 'Authorize Google Drive' once from the System panel"
            logger.info("Skipping scheduled cloud backup -- %s", message)
            _record_skipped_run("not_authorized", message)
        elif not SYNC_LOCK.acquire(blocking=False):
            message = "A sync was already running at the scheduled time"
            logger.info("Skipping scheduled cloud backup -- %s", message)
            _record_skipped_run("already_running", message)
        else:
            try:
                run_sync(triggered_by="schedule")
            except Exception:
                logger.exception("Scheduled cloud backup run raised unexpectedly")
            finally:
                SYNC_LOCK.release()
        _timer = threading.Timer(Config.CLOUD_BACKUP_INTERVAL, _job)
        _timer.daemon = True
        _timer.start()

    delay = initial_delay if initial_delay and initial_delay > 0 else Config.CLOUD_BACKUP_INTERVAL
    _timer = threading.Timer(delay, _job)
    _timer.daemon = True
    _timer.start()
