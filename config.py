import os
from datetime import timedelta


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
    CACHE_SIZE = int(os.environ.get("CACHE_SIZE", "100"))
    CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))
    MAX_IMAGE_RESPONSE_LIMIT = int(os.environ.get("MAX_IMAGE_RESPONSE_LIMIT", "500"))
    INITIAL_IMAGE_SCAN = os.environ.get("INITIAL_IMAGE_SCAN", "1").strip().lower() not in ("0", "false")

    # Auth
    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    # Can be plaintext (legacy) or werkzeug pbkdf2 hash
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "idris123")

    # Logging
    LOG_DIR = os.environ.get("LOG_DIR", "logs")
    LOG_FILE = os.environ.get("LOG_FILE", "logs/app.log")
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
