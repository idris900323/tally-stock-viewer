import os
import logging

from config import Config
from database import add_images_batch, get_all_images_with_link_status, get_stock_item_names_for_images


IMAGE_EXTENSIONS = set(Config.ALLOWED_IMAGE_EXTENSIONS)
IMAGE_SCAN_BATCH_SIZE = 100
logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# If more than this fraction of the catalog appears missing, treat it as
# suspicious (disconnected network drive, renamed folder) rather than a
# genuine mass deletion -- but only once the absolute count is large enough
# that a small catalog's naturally noisy percentage doesn't trip it (e.g. 1
# missing file out of 4 total is 25%, and clearly not a disconnected drive).
MISSING_IMAGE_WARNING_RATIO = 0.15
MISSING_IMAGE_WARNING_MIN_COUNT = 20


def _resolve_base_path(base_path):
    if os.path.isabs(base_path):
        return base_path
    return os.path.abspath(os.path.join(os.path.dirname(__file__), base_path))


def _car_folder_from_root(base_path, root):
    relative_root = os.path.relpath(root, base_path)
    if relative_root in (".", ""):
        return os.path.basename(base_path)
    return relative_root.split(os.sep, 1)[0]


def _relative_image_path(base_path, full_path):
    relative_path = os.path.relpath(full_path, base_path).replace("\\", "/")
    normalized = os.path.normpath(relative_path).replace("\\", "/")
    if normalized in (".", ""):
        return os.path.basename(full_path)
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"unsafe relative path outside image root: {full_path}")
    return normalized.lstrip("/")


def scan_ss_image_folder(base_path=None):
    if base_path is None:
        base_path = os.path.join(BASE_DIR, "data", "S.S IMAGE")
    resolved_base_path = _resolve_base_path(base_path)

    if not os.path.exists(resolved_base_path):
        return {
            "base_path": resolved_base_path,
            "total_folders": 0,
            "total_images": 0,
            "scanned": 0,
        }

    folder_names = set()
    total_images = 0
    batch_records = []

    for root, _, files in os.walk(resolved_base_path):
        car_folder = _car_folder_from_root(resolved_base_path, root)
        for filename in files:
            extension = os.path.splitext(filename)[1].lower()
            if extension not in IMAGE_EXTENSIONS:
                continue

            full_path = os.path.join(root, filename)
            if not os.path.exists(full_path) or not os.access(full_path, os.R_OK):
                logger.warning("Skipping unreadable image file: %s", full_path)
                continue

            try:
                if os.path.getsize(full_path) > Config.MAX_IMAGE_SIZE:
                    logger.warning("Skipping oversized image file: %s", full_path)
                    continue
            except OSError:
                logger.warning("Skipping inaccessible image file: %s", full_path)
                continue

            image_name = filename
            try:
                relative_path = _relative_image_path(resolved_base_path, full_path)
            except ValueError:
                logger.warning("Skipping unsafe image path outside root: %s", full_path)
                continue

            batch_records.append((car_folder, image_name, relative_path))
            if len(batch_records) >= IMAGE_SCAN_BATCH_SIZE:
                total_images += add_images_batch(batch_records)
                batch_records.clear()

            folder_names.add(car_folder)

    if batch_records:
        total_images += add_images_batch(batch_records)
        batch_records.clear()

    return {
        "base_path": resolved_base_path,
        "total_folders": len(folder_names),
        "total_images": total_images,
        "scanned": total_images,
    }


def _resolve_stored_path(base_path, stored_filepath):
    value = str(stored_filepath or "").strip().replace("\\", "/")
    if not value:
        return None
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(base_path, value))


def find_missing_image_rows(base_path=None):
    """Find image rows whose file no longer exists on disk under base_path.

    Read-only: does not delete or modify anything. Mirrors the safety-first,
    list-before-delete pattern used by database.find_duplicate_image_rows()
    -- detection is a separate step from any removal, so nothing here ever
    touches the database.

    Includes an over_threshold_warning: if the missing count looks like it
    could be caused by base_path itself being temporarily unreachable (a
    disconnected network drive, a renamed folder) rather than genuine
    deletions, every stored row would resolve as "missing" at once -- this
    flags that scenario instead of silently reporting a huge missing count.
    """
    if base_path is None:
        base_path = os.path.join(BASE_DIR, "data", "S.S IMAGE")
    resolved_base_path = _resolve_base_path(base_path)

    all_images = get_all_images_with_link_status()
    total_images = len(all_images)

    missing_rows = []
    for row in all_images:
        full_path = _resolve_stored_path(resolved_base_path, row.get("filepath"))
        if full_path is None or not os.path.exists(full_path):
            missing_rows.append(row)

    missing_count = len(missing_rows)
    missing_mapped_count = sum(1 for row in missing_rows if row.get("mapped"))

    # Only looked up for the (usually small) missing subset, not the whole
    # catalog -- so the admin can see which stock item(s) each missing row
    # was linked to without a second full disk rescan.
    stock_items_by_image = get_stock_item_names_for_images(
        [row["id"] for row in missing_rows]
    )
    for row in missing_rows:
        row["stock_item_names"] = stock_items_by_image.get(row["id"], [])

    over_threshold_warning = (
        total_images > 0
        and missing_count >= MISSING_IMAGE_WARNING_MIN_COUNT
        and (missing_count / total_images) > MISSING_IMAGE_WARNING_RATIO
    )

    return {
        "base_path": resolved_base_path,
        "total_images": total_images,
        "missing_count": missing_count,
        "missing_mapped_count": missing_mapped_count,
        "over_threshold_warning": over_threshold_warning,
        "rows": missing_rows,
    }
