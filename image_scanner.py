import os
import logging

from config import Config
from database import add_images_batch, init_database


IMAGE_EXTENSIONS = set(Config.ALLOWED_IMAGE_EXTENSIONS)
IMAGE_SCAN_BATCH_SIZE = 100
logger = logging.getLogger(__name__)


def _resolve_base_path(base_path):
    if os.path.isabs(base_path):
        return base_path
    return os.path.abspath(os.path.join(os.path.dirname(__file__), base_path))


def _car_folder_from_root(base_path, root):
    relative_root = os.path.relpath(root, base_path)
    if relative_root in (".", ""):
        return os.path.basename(base_path)
    return relative_root.split(os.sep, 1)[0]


def scan_ss_image_folder(base_path="data/S.S IMAGE"):
    init_database()
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

            image_name = os.path.splitext(filename)[0]
            batch_records.append((car_folder, image_name, full_path))
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
