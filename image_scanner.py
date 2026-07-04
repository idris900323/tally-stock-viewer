import os
import logging

from config import Config
from database import add_images_batch


IMAGE_EXTENSIONS = set(Config.ALLOWED_IMAGE_EXTENSIONS)
IMAGE_SCAN_BATCH_SIZE = 100
logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


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
