import os
from datetime import timedelta

try:
    # .env is written by first_time_setup.bat but nothing previously loaded
    # it into the process environment — os.environ.get() below only ever
    # saw real OS env vars. Load it here so .env actually takes effect.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Config:
    # Flask
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "tally-stock-viewer-dev-secret")
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

    # Session and cookies
    SESSION_TIMEOUT_HOURS = int(os.environ.get("SESSION_TIMEOUT_HOURS", "8"))
    PERMANENT_SESSION_LIFETIME = timedelta(hours=SESSION_TIMEOUT_HOURS)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"

    # Database
    DB_PATH = os.environ.get("DB_PATH", "data/mappings.db")
    DB_TIMEOUT = int(os.environ.get("DB_TIMEOUT", "30"))
    SQLITE_CACHE_KB = int(os.environ.get("SQLITE_CACHE_KB", "4096"))

    # Tally
    TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000")
    TALLY_TIMEOUT = int(os.environ.get("TALLY_TIMEOUT", "30"))
    TALLY_RETRY_ATTEMPTS = int(os.environ.get("TALLY_RETRY_ATTEMPTS", "3"))
    TALLY_EXPORT_INTERVAL = int(os.environ.get("TALLY_EXPORT_INTERVAL", "180"))

    # Files
    MAX_IMAGE_SIZE = int(os.environ.get("MAX_IMAGE_SIZE", str(10 * 1024 * 1024)))
    ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif"}

    # Cache
    MAX_IMAGE_RESPONSE_LIMIT = int(os.environ.get("MAX_IMAGE_RESPONSE_LIMIT", "500"))
    INITIAL_IMAGE_SCAN = os.environ.get("INITIAL_IMAGE_SCAN", "1").strip().lower() not in ("0", "false")

    # System panel (remote deployment/admin panel) — deliberately has no
    # default. If unset, the entire /admin/system* panel stays disabled.
    SYSTEM_ACCESS_TOKEN = os.environ.get("SYSTEM_ACCESS_TOKEN", "").strip()

    # Accounts panel secondary password gate — deliberately has no default.
    # If unset, Manage Accounts stays locked (same "not configured" pattern
    # as SYSTEM_ACCESS_TOKEN above) rather than silently allowing access.
    ACCOUNTS_ACCESS_PASSWORD = os.environ.get("ACCOUNTS_ACCESS_PASSWORD", "").strip()

    # Cloud backup (Google Drive) — OAuth installed-app flow, authenticating
    # as an actual Google account rather than a Service Account (a Service
    # Account has zero personal Drive storage quota of its own: it can
    # create folders but every real file upload fails with a "Service
    # Accounts do not have storage quota" 403 — see build_drive_service()
    # in cloud_backup.py). One-time setup: create an OAuth Client ID
    # (Desktop app) in the Google Cloud project, download its JSON to
    # GDRIVE_OAUTH_CLIENT_SECRETS_PATH, then run a sync once — a browser
    # opens for a one-time consent step and the resulting token (with
    # refresh token) is saved to GDRIVE_OAUTH_TOKEN_PATH. Every run after
    # that refreshes silently, no browser needed, so scheduled/unattended
    # runs work too. Deliberately has no default for any of these. If any
    # is missing, cloud_backup.py cleanly no-ops (logs once, never
    # schedules the job, never crashes) — same fallback discipline as
    # SYSTEM_ACCESS_TOKEN/ACCOUNTS_ACCESS_PASSWORD above.
    GDRIVE_OAUTH_CLIENT_SECRETS_PATH = os.environ.get("GDRIVE_OAUTH_CLIENT_SECRETS_PATH", "").strip()
    GDRIVE_OAUTH_TOKEN_PATH = os.environ.get("GDRIVE_OAUTH_TOKEN_PATH", "").strip()
    GDRIVE_BACKUP_FOLDER_ID = os.environ.get("GDRIVE_BACKUP_FOLDER_ID", "").strip()
    CLOUD_BACKUP_INTERVAL = int(os.environ.get("CLOUD_BACKUP_INTERVAL", str(3 * 24 * 3600)))

    # Logging
    LOG_DIR = os.environ.get("LOG_DIR", "logs")
    LOG_FILE = os.environ.get("LOG_FILE", "logs/app.log")
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
