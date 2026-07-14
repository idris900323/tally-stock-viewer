import hashlib
import os
import shutil
import sqlite3
import logging
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime

from config import Config
from utils.normalize import normalize_lookup_key


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = Config.DB_PATH if os.path.isabs(Config.DB_PATH) else os.path.join(BASE_DIR, Config.DB_PATH)
IMAGE_ROOT = os.path.join(BASE_DIR, "data", "S.S IMAGE")
logger = logging.getLogger(__name__)


def _canonicalize_filepath(filepath):
    value = str(filepath or "").strip()
    if not value:
        return value
    if os.path.isabs(value):
        normalized = os.path.normpath(os.path.abspath(value))
        if os.name == "nt":
            normalized = os.path.normcase(normalized)
        return normalized
    return value.replace("\\", "/")


def _validate_stock_item_name(stock_item_name):
    value = str(stock_item_name or "")
    if "\x00" in value:
        raise ValueError("stock item name contains null byte")
    if len(value) > 500:
        raise ValueError("stock item name exceeds 500 characters")
    return value


def _validate_confidence(confidence):
    value = float(confidence)
    if value < 0.0 or value > 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")
    return value


def _resolve_filepath_candidate(value):
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(IMAGE_ROOT, value))


def _validate_filepath(filepath):
    value = _canonicalize_filepath(filepath)
    if not value:
        raise ValueError("filepath is required")
    if "\x00" in value:
        raise ValueError("filepath contains null byte")

    candidate = _resolve_filepath_candidate(value)
    if not os.path.exists(candidate):
        raise ValueError(f"filepath does not exist: {candidate}")
    if not os.access(candidate, os.R_OK):
        raise ValueError(f"filepath is not readable: {candidate}")
    if os.path.getsize(candidate) > Config.MAX_IMAGE_SIZE:
        raise ValueError(f"filepath exceeds max image size: {candidate}")

    if os.path.isabs(value):
        image_root_norm = os.path.normcase(os.path.normpath(IMAGE_ROOT)) if os.name == "nt" else os.path.normpath(IMAGE_ROOT)
        candidate_norm = os.path.normcase(os.path.normpath(candidate)) if os.name == "nt" else os.path.normpath(candidate)
        if candidate_norm.startswith(image_root_norm + os.sep) or candidate_norm == image_root_norm:
            relative_path = os.path.relpath(candidate, IMAGE_ROOT).replace("\\", "/")
            return relative_path
        raise ValueError("absolute filepath outside IMAGE_ROOT is not allowed")

    normalized_value = os.path.normpath(value)
    if normalized_value == os.pardir or normalized_value.startswith(os.pardir + os.sep):
        raise ValueError("filepath contains unsafe relative traversal")

    return value.replace("\\", "/")


def _normalize_legacy_absolute_image_paths(conn):
    """Convert legacy absolute image paths under IMAGE_ROOT to portable relative paths.

    Some rows already have a canonical relative-path row for the same physical
    file (left behind by earlier scans). Renaming the legacy row in place would
    collide with that row's UNIQUE(filepath) constraint, so such duplicates are
    merged into the canonical row (preserving any mapping) and the legacy row
    is dropped instead of renamed.
    """
    rows = conn.execute("SELECT id, filepath FROM images").fetchall()
    image_root_norm = os.path.normcase(os.path.normpath(IMAGE_ROOT)) if os.name == "nt" else os.path.normpath(IMAGE_ROOT)

    canonical_ids_by_relpath = {}
    legacy_rows = []
    for row in rows:
        raw_value = str(row["filepath"] or "").strip()
        if not raw_value:
            continue
        if os.path.isabs(raw_value):
            legacy_rows.append(row)
        else:
            canonical_ids_by_relpath[raw_value.strip("/").lower()] = int(row["id"])

    renamed = 0
    merged = 0
    for row in legacy_rows:
        raw_value = str(row["filepath"]).strip()
        abs_value = os.path.normpath(os.path.abspath(raw_value))
        abs_norm = os.path.normcase(abs_value) if os.name == "nt" else abs_value
        if not (abs_norm == image_root_norm or abs_norm.startswith(image_root_norm + os.sep)):
            continue

        relative_path = os.path.relpath(abs_value, IMAGE_ROOT).replace("\\", "/")
        if not relative_path or relative_path == ".":
            continue

        legacy_id = int(row["id"])
        canonical_id = canonical_ids_by_relpath.get(relative_path.strip("/").lower())

        if canonical_id is None:
            conn.execute("UPDATE images SET filepath = ? WHERE id = ?", (relative_path, legacy_id))
            canonical_ids_by_relpath[relative_path.strip("/").lower()] = legacy_id
            renamed += 1
            continue

        legacy_mapping = conn.execute(
            "SELECT stock_item_name, car_model, confidence, confirmed_by, created_at FROM mappings WHERE image_id = ?",
            (legacy_id,),
        ).fetchone()
        canonical_mapping = conn.execute(
            "SELECT created_at FROM mappings WHERE image_id = ?", (canonical_id,)
        ).fetchone()
        # Both rows may carry independent human-confirmed mappings (the legacy
        # duplicate wasn't always a dead end); keep whichever was confirmed most
        # recently rather than always favoring the canonical row.
        legacy_is_newer = legacy_mapping is not None and (
            canonical_mapping is None or str(legacy_mapping["created_at"]) > str(canonical_mapping["created_at"])
        )
        if legacy_is_newer:
            conn.execute(
                """
                INSERT INTO mappings (image_id, stock_item_name, car_model, confidence, confirmed_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_id, stock_item_name) DO UPDATE SET
                    stock_item_name=excluded.stock_item_name,
                    car_model=excluded.car_model,
                    confidence=excluded.confidence,
                    confirmed_by=excluded.confirmed_by,
                    created_at=excluded.created_at
                """,
                (
                    canonical_id,
                    legacy_mapping["stock_item_name"],
                    legacy_mapping["car_model"],
                    legacy_mapping["confidence"],
                    legacy_mapping["confirmed_by"],
                    legacy_mapping["created_at"],
                ),
            )
        conn.execute("DELETE FROM images WHERE id = ?", (legacy_id,))
        merged += 1

    return renamed + merged


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=Config.DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA cache_size = {-max(512, int(Config.SQLITE_CACHE_KB))}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_connection():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_database():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                car_folder TEXT NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL UNIQUE,
                scan_date TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL,
                stock_item_name TEXT,
                car_model TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                confirmed_by TEXT NOT NULL DEFAULT 'human',
                created_at TEXT NOT NULL,
                FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS folder_car_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_name TEXT NOT NULL UNIQUE,
                car_model_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                access_code TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'customer')),
                force_contact_us INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_login TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                performed_by INTEGER,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (performed_by) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS base_prices (
                stock_item_name TEXT PRIMARY KEY,
                price TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                stock_item_name TEXT NOT NULL,
                price TEXT NOT NULL,
                UNIQUE(user_id, stock_item_name),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_car_folder ON images(car_folder)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_car_folder_id ON images(car_folder, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_stock_item ON mappings(stock_item_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_stock_item_lower ON mappings(LOWER(stock_item_name), id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_image_id ON mappings(image_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mappings_image_stock_unique ON mappings(image_id, stock_item_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_filepath ON images(filepath)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_folder_car_mapping ON folder_car_mapping(folder_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
        _ensure_users_schema(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_account_logs_timestamp ON account_logs(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_prices_user_stock ON customer_prices(user_id, stock_item_name)")
        seed_default_data(conn)
        try:
            normalized_count = _normalize_legacy_absolute_image_paths(conn)
            if normalized_count:
                logger.info("Normalized %s legacy absolute image paths to relative paths", normalized_count)
        except Exception:
            logger.exception("Failed to normalize legacy image paths")

    _migrate_remove_image_id_unique()


def _backup_database_file():
    """Copy the live DB file, matching the timestamp convention used by
    the System panel's /admin/system/download_backup (backup_TIMESTAMP)."""
    if not os.path.exists(DB_PATH):
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{DB_PATH}.backup_{timestamp}"
    shutil.copy2(DB_PATH, backup_path)
    return backup_path


def _migrate_remove_image_id_unique():
    """One-time migration: rebuild mappings without UNIQUE(image_id).

    The UNIQUE constraint made add_mapping()'s ON CONFLICT(image_id) upsert
    silently overwrite the existing row whenever the same image was confirmed
    against a second stock item, instead of creating a second row -- one
    image could never be linked to more than one stock item at a time.
    SQLite can't drop a column constraint via ALTER TABLE, so this rebuilds
    the table in a transaction, guarded to run only while the old unique
    auto-index is still present. Recreates a compound UNIQUE(image_id,
    stock_item_name) index so add_mapping()'s upsert still has a real
    constraint to target -- this only dedupes the exact same pair and does
    not restrict how many stock items a single image can map to.
    """
    conn = _connect()
    try:
        still_has_unique_image_id = False
        for row in conn.execute("PRAGMA index_list('mappings')").fetchall():
            if not row["unique"] or row["origin"] != "u":
                continue
            cols = conn.execute(f"PRAGMA index_info('{row['name']}')").fetchall()
            if [c["name"] for c in cols] == ["image_id"]:
                still_has_unique_image_id = True
                break

        if not still_has_unique_image_id:
            return

        backup_path = _backup_database_file()
        logger.info("Migrating mappings table to remove UNIQUE(image_id); backup at %s", backup_path)

        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                CREATE TABLE mappings_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    stock_item_name TEXT,
                    car_model TEXT,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    confirmed_by TEXT NOT NULL DEFAULT 'human',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                INSERT INTO mappings_new (id, image_id, stock_item_name, car_model, confidence, confirmed_by, created_at)
                SELECT id, image_id, stock_item_name, car_model, confidence, confirmed_by, created_at FROM mappings
                """
            )
            conn.execute("DROP TABLE mappings")
            conn.execute("ALTER TABLE mappings_new RENAME TO mappings")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_stock_item ON mappings(stock_item_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_stock_item_lower ON mappings(LOWER(stock_item_name), id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_image_id ON mappings(image_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mappings_image_stock_unique ON mappings(image_id, stock_item_name)")
            conn.commit()
            logger.info("Migration succeeded: mappings.image_id UNIQUE constraint removed.")
        except Exception:
            conn.rollback()
            logger.exception(
                "Migration failed removing UNIQUE(image_id); rolled back. Backup preserved at %s",
                backup_path,
            )
            raise
    finally:
        conn.close()


def _ensure_users_schema(conn):
    rows = conn.execute("PRAGMA table_info(users)").fetchall()
    columns = {str(row[1]).lower() for row in rows}
    if "created_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
        conn.execute("UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL OR TRIM(created_at) = ''")
    if "is_active" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "last_login" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            performed_by INTEGER,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (performed_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )


def seed_default_data(conn=None):
    own_connection = conn is None
    if own_connection:
        conn = _connect()

    try:
        row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        user_count = int(row["count"] if row else 0)
        if user_count > 0:
            return

        conn.executemany(
            """
            INSERT INTO users (username, access_code, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("admin", "idris123", "admin", datetime.now().isoformat(timespec="seconds")),
                ("star", "111", "customer", datetime.now().isoformat(timespec="seconds")),
                ("jeewajee", "222", "customer", datetime.now().isoformat(timespec="seconds")),
            ],
        )
    finally:
        if own_connection and conn is not None:
            conn.close()


def _row_to_dict(row):
    return dict(row) if row is not None else None


def add_image(car_folder, filename, filepath):
    filepath = _validate_filepath(filepath)
    scan_date = datetime.now().isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO images (car_folder, filename, filepath, scan_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    car_folder=excluded.car_folder,
                    filename=excluded.filename,
                    scan_date=excluded.scan_date
                """,
                (car_folder, filename, filepath, scan_date),
            )
            return cursor.lastrowid
    except Exception:
        logger.exception("add_image failed for filepath=%s", filepath)
        raise


def add_images_batch(records):
    """Batch upsert image records.

    records: iterable of (car_folder, filename, filepath)
    """
    scan_date = datetime.now().isoformat(timespec="seconds")
    values = []
    for car_folder, filename, filepath in records:
        try:
            safe_path = _validate_filepath(filepath)
        except Exception:
            logger.warning("Skipping invalid filepath during batch insert: %s", filepath)
            continue
        values.append((car_folder, filename, safe_path, scan_date))

    if not values:
        return 0

    try:
        with _connect() as conn:
            conn.executemany(
                """
                INSERT INTO images (car_folder, filename, filepath, scan_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    car_folder=excluded.car_folder,
                    filename=excluded.filename,
                    scan_date=excluded.scan_date
                """,
                values,
            )
        return len(values)
    except Exception:
        logger.exception("add_images_batch failed")
        raise


def add_mapping(image_id, stock_item_name, car_model, confidence, confirmed_by="human"):
    stock_item_name = _validate_stock_item_name(stock_item_name)
    confidence = _validate_confidence(confidence)
    created_at = datetime.now().isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO mappings (image_id, stock_item_name, car_model, confidence, confirmed_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_id, stock_item_name) DO UPDATE SET
                    stock_item_name=excluded.stock_item_name,
                    car_model=excluded.car_model,
                    confidence=excluded.confidence,
                    confirmed_by=excluded.confirmed_by,
                    created_at=excluded.created_at
                """,
                (image_id, stock_item_name, car_model, confidence, confirmed_by, created_at),
            )
    except Exception:
        logger.exception("add_mapping failed for image_id=%s", image_id)
        raise


def remove_mapping_by_image_id(image_id):
    try:
        with _connect() as conn:
            cursor = conn.execute(
                "DELETE FROM mappings WHERE image_id = ?",
                (image_id,),
            )
        return cursor.rowcount > 0
    except Exception:
        logger.exception("remove_mapping_by_image_id failed for image_id=%s", image_id)
        raise


def add_folder_mapping(folder_name, car_model):
    created_at = datetime.now().isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO folder_car_mapping (folder_name, car_model_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(folder_name) DO UPDATE SET
                    car_model_name=excluded.car_model_name,
                    created_at=excluded.created_at
                """,
                (folder_name, car_model, created_at),
            )
    except Exception:
        logger.exception("add_folder_mapping failed for folder=%s", folder_name)
        raise


def cleanup_database():
    """Run lightweight SQLite maintenance tasks."""
    with _connect() as conn:
        conn.execute("PRAGMA optimize")
        conn.execute("VACUUM")


def get_folder_car_model(folder_name):
    with _connect() as conn:
        row = conn.execute(
            "SELECT folder_name, car_model_name, created_at FROM folder_car_mapping WHERE folder_name = ?",
            (folder_name,),
        ).fetchone()
    return _row_to_dict(row)


def get_image_by_id(image_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, car_folder, filename, filepath, scan_date FROM images WHERE id = ?",
            (image_id,),
        ).fetchone()
    return _row_to_dict(row)


def get_mapping_by_image_id(image_id):
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, image_id, stock_item_name, car_model, confidence, confirmed_by, created_at
            FROM mappings
            WHERE image_id = ?
            """,
            (image_id,),
        ).fetchone()
    return _row_to_dict(row)


def remove_mappings_for_stock_item(stock_item_name, exclude_image_id=None):
    """Delete any mapping(s) pointing at stock_item_name, optionally excluding one image_id.

    Used so a stock item is only ever mapped to a single image at a time.
    """
    try:
        with _connect() as conn:
            if exclude_image_id is not None:
                cursor = conn.execute(
                    "DELETE FROM mappings WHERE LOWER(stock_item_name) = LOWER(?) AND image_id != ?",
                    (stock_item_name, exclude_image_id),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM mappings WHERE LOWER(stock_item_name) = LOWER(?)",
                    (stock_item_name,),
                )
        return cursor.rowcount
    except Exception:
        logger.exception("remove_mappings_for_stock_item failed for stock_item_name=%s", stock_item_name)
        raise


def get_mapping_for_stock_item(stock_item_name):
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                m.id,
                m.image_id,
                m.stock_item_name,
                m.car_model,
                m.confidence,
                m.confirmed_by,
                m.created_at,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date
            FROM mappings m
            JOIN images i ON i.id = m.image_id
            WHERE LOWER(m.stock_item_name) = LOWER(?)
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT 1
            """,
            (stock_item_name,),
        ).fetchone()
    return _row_to_dict(row)


def get_mappings_for_stock_items(stock_item_names):
    cleaned_keys = set()
    for stock_item_name in stock_item_names or []:
        try:
            value = _validate_stock_item_name(stock_item_name)
        except ValueError:
            continue
        key = normalize_lookup_key(value)
        if key:
            cleaned_keys.add(key)

    if not cleaned_keys:
        return {}

    # Matching is done in Python (not SQL LOWER()) because stock item names
    # commonly carry irregular internal whitespace from Tally exports, and
    # normalize_lookup_key() collapses those runs -- something plain SQL
    # LOWER() cannot replicate. Using the same shared key on both the input
    # names and the stored rows is what keeps this in sync with the app-side
    # lookup builder (see _normalize_lookup_key in app.py).
    query = """
        SELECT
            m.id,
            m.image_id,
            m.stock_item_name,
            m.car_model,
            m.confidence,
            m.confirmed_by,
            m.created_at,
            i.car_folder,
            i.filename,
            i.filepath,
            i.scan_date
        FROM mappings m
        JOIN images i ON i.id = m.image_id
    """
    with _connect() as conn:
        rows = conn.execute(query).fetchall()

    result = {}
    for row in rows:
        record = _row_to_dict(row)
        key = normalize_lookup_key(record.get("stock_item_name"))
        if key not in cleaned_keys:
            continue
        existing = result.get(key)
        candidate_rank = (record.get("created_at") or "", record.get("id") or 0)
        existing_rank = (existing.get("created_at") or "", existing.get("id") or 0) if existing else None
        if existing is None or candidate_rank > existing_rank:
            result[key] = record
    return result


def get_confirmed_mappings():
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                m.id,
                m.image_id,
                m.stock_item_name,
                m.car_model,
                m.confidence,
                m.confirmed_by,
                m.created_at,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date
            FROM mappings m
            JOIN images i ON i.id = m.image_id
            WHERE m.confidence >= 1.0
            ORDER BY m.created_at DESC, m.id DESC
            """
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_unmapped_images(limit=None):
    query = """
        WITH ranked_unmapped AS (
            SELECT
                i.id,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(i.filepath)
                    ORDER BY i.id DESC
                ) AS rn
            FROM images i
            LEFT JOIN mappings m ON m.image_id = i.id
            WHERE m.image_id IS NULL
        )
        SELECT id, car_folder, filename, filepath, scan_date
        FROM ranked_unmapped
        WHERE rn = 1
        ORDER BY LOWER(filename) ASC, id ASC
    """
    params = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_unmapped_images_by_folder(folder_name, limit=None):
    """Return unmapped images restricted to a specific folder name.

    This performs the filtering at the SQL level so callers don't need to
    pull the full unmapped set into memory.
    """
    query = """
        WITH ranked_unmapped AS (
            SELECT
                i.id,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(i.filepath)
                    ORDER BY i.id DESC
                ) AS rn
            FROM images i
            LEFT JOIN mappings m ON m.image_id = i.id
            WHERE m.image_id IS NULL
              AND i.car_folder = ?
        )
        SELECT id, car_folder, filename, filepath, scan_date
        FROM ranked_unmapped
        WHERE rn = 1
        ORDER BY LOWER(filename) ASC, id ASC
    """
    params = [folder_name]
    if limit is not None:
        query = query.strip() + " LIMIT ?"
        params.append(int(limit))

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_images_by_folder(folder_name, limit=None):
    """Return all images for a folder with mapping status details."""
    query = """
        WITH ranked_images AS (
            SELECT
                i.id,
                i.car_folder,
                i.filename,
                i.filepath,
                i.scan_date,
                CASE WHEN m.image_id IS NULL THEN 0 ELSE 1 END AS mapped,
                m.stock_item_name,
                m.confidence,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(i.filepath)
                    ORDER BY
                        CASE WHEN m.image_id IS NULL THEN 1 ELSE 0 END ASC,
                        i.id DESC
                ) AS rn
            FROM images i
            LEFT JOIN mappings m ON m.image_id = i.id
            WHERE i.car_folder = ?
        )
        SELECT
            id,
            car_folder,
            filename,
            filepath,
            scan_date,
            mapped,
            stock_item_name,
            confidence
        FROM ranked_images
        WHERE rn = 1
        ORDER BY LOWER(filename) ASC, id ASC
    """
    params = [folder_name]
    if limit is not None:
        query = query.strip() + " LIMIT ?"
        params.append(int(limit))

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_next_unmapped_image(after_image_id=None):
    if after_image_id is None:
        rows = get_unmapped_images(limit=1)
        return rows[0] if rows else None

    with _connect() as conn:
        row = conn.execute(
            """
            WITH ranked_unmapped AS (
                SELECT
                    i.id,
                    i.car_folder,
                    i.filename,
                    i.filepath,
                    i.scan_date,
                    ROW_NUMBER() OVER (
                        PARTITION BY LOWER(i.filepath)
                        ORDER BY i.id DESC
                    ) AS rn
                FROM images i
                LEFT JOIN mappings m ON m.image_id = i.id
                WHERE m.image_id IS NULL
                  AND i.id > ?
            )
            SELECT id, car_folder, filename, filepath, scan_date
            FROM ranked_unmapped
            WHERE rn = 1
            ORDER BY id ASC
            LIMIT 1
            """,
            (after_image_id,),
        ).fetchone()
    return _row_to_dict(row)


def get_image_count():
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM images").fetchone()
    return int(row["count"] if row else 0)


def get_mapped_image_count():
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM mappings
            WHERE TRIM(COALESCE(stock_item_name, '')) <> ''
              AND stock_item_name <> '__UNMATCHABLE__'
            """
        ).fetchone()
    return int(row["count"] if row else 0)


def get_processed_image_count():
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM mappings").fetchone()
    return int(row["count"] if row else 0)


def get_mapping_stats():
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM images) AS total_images,
                (
                    SELECT COUNT(*)
                    FROM mappings
                    WHERE TRIM(COALESCE(stock_item_name, '')) <> ''
                      AND stock_item_name <> '__UNMATCHABLE__'
                ) AS mapped_images,
                (SELECT COUNT(*) FROM mappings) AS processed_images
            """
        ).fetchone()
    total_images = int(row["total_images"] if row else 0)
    mapped_images = int(row["mapped_images"] if row else 0)
    processed_images = int(row["processed_images"] if row else 0)
    percent = round((processed_images / total_images) * 100, 2) if total_images else 0.0
    return {
        "total_images": total_images,
        "mapped_images": mapped_images,
        "unmapped_images": max(total_images - processed_images, 0),
        "percent_complete": percent,
    }


def get_all_images():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, car_folder, filename, filepath, scan_date FROM images ORDER BY id ASC"
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_all_images_with_link_status():
    """Return every image row plus whether it currently has a confirmed stock-item mapping.

    Uses an EXISTS subquery rather than a LEFT JOIN because a single image can
    have more than one mapping row (the UNIQUE(image_id) constraint was
    removed -- see _migrate_remove_image_id_unique), so a join would return
    one row per mapping and inflate counts for anything that iterates this.
    "Confirmed" mirrors get_mapped_image_count()'s definition: a non-empty
    stock_item_name that isn't the '__UNMATCHABLE__' sentinel.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                i.id, i.car_folder, i.filename, i.filepath, i.scan_date,
                EXISTS (
                    SELECT 1 FROM mappings m
                    WHERE m.image_id = i.id
                      AND TRIM(COALESCE(m.stock_item_name, '')) <> ''
                      AND m.stock_item_name <> '__UNMATCHABLE__'
                ) AS mapped
            FROM images i
            ORDER BY i.id ASC
            """
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_stock_item_names_for_images(image_ids):
    """Return {image_id: [stock_item_name, ...]} of confirmed mappings for the given ids.

    Scoped to a specific id set (rather than joining onto every image) so
    callers who only need this for a small subset -- e.g. the missing-images
    list -- don't pay for a catalog-wide GROUP_CONCAT. An image can have more
    than one mapping row (bulk match links one image to many stock items),
    so this returns a list per image rather than a single name. "Confirmed"
    mirrors get_mapped_image_count()'s definition.
    """
    ids = sorted({int(image_id) for image_id in (image_ids or [])})
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT image_id, stock_item_name
            FROM mappings
            WHERE image_id IN ({placeholders})
              AND TRIM(COALESCE(stock_item_name, '')) <> ''
              AND stock_item_name <> '__UNMATCHABLE__'
            ORDER BY image_id ASC, created_at DESC, id DESC
            """,
            ids,
        ).fetchall()

    result = defaultdict(list)
    for row in rows:
        result[int(row["image_id"])].append(row["stock_item_name"])
    return dict(result)


def get_image_folders():
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT car_folder
            FROM images
            WHERE car_folder IS NOT NULL AND TRIM(car_folder) <> ''
            ORDER BY LOWER(car_folder), car_folder
            """
        ).fetchall()
    return [row["car_folder"] for row in rows]


def _hash_file(file_path, chunk_size=65536):
    hasher = hashlib.sha256()
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _resolve_image_root_path(stored_path):
    value = str(stored_path or "").strip().replace("\\", "/")
    if not value:
        return None
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(IMAGE_ROOT, value))


def find_duplicate_image_rows():
    """Find image rows that are very likely visible duplicates.

    Candidate grouping is (car_folder, filename with extension stripped and
    lowercased) — e.g. "BLK-DKTAN" and "BLK-DKTAN.jpg" land in the same
    candidate group. Candidates are only reported as duplicates once
    confirmed by content hash, so two different photos that happen to share
    a generic base name (e.g. "1.jfif" reused across unrelated subfolders)
    are never wrongly flagged.

    Read-only: does not delete or modify anything.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                i.id, i.car_folder, i.filename, i.filepath, i.scan_date,
                CASE WHEN m.image_id IS NULL THEN 0 ELSE 1 END AS mapped,
                m.stock_item_name
            FROM images i
            LEFT JOIN mappings m ON m.image_id = i.id
            """
        ).fetchall()

    candidates = defaultdict(list)
    for row in rows:
        stem = os.path.splitext(str(row["filename"] or ""))[0].strip().lower()
        folder_key = str(row["car_folder"] or "").strip().lower()
        if not stem:
            continue
        candidates[(folder_key, stem)].append(_row_to_dict(row))

    duplicate_groups = []
    for (_folder_key, stem), group_rows in candidates.items():
        if len(group_rows) < 2:
            continue

        by_hash = defaultdict(list)
        for row in group_rows:
            file_path = _resolve_image_root_path(row.get("filepath"))
            if not file_path or not os.path.exists(file_path):
                continue
            try:
                file_hash = _hash_file(file_path)
            except OSError:
                continue
            by_hash[file_hash].append(row)

        for file_hash, hashed_rows in by_hash.items():
            if len(hashed_rows) < 2:
                continue
            duplicate_groups.append({
                "car_folder": hashed_rows[0].get("car_folder"),
                "filename_stem": stem,
                "content_hash": file_hash,
                "count": len(hashed_rows),
                "rows": sorted(hashed_rows, key=lambda r: r["id"]),
            })

    duplicate_groups.sort(key=lambda group: group["count"], reverse=True)
    return duplicate_groups


def remove_missing_image_rows(image_ids):
    """Delete the given image rows (and their mappings, via ON DELETE CASCADE).

    Callers are responsible for having already re-verified each id is still
    actually missing on disk right before calling this -- this function just
    performs the delete. Returns (images_removed, mappings_removed).
    """
    ids = sorted({int(image_id) for image_id in (image_ids or [])})
    if not ids:
        return 0, 0

    placeholders = ",".join("?" for _ in ids)
    try:
        with _connect() as conn:
            mapping_row = conn.execute(
                f"SELECT COUNT(*) AS count FROM mappings WHERE image_id IN ({placeholders})",
                ids,
            ).fetchone()
            mappings_removed = int(mapping_row["count"] if mapping_row else 0)

            cursor = conn.execute(
                f"DELETE FROM images WHERE id IN ({placeholders})",
                ids,
            )
            images_removed = cursor.rowcount
        return images_removed, mappings_removed
    except Exception:
        logger.exception("remove_missing_image_rows failed for ids=%s", ids)
        raise


def authenticate_user(username, access_code):
    username_value = str(username or "").strip()
    access_code_value = str(access_code or "").strip()
    if not username_value or not access_code_value:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, username, role, is_active
            FROM users
            WHERE LOWER(username) = LOWER(?) AND access_code = ?
            LIMIT 1
            """,
            (username_value, access_code_value),
        ).fetchone()
    return _row_to_dict(row)


def update_last_login(user_id):
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), int(user_id)),
        )


def get_user_by_id(user_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
    return _row_to_dict(row)


def get_customer_users():
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, username, role, created_at
            FROM users
            WHERE role = 'customer'
            ORDER BY LOWER(username), username
            """
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def create_customer_user(username, access_code):
    username_value = str(username or "").strip()
    access_code_value = str(access_code or "").strip()
    if not username_value:
        raise ValueError("username is required")
    if not access_code_value:
        raise ValueError("access code is required")
    if len(username_value) > 100:
        raise ValueError("username is too long")
    if len(access_code_value) > 100:
        raise ValueError("access code is too long")

    try:
        with _connect() as conn:
            created_at = datetime.now().isoformat(timespec="seconds")
            cursor = conn.execute(
                """
                INSERT INTO users (username, access_code, role, created_at)
                VALUES (?, ?, 'customer', ?)
                """,
                (username_value, access_code_value, created_at),
            )
            user_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise ValueError("username already exists") from exc

    return get_user_by_id(user_id)


def log_account_action(user_id, action, performed_by):
    action_value = str(action or "").strip().lower()
    if not action_value:
        return
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO account_logs (user_id, action, performed_by, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (
                int(user_id) if user_id is not None else None,
                action_value,
                int(performed_by) if performed_by is not None else None,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def get_all_customers_with_details():
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, username, access_code, role, is_active, created_at, last_login
            FROM users
            WHERE role = 'customer'
            ORDER BY datetime(COALESCE(created_at, '1970-01-01T00:00:00')) DESC, id DESC
            """
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def toggle_customer_active_status(user_id):
    user_id_value = int(user_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, username, role, is_active FROM users WHERE id = ?",
            (user_id_value,),
        ).fetchone()
        user = _row_to_dict(row)
        if not user:
            raise ValueError("user not found")
        if user.get("role") != "customer":
            raise ValueError("only customer accounts can be paused or resumed")

        new_status = 0 if user.get("is_active") else 1
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, user_id_value))
        user["is_active"] = new_status
    return user


def set_all_customer_active_status(is_active):
    status_value = 1 if is_active else 0
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE users SET is_active = ? WHERE role = 'customer'",
            (status_value,),
        )
    return cursor.rowcount


def delete_customer_user(user_id):
    user_id_value = int(user_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE id = ?",
            (user_id_value,),
        ).fetchone()
        user = _row_to_dict(row)
        if not user:
            raise ValueError("user not found")
        if user.get("role") != "customer":
            raise ValueError("only customer accounts can be deleted")

        conn.execute("DELETE FROM customer_prices WHERE user_id = ?", (user_id_value,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id_value,))
    return user


