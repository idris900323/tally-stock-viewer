from flask import Flask, jsonify, request, render_template, send_file, Response, session, redirect, url_for
import json
import os
import requests
import xml.etree.ElementTree as ET
import re
import threading
import time
import glob
import logging
import psutil
import pandas as pd
from collections import defaultdict
from io import BytesIO
import openpyxl
from datetime import datetime
from functools import wraps
from logging.handlers import RotatingFileHandler
import zipfile
from urllib.parse import quote

import database as db
import image_scanner
import matcher
from api.search import search_bp, set_search_dependencies
from config import Config
from tally.sync import fetch_from_tally_with_retry
from utils.excel_helpers import load_excel_column, load_excel_rows
from utils.normalize import extract_car_base_name, normalize_text

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = app.config["SECRET_KEY"]
app.register_blueprint(search_bp)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _configure_logging():
    log_dir = app.config["LOG_DIR"]
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(BASE_DIR, log_dir)
    os.makedirs(log_dir, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    root = logging.getLogger()
    root.setLevel(getattr(logging, app.config["LOG_LEVEL"], logging.INFO))
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        log_file = app.config["LOG_FILE"]
        if not os.path.isabs(log_file):
            log_file = os.path.join(BASE_DIR, log_file)
        file_handler = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=5)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


_configure_logging()
logger = logging.getLogger(__name__)

TALLY_HTTP_SESSION = requests.Session()
EXPORT_LOCK = threading.Lock()
FULL_REFRESH_LOCK = threading.Lock()
full_refresh_status = {
    "running": False,
    "stage": None,
    "error": None,
    "timestamp": None,
}
LOGIN_ATTEMPTS = {}

PROJECT_ROOT = BASE_DIR
PROJECT_IMAGE_ROOT = os.path.join(BASE_DIR, "data", "S.S IMAGE")
IMAGE_SCAN_ROOT = os.path.abspath(
    os.environ.get("IMAGE_SCAN_ROOT", PROJECT_IMAGE_ROOT)
)
PLACEHOLDER_SVG = b"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 320 320' role='img' aria-label='No image available'>
<rect width='320' height='320' rx='24' fill='#eef2f7'/>
<rect x='54' y='54' width='212' height='212' rx='18' fill='#d9e2ec'/>
<path d='M84 210l46-46 36 36 26-26 44 44H84z' fill='#8da2b8'/>
<circle cx='121' cy='122' r='20' fill='#8da2b8'/>
<text x='160' y='286' text-anchor='middle' font-family='Arial, sans-serif' font-size='20' fill='#52616b'>No image mapped</text>
</svg>"""

# file locations and polling configuration

CAR_FILE = os.path.join(BASE_DIR, "data", "car master list.xls")  # dropdown list
CAR_MASTER_CACHE_JSON = "data/car_master.json"
MAIN_FILE_CANDIDATES = [
    os.path.join(BASE_DIR, "data", "main.xlsx"),
    os.path.join(BASE_DIR, "data", "main.xls"),
    os.path.join(BASE_DIR, "main.xlsx"),
    os.path.join(BASE_DIR, "main.xls"),
]  # parent-child hierarchy (never overwritten)
MAIN_HIERARCHY_CACHE_JSON = "data/main_hierarchy.json"
ITEM_STOCK_FILE = os.path.join(BASE_DIR, "data", "item stock list.xls")  # flat stock item export (quantities only)
ITEM_STOCK_FILE_XLSX = os.path.join(BASE_DIR, "data", "item stock list.xlsx")  # preferred auto-export target
ITEM_STOCK_FILE_AUTO = os.path.join(BASE_DIR, "data", "item stock list.auto.xlsx")  # runtime auto-export target
ITEM_STOCK_CACHE_JSON = os.path.join(BASE_DIR, "data", "item stock list.auto.json")
ITEM_EXPORT_INTERVAL = app.config["TALLY_EXPORT_INTERVAL"]
item_export_timer = None
item_export_enabled = os.environ.get("AUTO_EXPORT_ITEM", "1").strip().lower() not in ("0", "false")
MAX_IMAGE_RESPONSE_LIMIT = max(1, int(app.config["MAX_IMAGE_RESPONSE_LIMIT"]))

# Configuration for automatic polling of Tally
TALLY_URL = app.config["TALLY_URL"]
TALLY_TIMEOUT = max(120, int(app.config["TALLY_TIMEOUT"]))
TALLY_RETRY_ATTEMPTS = app.config["TALLY_RETRY_ATTEMPTS"]
MAX_RETRY_ATTEMPTS = 3
VALID_ROLES = {"admin", "customer"}
PUBLIC_ENDPOINTS = {"login", "logout", "static", "full_refresh_status_route"}

db.init_database()


def _current_role():
    role = session.get("role")
    return role if role in VALID_ROLES else None


def _current_user_id():
    user_id = session.get("user_id")
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def _safe_next_url(raw_next):
    from urllib.parse import urlparse

    if not raw_next or not isinstance(raw_next, str):
        return url_for("home")

    parsed = urlparse(raw_next)
    if parsed.netloc or parsed.scheme:
        return url_for("home")
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return url_for("home")

    return raw_next


def _admin_denied():
    return jsonify({"error": "admin access required"}), 403


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if _current_role() != "admin":
            return _admin_denied()
        return view_func(*args, **kwargs)

    return wrapper


@app.before_request
def require_login():
    session.permanent = True
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if _current_role() is None:
        if request.method == "GET":
            return render_template(
                "login.html",
                error=None,
                next_url=request.path or url_for("home"),
            ), 401
        return jsonify({"error": "login required"}), 401


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


@app.context_processor
def inject_session_context():
    role = _current_role()
    return {
        "current_role": role,
        "current_user_id": _current_user_id(),
        "current_username": session.get("username", ""),
        "is_admin": role == "admin",
        "is_customer": role == "customer",
        "is_viewer": role == "customer",
    }


def _normalize_text(text):
    if text is None:
        return ""
    return normalize_text(str(text)).strip()


def _normalize_lookup_key(text):
    return _normalize_text(text).lower()


def _sanitize_tally_xml(text):
    import re
    text = re.sub(u"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

    def _strip_invalid_ref(match):
        try:
            code = int(match.group(1))
        except ValueError:
            return ""
        if code in (0x9, 0xA, 0xD) or (0x20 <= code <= 0xD7FF) or (0xE000 <= code <= 0xFFFD):
            return match.group(0)
        return ""

    text = re.sub(r"&#(\d+);", _strip_invalid_ref, text)
    return text


def _file_fingerprint(file_path):
    if not file_path or not os.path.exists(file_path):
        return None
    stats = os.stat(file_path)
    return (os.path.abspath(file_path), stats.st_mtime_ns, stats.st_size)


def _check_multiple_tally_instances():
    count = 0
    for process in psutil.process_iter(["name", "exe"]):
        try:
            names = []
            process_name = str(process.info.get("name") or "").strip().lower()
            if process_name:
                names.append(process_name)
                if process_name.endswith(".exe"):
                    names.append(process_name[:-4])

            process_exe = str(process.info.get("exe") or "").strip().lower()
            if process_exe:
                exe_name = os.path.basename(process_exe)
                if exe_name:
                    names.append(exe_name)
                    if exe_name.endswith(".exe"):
                        names.append(exe_name[:-4])

            if "tally.exe" in names or "tally" in names:
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return count


# Runtime caches used by hot endpoints.
STOCK_ITEMS_CACHE = []
MAIN_ROWS_CACHE = {"fingerprint": None, "rows": [], "exact_index": {}}
STOCK_QTY_CACHE = {"fingerprint": None, "qty_map": {}}
PARENT_NAME_SET = set()
DATA_CACHE_LOCK = threading.Lock()


def _invalidate_runtime_caches():
    global STOCK_ITEMS_CACHE, MAIN_ROWS_CACHE, STOCK_QTY_CACHE
    STOCK_ITEMS_CACHE = []
    MAIN_ROWS_CACHE = {"fingerprint": None, "rows": [], "exact_index": {}}
    STOCK_QTY_CACHE = {"fingerprint": None, "qty_map": {}}


def _all_stock_items():
    global STOCK_ITEMS_CACHE
    if STOCK_ITEMS_CACHE:
        return STOCK_ITEMS_CACHE

    items = []
    seen = set()
    for car_model, designs in CAR_DESIGN_MAP.items():
        for design in designs:
            stock_item = design.get("raw") or design.get("design")
            if not stock_item:
                continue
            normalized = _normalize_text(stock_item)
            if normalized in seen:
                continue
            seen.add(normalized)
            items.append({
                "car_model": car_model,
                "stock_item_name": stock_item,
                "qty": design.get("qty", 0),
            })
    STOCK_ITEMS_CACHE = items
    return STOCK_ITEMS_CACHE


def _build_design_payload(designs):
    stock_item_names = []
    seen = set()
    for item in designs or []:
        stock_item_name = item.get("design") or item.get("raw") or "Unknown"
        lookup_key = _normalize_lookup_key(stock_item_name)
        if lookup_key and lookup_key not in seen:
            seen.add(lookup_key)
            stock_item_names.append(stock_item_name)

    mapping_lookup = db.get_mappings_for_stock_items(stock_item_names)
    payload = []
    for item in designs or []:
        stock_item_name = item.get("design") or item.get("raw") or "Unknown"
        mapping = mapping_lookup.get(_normalize_lookup_key(stock_item_name))
        enriched_item = dict(item)
        if mapping and mapping.get("image_id"):
            enriched_item.update({
                "mapped": True,
                "image_id": mapping["image_id"],
                "thumbnail_url": f"/get_image/{mapping['image_id']}",
                "confidence": mapping.get("confidence", 1.0),
            })
        else:
            enriched_item.update({
                "mapped": False,
                "image_id": None,
                "thumbnail_url": f"/get_stock_image?stock_item={quote(stock_item_name)}",
                "confidence": 0.0,
            })
        enriched_item["fix_url"] = f"/train?stock_item={quote(stock_item_name)}"
        payload.append(enriched_item)
    return payload


def _placeholder_response():
    return Response(PLACEHOLDER_SVG, mimetype="image/svg+xml")


def _resolve_car_model_hint(image_record):
    if not image_record:
        return None

    folder_name = image_record.get("car_folder") or ""
    folder_mapping = db.get_folder_car_model(folder_name)
    if folder_mapping:
        return folder_mapping.get("car_model_name")

    folder_norm = _normalize_text(folder_name)
    for car_model in CAR_GROUPS:
        car_norm = _normalize_text(car_model)
        if car_norm and (car_norm in folder_norm or folder_norm in car_norm):
            return car_model

    return None


def _load_training_stock_qty_map():
    json_file = ITEM_STOCK_CACHE_JSON
    if os.path.exists(json_file):
        try:
            with open(json_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            qty_map = {}
            for row in payload if isinstance(payload, list) else []:
                item_name = str(row.get("item_name") or "").strip()
                try:
                    qty = int(row.get("qty") or 0)
                except (TypeError, ValueError):
                    continue
                if item_name and qty > 0:
                    qty_map[_normalize_text(item_name)] = qty
            if qty_map:
                with DATA_CACHE_LOCK:
                    STOCK_QTY_CACHE["fingerprint"] = _file_fingerprint(json_file)
                    STOCK_QTY_CACHE["qty_map"] = qty_map
                return qty_map
        except Exception:
            pass

    stock_file = get_latest_stock_file_path()
    fingerprint = _file_fingerprint(stock_file)
    if fingerprint is None:
        return {}

    with DATA_CACHE_LOCK:
        if STOCK_QTY_CACHE["fingerprint"] == fingerprint:
            return STOCK_QTY_CACHE["qty_map"]

    qty_map = {}
    try:
        rows = load_excel_rows(stock_file, usecols=[0, 1], min_row=2)
        for row in rows:
            item_name = str(row[0]).strip() if row and row[0] not in (None, "") else ""
            qty_val = row[1] if len(row) > 1 else None
            if not item_name or qty_val in (None, ""):
                continue
            try:
                qty = int(float(qty_val))
            except (ValueError, TypeError):
                continue
            if qty <= 0:
                continue
            qty_map[_normalize_text(item_name)] = qty
    except Exception:
        qty_map = {}

    with DATA_CACHE_LOCK:
        STOCK_QTY_CACHE["fingerprint"] = fingerprint
        STOCK_QTY_CACHE["qty_map"] = qty_map
    return qty_map


def _load_training_hierarchy_items():
    qty_map = _load_training_stock_qty_map()
    items = []

    if os.path.exists(MAIN_HIERARCHY_CACHE_JSON):
        try:
            with open(MAIN_HIERARCHY_CACHE_JSON, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            for entry in payload if isinstance(payload, list) else []:
                parent_name = str(entry.get("parent") or "").strip()
                children = entry.get("children", [])
                if not parent_name or not isinstance(children, list):
                    continue
                for child in children:
                    item_name = str(child.get("item_name") or "").strip()
                    if not item_name:
                        continue
                    items.append({
                        "car_model": parent_name,
                        "stock_item_name": item_name,
                        "qty": qty_map.get(_normalize_text(item_name), 0),
                    })
            if items:
                return items
        except Exception:
            pass

    main_file = get_main_file_path()
    if not os.path.exists(main_file):
        return []

    parent_names = {
        _normalize_text(name)
        for name in _load_car_groups_from_cache_or_excel()
        if str(name).strip()
    }
    ignored_labels = {"PARTICULARS", "STOCK SUMMARY", "CLOSING BALANCE", "QUANTITY", ""}
    current_parent = None

    try:
        rows = load_excel_rows(main_file, usecols=[0, 1], min_row=1)
    except Exception:
        return []

    for row in rows:
        item_name = str(row[0]).strip() if row and row[0] not in (None, "") else ""
        if not item_name:
            continue

        item_upper = _normalize_text(item_name)
        if item_upper in ignored_labels:
            continue

        if item_upper in parent_names:
            current_parent = item_name
            continue

        if current_parent is not None:
            items.append({
                "car_model": current_parent,
                "stock_item_name": item_name,
                "qty": qty_map.get(item_upper, 0),
            })

    return items


def get_stock_items_for_training_from_hierarchy(car_full_name):
    if not os.path.exists(MAIN_HIERARCHY_CACHE_JSON):
        return None

    try:
        with open(MAIN_HIERARCHY_CACHE_JSON, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None

    car_upper = _normalize_text(car_full_name)
    qty_map = _load_training_stock_qty_map()
    for entry in payload if isinstance(payload, list) else []:
        parent_name = str(entry.get("parent") or "").strip()
        if _normalize_text(parent_name) != car_upper:
            continue

        children = entry.get("children", [])
        if not isinstance(children, list):
            children = []

        return [
            {
                "car_model": parent_name,
                "stock_item_name": item_name,
                "qty": qty_map.get(_normalize_text(item_name), 0),
            }
            for child in children
            for item_name in [str(child.get("item_name") or "").strip()]
            if item_name
        ]

    return None


def get_stock_items_for_training(car_full_name):
    base_name = extract_car_base_name(car_full_name)
    base_norm = _normalize_text(base_name)
    full_norm = _normalize_text(car_full_name)
    exact_car_norm = _normalize_text(car_full_name)

    items = _load_training_hierarchy_items()
    if not base_norm:
        return items

    exact_items = [
        item for item in items
        if _normalize_text(item.get("car_model")) == exact_car_norm
    ]
    if exact_items:
        return sorted(exact_items, key=lambda item: _normalize_text(item.get("stock_item_name")))

    year_tokens = set(re.findall(r"\b(19\d{2}|20\d{2})\b", full_norm))
    short_year_tokens = {token[-2:] for token in year_tokens}
    variant_tokens = set(re.findall(r"\(([^)]+)\)", full_norm))

    variant_identifiers = set()
    for token in variant_tokens:
        for piece in re.split(r"[^A-Z0-9]+", token):
            piece = piece.strip()
            if not piece or len(piece) < 2:
                continue
            variant_identifiers.add(piece)

    specific_identifiers = set(year_tokens) | short_year_tokens | variant_identifiers

    scored = []
    seen = set()
    for item in items:
        car_model_norm = _normalize_text(item.get("car_model"))
        stock_item_norm = _normalize_text(item.get("stock_item_name"))

        if base_norm not in car_model_norm and base_norm not in stock_item_norm:
            continue

        key = (car_model_norm, stock_item_norm)
        if key in seen:
            continue
        seen.add(key)

        if specific_identifiers:
            matched_identifiers = [token for token in specific_identifiers if token in stock_item_norm]
            if not matched_identifiers:
                continue
            score = len(matched_identifiers)
        else:
            score = 1

        scored.append((score, item))

    if scored:
        scored.sort(key=lambda pair: (-pair[0], _normalize_text(pair[1].get("stock_item_name"))))
        return [pair[1] for pair in scored]

    fallback = []
    fallback_token = next((token for token in base_norm.split() if len(token) > 2), "")
    if not fallback_token:
        return fallback

    for item in items:
        car_model_norm = _normalize_text(item.get("car_model"))
        stock_item_norm = _normalize_text(item.get("stock_item_name"))
        if fallback_token in car_model_norm or fallback_token in stock_item_norm:
            fallback.append(item)

    return fallback


def _check_login_rate_limit(username):
    now = time.time()
    key = str(username or "").strip().lower()
    window_seconds = 300
    max_attempts = 10
    if len(LOGIN_ATTEMPTS) > 500:
        for existing_key in list(LOGIN_ATTEMPTS.keys()):
            existing_attempts = [ts for ts in LOGIN_ATTEMPTS.get(existing_key, []) if now - ts < window_seconds]
            LOGIN_ATTEMPTS[existing_key] = existing_attempts
            if not existing_attempts:
                del LOGIN_ATTEMPTS[existing_key]

    attempts = LOGIN_ATTEMPTS.get(key, [])
    attempts = [ts for ts in attempts if now - ts < window_seconds]
    LOGIN_ATTEMPTS[key] = attempts
    if not attempts:
        del LOGIN_ATTEMPTS[key]
    if len(attempts) >= max_attempts:
        return False
    attempts.append(now)
    LOGIN_ATTEMPTS[key] = attempts
    return True


def _post_tally_with_retry(xml_req):
    response = fetch_from_tally_with_retry(
        TALLY_HTTP_SESSION,
        TALLY_URL,
        xml_req,
        timeout=TALLY_TIMEOUT,
        max_retries=TALLY_RETRY_ATTEMPTS,
        logger=logger,
    )
    return response.text


def _classify_tally_exception(exc):
    message = str(exc or "").lower()
    if isinstance(exc, requests.Timeout) or "timed out" in message:
        return "TALLY_TIMEOUT"
    if isinstance(exc, requests.ConnectionError) or "connection refused" in message or "failed to establish a new connection" in message or "max retries exceeded" in message:
        return "TALLY_UNREACHABLE"
    return "TALLY_ERROR"


def get_main_file_path():
    for path in MAIN_FILE_CANDIDATES:
        if os.path.exists(path):
            return path
    return MAIN_FILE_CANDIDATES[0]


def get_latest_stock_file_path():
    candidates = [
        ITEM_STOCK_FILE_AUTO,
        ITEM_STOCK_FILE_XLSX,
        ITEM_STOCK_FILE,
    ]
    candidates.extend(glob.glob(os.path.join(BASE_DIR, "data", "item stock list.auto.*.xlsx")))
    existing = [
        path for path in candidates
        if os.path.exists(path)
        and not str(path).lower().endswith(".tmp.xlsx")
    ]
    if not existing:
        return None
    return max(existing, key=lambda path: os.path.getmtime(path))


def _scan_images_on_startup():
    try:
        if not app.config["INITIAL_IMAGE_SCAN"]:
            logger.info("Initial image scan disabled by INITIAL_IMAGE_SCAN")
            return
        if not os.path.exists(IMAGE_SCAN_ROOT):
            return
        result = image_scanner.scan_ss_image_folder(IMAGE_SCAN_ROOT)
        print(f"scanned image folder: {result['total_images']} images from {result['total_folders']} folders")
    except Exception as exc:
        print("warning: initial image scan skipped:", exc)


STARTUP_TASKS_STARTED = False

def start_background_startup_tasks():
    global STARTUP_TASKS_STARTED
    if STARTUP_TASKS_STARTED:
        return
    STARTUP_TASKS_STARTED = True

    def _startup_routine():
        try:
            main_file = get_main_file_path()
            if os.path.exists(main_file):
                print("Starting background data load...")
                load_data(refresh_first=False)
            else:
                logger.warning("Skipping background data load because main hierarchy file is missing")
        except Exception:
            logger.exception("Background data load failed")

        try:
            _scan_images_on_startup()
        except Exception:
            logger.exception("Background image scan failed")

        if item_export_enabled:
            schedule_item_export(initial_delay=10)

    thread = threading.Thread(target=_startup_routine, daemon=True)
    thread.start()

    def _startup_full_refresh():
        time.sleep(45)  # give Tally and the app time to be ready
        if FULL_REFRESH_LOCK.acquire(blocking=False):
            try:
                logger.info("Running automatic full refresh on startup")
                run_full_refresh_job()
            finally:
                FULL_REFRESH_LOCK.release()
        else:
            logger.info("Skipping startup full refresh — already running")

    threading.Thread(target=_startup_full_refresh, daemon=True).start()


def _refresh_stock_data():
    global last_refresh_status
    if FULL_REFRESH_LOCK.locked():
        now = datetime.now()
        return {
            "ok": False,
            "busy": True,
            "tally_online": None,
            "status": "full_refresh_in_progress",
            "message": "Full refresh is in progress. Please wait.",
            "timestamp": now.isoformat(),
            "formatted": now.strftime("%d/%m/%Y %H:%M:%S"),
        }
    try:
        export_result = fetch_item_stock_flat()
        load_data()
        msg = "Stock updated successfully"
        if export_result and export_result.get("warning"):
            msg = f"{msg} ({export_result.get('warning')})"
        now = datetime.now()
        last_refresh_status = {
            "success": True,
            "message": msg,
            "timestamp": now.isoformat(),
        }
        timestamp_iso = now.isoformat()
        return {
            "ok": True,
            "tally_online": True,
            "status": "stock updated",
            "message": msg,
            "file": export_result.get("file") if export_result else None,
            "warning": export_result.get("warning") if export_result else None,
            "timestamp": timestamp_iso,
            "formatted": now.strftime("%d/%m/%Y %H:%M:%S"),
        }
    except Exception as exc:
        raw_error = str(exc)
        error_code = _classify_tally_exception(exc)
        if error_code == "TALLY_UNREACHABLE":
            fallback_message = f"Tally unreachable at {TALLY_URL}; using the last saved upload. ({raw_error})"
        elif error_code == "TALLY_TIMEOUT":
            fallback_message = f"Tally request timed out; using the last saved upload. ({raw_error})"
        else:
            fallback_message = f"Tally error; using the last saved upload. ({raw_error})"

        now = datetime.now()
        last_refresh_status = {
            "success": False,
            "message": fallback_message,
            "timestamp": now.isoformat(),
            "error_code": error_code,
            "raw_error": raw_error,
        }
        timestamp_iso = now.isoformat()
        return {
            "ok": True,
            "tally_online": False,
            "status": "using_last_saved_upload",
            "message": fallback_message,
            "error_code": error_code,
            "raw_error": raw_error,
            "file": get_latest_stock_file_path(),
            "warning": fallback_message,
            "timestamp": timestamp_iso,
            "formatted": now.strftime("%d/%m/%Y %H:%M:%S"),
                }

# ---------- Tally export logic ----------

def fetch_item_stock_flat():
    """Export item stock from Tally Stock Summary and save as clean Excel.

    This avoids writing Tally XML error text into the spreadsheet file.
    """
    tally_instance_count = _check_multiple_tally_instances()
    if tally_instance_count > 1:
        raise Exception(
            f"Multiple Tally instances detected ({tally_instance_count} running). "
            "Close duplicate Tally windows and keep only one open, then try again."
        )

    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).upper()

    def _fetch_stock_item_master_names():
        xml_req = '''<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>List of Stock Items</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>'''.strip()

        text = _post_tally_with_retry(xml_req)
        if "<LINEERROR>" in text or "<RESPONSE>Error" in text or "Unknown Request" in text:
            raise Exception(f"Tally returned error for stock item collection: {text[:300]}")

        text = _sanitize_tally_xml(text)
        root = ET.fromstring(text)
        master_names = set()
        for item_node in root.findall('.//COLLECTION/STOCKITEM'):
            raw_name = item_node.attrib.get('NAME', '')
            if not raw_name:
                name_node = item_node.find('.//NAME')
                raw_name = name_node.text if (name_node is not None and name_node.text) else ''
            norm_name = _norm(raw_name)
            if norm_name:
                master_names.add(norm_name)
        return master_names

    def _fetch_rows(detailed: bool):
        detailed_vars = """
                <SVSTOCKGROUP>Primary</SVSTOCKGROUP>
                <ISDETAILED>Yes</ISDETAILED>
                <EXPLODEFLAG>Yes</EXPLODEFLAG>
                <SVSHOWALLITEMS>Yes</SVSHOWALLITEMS>
                <SVSHOWZEROBALANCES>Yes</SVSHOWZEROBALANCES>
        """ if detailed else ""

        xml_req = f'''<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Stock Summary</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
{detailed_vars}
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>'''.strip()

        text = _post_tally_with_retry(xml_req)
        if "<LINEERROR>" in text or "<RESPONSE>Error" in text or "Unknown Request" in text:
            raise Exception(f"Tally returned error: {text[:300]}")

        text = _sanitize_tally_xml(text)
        root = ET.fromstring(text)
        names = root.findall(".//DSPACCNAME")
        stocks = root.findall(".//DSPSTKINFO")

        parsed_rows = []
        for name_node, stock_node in zip(names, stocks):
            display_node = name_node.find("DSPDISPNAME")
            qty_node = stock_node.find(".//DSPCLQTY")
            item_name = display_node.text.strip() if (display_node is not None and display_node.text) else ""
            qty_text = qty_node.text if (qty_node is not None and qty_node.text) else "0"
            match = re.search(r"-?\d+", qty_text)
            qty = int(match.group()) if match else 0
            if item_name and qty > 0:
                parsed_rows.append({"item_name": item_name, "qty": qty, "upper_name": _norm(item_name)})
        return parsed_rows

    def _load_main_name_set():
        main_file = get_main_file_path()
        if not os.path.exists(main_file):
            return set()

        try:
            values = load_excel_column(main_file, col_index=0, min_row=1)
        except Exception:
            return set()

        names = set()
        for raw in values:
            normalized = _norm(raw)
            if normalized and normalized not in {"PARTICULARS", "STOCK SUMMARY", "CLOSING BALANCE", "QUANTITY"}:
                names.add(normalized)
        return names

    print("requesting flat item stock list from Tally...")
    try:
        master_name_set = _fetch_stock_item_master_names()
        main_name_set = _load_main_name_set()
        summary_rows = _fetch_rows(detailed=False)
        detailed_rows = _fetch_rows(detailed=True)

        # Primary filter: keep only names that exist in Stock Item master.
        rows = [
            {"item_name": r["item_name"], "qty": r["qty"]}
            for r in detailed_rows
            if r["upper_name"] in master_name_set
        ]

        # Secondary filter: remove obvious group names by subtracting summary rows.
        group_name_set = {r["upper_name"] for r in summary_rows}
        rows = [r for r in rows if _norm(r["item_name"]) not in group_name_set]

        # Optional strict alignment with main hierarchy names, when available.
        if main_name_set:
            aligned_rows = [r for r in rows if _norm(r["item_name"]) in main_name_set]
            if aligned_rows:
                rows = aligned_rows

        # fallback: if strict filtering yields nothing, keep detailed rows as-is
        if not rows:
            rows = [{"item_name": r["item_name"], "qty": r["qty"]} for r in detailed_rows]

        if not rows:
            raise Exception("No stock rows parsed from Tally Stock Summary")

        seen = {}
        for r in rows:
            seen[r["item_name"]] = r
        deduped = list(seen.values())

        try:
            with open(ITEM_STOCK_CACHE_JSON, "w", encoding="utf-8") as handle:
                json.dump(deduped, handle, ensure_ascii=False)
        except Exception:
            pass

        threading.Thread(target=_write_stock_excel, args=(deduped,), daemon=True).start()
        return {"rows": len(deduped), "file": ITEM_STOCK_FILE_AUTO, "warning": None}
    except Exception as exc:
        print(f"item stock export failed: {str(exc)}")
        raise


def fetch_car_master_from_tally():
    tally_instance_count = _check_multiple_tally_instances()
    if tally_instance_count > 1:
        raise Exception(
            f"Multiple Tally instances detected ({tally_instance_count} running). "
            "Close duplicate Tally windows and keep only one open, then try again."
        )

    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).upper()

    xml_req = '''<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>List of Stock Groups</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>'''.strip()

    text = _post_tally_with_retry(xml_req)
    if "<LINEERROR>" in text or "Unknown Request" in text:
        raise Exception(f"Tally returned error for stock group collection: {text[:300]}")

    text = _sanitize_tally_xml(text)
    root = ET.fromstring(text)
    car_names = set()
    for item_node in root.findall('.//COLLECTION/STOCKGROUP'):
        raw_name = item_node.attrib.get('NAME', '')
        if not raw_name:
            name_node = item_node.find('.//NAME')
            raw_name = name_node.text if (name_node is not None and name_node.text) else ''
        norm_name = _norm(raw_name)
        if norm_name:
            car_names.add(norm_name)

    return sorted(car_names)


def _load_car_groups_from_cache_or_excel():
    car_groups = []

    if os.path.exists(CAR_MASTER_CACHE_JSON):
        try:
            with open(CAR_MASTER_CACHE_JSON, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                car_groups = [str(value).strip() for value in payload if str(value).strip()]
        except Exception:
            car_groups = []

    if not car_groups:
        try:
            values = load_excel_column(CAR_FILE, col_index=0, min_row=1)
            car_groups = [str(value).strip() for value in values if str(value).strip()]
        except Exception as exc:
            raise Exception(f"Failed to load car master list: {exc}")

    # ============================================================
    # !! CRITICAL - CAR MODEL RANGE FILTER - READ THIS FIRST !!
    # ============================================================
    # The list of car models loaded from Tally is trimmed to only
    # the range between start_marker and end_marker below.
    #
    # start_marker = "ACCESSORIES"
    # end_marker   = "ZS - EV FOOT MAT"
    #
    # IF THE CAR DROPDOWN IS MISSING MODELS OR SHOWING WRONG CARS:
    #   1. Open Tally Prime
    #   2. Check the Stock Groups list
    #   3. Confirm these two group names still exist exactly as written
    #   4. If either name changed, update start_marker and end_marker here
    #   5. Then run a Full Refresh from the admin UI
    #
    # IF THIS FILTER IS REMOVED, ALL TALLY STOCK GROUPS WILL APPEAR
    # IN THE DROPDOWN INCLUDING INTERNAL/ACCOUNTING GROUPS.
    # ============================================================
    start_marker = "ACCESSORIES"
    end_marker = "ZS - EV FOOT MAT"
    if start_marker in car_groups and end_marker in car_groups:
        start_idx = car_groups.index(start_marker)
        end_idx = car_groups.index(end_marker)
        if start_idx <= end_idx:
            car_groups = car_groups[start_idx : end_idx + 1]
        else:
            car_groups = car_groups[end_idx : start_idx + 1]

    return car_groups


def save_car_master_to_file(car_names):
    temp_file = CAR_FILE + ".tmp.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    for car_name in car_names:
        ws.append([car_name])
    wb.save(temp_file)
    os.replace(temp_file, CAR_FILE)
    with open(CAR_MASTER_CACHE_JSON, "w", encoding="utf-8") as handle:
        json.dump(car_names, handle, ensure_ascii=False)
    print(f"wrote {len(car_names)} car names to {CAR_FILE}")


def fetch_main_hierarchy_from_tally():
    tally_instance_count = _check_multiple_tally_instances()
    if tally_instance_count > 1:
        raise Exception(
            f"Multiple Tally instances detected ({tally_instance_count} running). "
            "Close duplicate Tally windows and keep only one open, then try again."
        )

    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).upper()

    def _node_text(node, tag_name: str) -> str:
        child = node.find(f".//{tag_name}")
        return child.text.strip() if (child is not None and child.text) else ""

    def _load_existing_flat_rows():
        main_file = get_main_file_path()
        if not os.path.exists(main_file):
            return None

        try:
            rows_data = load_excel_rows(main_file, usecols=[0, 1], min_row=1)
        except Exception:
            logger.exception("Failed to read existing main hierarchy for fallback")
            return None

        flat_rows = []
        for row in rows_data:
            item_name = str(row[0]).strip() if row and row[0] not in (None, "") else ""
            qty = row[1] if len(row) > 1 else ""
            qty_text = str(qty or "").strip().upper()
            if item_name.upper() == "PARTICULARS" and qty_text == "QUANTITY":
                continue
            flat_rows.append({"item_name": item_name, "qty": "" if qty is None else qty})
        return flat_rows or None

    def _fallback_existing(reason: str):
        existing_rows = _load_existing_flat_rows()
        if existing_rows:
            logger.warning("%s Keeping existing main hierarchy unchanged.", reason)
            return existing_rows
        raise Exception(reason)

    def _fetch_stock_items_with_parent():
        xml_req = '''<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>List of Stock Items with Parent</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="List of Stock Items with Parent" ISMODIFY="No">
                        <TYPE>StockItem</TYPE>
                        <FETCH>NAME, PARENT</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>'''.strip()

        text = _post_tally_with_retry(xml_req)
        if "<LINEERROR>" in text or "<RESPONSE>Error" in text or "Unknown Request" in text:
            raise Exception(f"TDL collection request failed for stock items with parent: {text[:300]}")

        text = _sanitize_tally_xml(text)
        root = ET.fromstring(text)
        items = []
        for item_node in root.findall(".//COLLECTION/STOCKITEM"):
            item_name = item_node.attrib.get("NAME", "").strip() or _node_text(item_node, "NAME")
            parent_name = item_node.attrib.get("PARENT", "").strip() or _node_text(item_node, "PARENT")
            if item_name and parent_name:
                items.append((item_name, parent_name))
        return items

    def _fetch_current_qty_map():
        xml_req = '''<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Stock Summary</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVSTOCKGROUP>Primary</SVSTOCKGROUP>
                <ISDETAILED>Yes</ISDETAILED>
                <EXPLODEFLAG>Yes</EXPLODEFLAG>
                <SVSHOWALLITEMS>Yes</SVSHOWALLITEMS>
                <SVSHOWZEROBALANCES>Yes</SVSHOWZEROBALANCES>
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>'''.strip()

        text = _post_tally_with_retry(xml_req)
        if "<LINEERROR>" in text or "<RESPONSE>Error" in text or "Unknown Request" in text:
            raise Exception(f"Tally returned error while fetching detailed stock quantities: {text[:300]}")

        text = _sanitize_tally_xml(text)
        root = ET.fromstring(text)
        names = root.findall(".//DSPACCNAME")
        stocks = root.findall(".//DSPSTKINFO")

        qty_map = {}
        for name_node, stock_node in zip(names, stocks):
            display_node = name_node.find("DSPDISPNAME")
            qty_node = stock_node.find(".//DSPCLQTY")
            item_name = display_node.text.strip() if (display_node is not None and display_node.text) else ""
            qty_text = qty_node.text if (qty_node is not None and qty_node.text) else "0"
            match = re.search(r"-?\d+", qty_text)
            qty = int(match.group()) if match else 0
            if item_name and qty > 0:
                qty_map[_norm(item_name)] = qty
        return qty_map

    stock_items = []
    try:
        stock_items = _fetch_stock_items_with_parent()
    except ET.ParseError:
        raise
    except Exception as exc:
        return _fallback_existing(str(exc))

    if not stock_items:
        return _fallback_existing("Stock item master collection returned no items for main hierarchy refresh.")

    qty_map = _fetch_current_qty_map()
    allowed_parents = _load_car_groups_from_cache_or_excel()
    allowed_parent_lookup = {_norm(parent_name): parent_name for parent_name in allowed_parents}

    grouped_items = defaultdict(list)
    for item_name, parent_name in stock_items:
        parent_upper = _norm(parent_name)
        canonical_parent = allowed_parent_lookup.get(parent_upper)
        if canonical_parent:
            grouped_items[canonical_parent].append(item_name)

    if not grouped_items:
        return _fallback_existing("Stock item master collection returned no items in the configured car range.")

    flat_rows = []
    for parent_name in allowed_parents:
        child_names = grouped_items.get(parent_name, [])
        if not child_names:
            continue

        child_rows = []
        parent_qty = 0
        for item_name in child_names:
            qty = qty_map.get(_norm(item_name), 0)
            parent_qty += qty
            child_rows.append({"item_name": item_name, "qty": qty})

        flat_rows.append({"item_name": parent_name, "qty": parent_qty})
        flat_rows.extend(child_rows)
        flat_rows.append({"item_name": "", "qty": ""})

    if not flat_rows:
        return _fallback_existing("No parent-child hierarchy rows were built from the stock item master collection.")

    return flat_rows


def _build_main_hierarchy_structure(flat_rows):
    parent_names = {
        normalize_text(name)
        for name in _load_car_groups_from_cache_or_excel()
        if str(name).strip()
    }
    structured_rows = []
    current_parent = None

    for row in flat_rows or []:
        item_name = str(row.get("item_name") or "").strip()
        qty = row.get("qty") or 0
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = 0

        if not item_name:
            continue

        item_name_upper = normalize_text(item_name)
        if item_name_upper in parent_names:
            current_parent = {
                "parent": item_name,
                "parent_qty": qty,
                "children": [],
            }
            structured_rows.append(current_parent)
            continue

        if current_parent is not None:
            current_parent["children"].append({
                "item_name": item_name,
                "qty": qty,
            })

    return structured_rows


def save_main_hierarchy_to_file(flat_rows):
    main_file = get_main_file_path()
    temp_file = main_file + ".tmp.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["PARTICULARS", "QUANTITY"])
    for row in flat_rows:
        ws.append([row.get("item_name", ""), row.get("qty", 0)])
    wb.save(temp_file)
    os.replace(temp_file, main_file)

    structured_rows = _build_main_hierarchy_structure(flat_rows)
    with open(MAIN_HIERARCHY_CACHE_JSON, "w", encoding="utf-8") as handle:
        json.dump(structured_rows, handle, ensure_ascii=False)

    print(f"wrote {len(flat_rows)} hierarchy rows to {main_file}")


def _write_stock_excel(deduped):
    EXPORT_LOCK.acquire()
    try:
        temp_file = ITEM_STOCK_FILE_AUTO + ".tmp.xlsx"
        out_file = ITEM_STOCK_FILE_AUTO
        warning = None

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["item_name", "qty"])
        for r in deduped:
            ws.append([r["item_name"], r["qty"]])
        wb.save(temp_file)

        replaced = False
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                os.replace(temp_file, ITEM_STOCK_FILE_AUTO)
                replaced = True
                out_file = ITEM_STOCK_FILE_AUTO
                break
            except PermissionError:
                time.sleep(0.1 * (2 ** attempt))
            except OSError as exc:
                if getattr(exc, "winerror", None) == 5:
                    time.sleep(0.1 * (2 ** attempt))
                else:
                    raise

        if not replaced:
            warning = "auto file locked; wrote to alternate file"
            try:
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.append(["item_name", "qty"])
                for r in deduped:
                    ws.append([r["item_name"], r["qty"]])
                wb.save(ITEM_STOCK_FILE_XLSX)
                out_file = ITEM_STOCK_FILE_XLSX
            except Exception:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_file = os.path.join(BASE_DIR, "data", f"item stock list.auto.{timestamp}.xlsx")
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.append(["item_name", "qty"])
                for r in deduped:
                    ws.append([r["item_name"], r["qty"]])
                wb.save(out_file)

        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass

        print(f"exported item stock list to {out_file} ({len(deduped)} rows)")
        if warning:
            print(warning)
        return {"rows": len(deduped), "file": out_file, "warning": warning}
    except Exception:
        logger.exception("background item stock excel write failed")
    finally:
        EXPORT_LOCK.release()


def schedule_item_export(initial_delay: int = 0):
    global item_export_timer, last_refresh_status

    def _export_job():
        global item_export_timer

        if FULL_REFRESH_LOCK.locked():
            logger.info("Skipping scheduled export because full refresh is in progress")
            item_export_timer = threading.Timer(ITEM_EXPORT_INTERVAL, schedule_item_export)
            item_export_timer.daemon = True
            item_export_timer.start()
            return

        if not EXPORT_LOCK.acquire(blocking=False):
            logger.warning("Skipping scheduled export because previous export is still running")
            item_export_timer = threading.Timer(ITEM_EXPORT_INTERVAL, schedule_item_export)
            item_export_timer.daemon = True
            item_export_timer.start()
            return

        try:
            export_result = fetch_item_stock_flat()
            try:
                load_data()
            except Exception:
                pass
            msg = "Stock updated successfully"
            if export_result and export_result.get("warning"):
                msg = f"{msg} ({export_result.get('warning')})"
            last_refresh_status = {
                "success": True,
                "message": msg,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as exc:
            logger.exception("scheduled item stock export failed")
            last_refresh_status = {
                "success": False,
                "message": str(exc),
                "timestamp": datetime.now().isoformat()
            }
        finally:
            EXPORT_LOCK.release()

        item_export_timer = threading.Timer(ITEM_EXPORT_INTERVAL, schedule_item_export)
        item_export_timer.daemon = True
        item_export_timer.start()

    if initial_delay and initial_delay > 0:
        item_export_timer = threading.Timer(initial_delay, _export_job)
        item_export_timer.daemon = True
        item_export_timer.start()
        return

    _export_job()


# refresh status (auto + manual item stock export)
last_refresh_status = {"success": False, "message": "Not yet run", "timestamp": None}

# the actual start of the timer happens later, once all helper functions
# (especially load_data) are defined.  see bottom of this file.

# ----------------------------
# Hierarchical parser for Tally export
# ----------------------------
def parse_flat_tally(file_path):
    """Load all designs from Tally export as a flat list (no hierarchy parsing).
    
    Simply reads all valid rows and returns them as a list for matching to cars.
    
    Returns:
        list: [{"design": str, "raw": str, "qty": int}, ...]
    """
    designs = []
    ignored_labels = {"PARTICULARS", "STOCK SUMMARY", "CLOSING BALANCE", "QUANTITY", ""}

    print(f"Reading Tally rows from {file_path}")
    rows = load_excel_rows(file_path, usecols=[0, 1], min_row=2)
    for raw0, col1_val in rows:
        col0 = str(raw0).strip() if raw0 not in (None, "") else ""
        if not col0 or _normalize_text(col0) in ignored_labels:
            continue

        qty = None
        if col1_val not in (None, ""):
            try:
                qty = int(float(col1_val))
            except (ValueError, TypeError):
                pass

        if qty is not None and qty > 0:
            designs.append({
                "raw": col0,
                "design": col0,
                "qty": qty,
            })

    print(f"Loaded {len(designs)} valid designs")
    return designs



# In-memory cache (module-level globals)
CAR_GROUPS = []
CAR_DESIGN_MAP = {}
TALLY_HIERARCHY = {}  # Store raw Tally hierarchy: {parent_name: [child_items]}


def load_data(refresh_first: bool = False):
    """Populate in-memory caches for dual-file workflow.

    - car master list.xls provides dropdown car models
    - main.xlsx/main.xls provides parent-child hierarchy only
    - item stock list.xls provides live quantities
    """
    global CAR_GROUPS, CAR_DESIGN_MAP, TALLY_HIERARCHY, PARENT_NAME_SET

    CAR_DESIGN_MAP = {}
    CAR_GROUPS = []
    TALLY_HIERARCHY = {}
    PARENT_NAME_SET = set()
    _invalidate_runtime_caches()

    # optionally pull a fresh export from Tally
    if refresh_first:
        try:
            fetch_item_stock_flat()
        except Exception as exc:
            print("warning: could not refresh stock from Tally:", exc)

    # ---- Load car groups from car master list ----
    try:
        CAR_GROUPS = _load_car_groups_from_cache_or_excel()
        PARENT_NAME_SET = {normalize_text(name) for name in CAR_GROUPS if str(name).strip()}
        print(f"[OK] Loaded {len(CAR_GROUPS)} car models from dropdown")
    except Exception as exc:
        print(f"[ERROR] Error loading car master list: {exc}")
        return

    # ---- Load designs from Tally export as flat list ----
    try:
        main_file = get_main_file_path()
        if not os.path.exists(main_file):
            print(f"[ERROR] Main file not found. Tried: {', '.join(MAIN_FILE_CANDIDATES)}")
            print(f"  Designs will be empty until you export from Tally")
            return
        
        # Load all designs from Tally as a flat list
        all_designs = parse_flat_tally(main_file)
        design_index = defaultdict(list)
        for index, design_item in enumerate(all_designs):
            for token in normalize_text(design_item["raw"]).split():
                if len(token) > 2:
                    design_index[token].append(index)

        for car in CAR_GROUPS:
            base = normalize_text(extract_car_base_name(car) or car)
            candidate_indices = set()
            for token in base.split():
                if len(token) > 2:
                    for index in design_index.get(token, []):
                        candidate_indices.add(index)
            CAR_DESIGN_MAP[car] = [all_designs[index] for index in sorted(candidate_indices)]

        total_designs = sum(len(v) for v in CAR_DESIGN_MAP.values())
        print(f"Matched {len(CAR_DESIGN_MAP)} cars to {total_designs} designs")
    
    except Exception as exc:
        print(f"[ERROR] Error loading Tally designs: {exc}")
        import traceback
        traceback.print_exc()


def ensure_data_loaded():
    """Load cached data on demand if startup initialization did not run."""
    if CAR_GROUPS:
        return None

    try:
        load_data(refresh_first=False)
    except Exception as exc:
        return str(exc)

    if not CAR_GROUPS:
        return "No car models loaded from car master list"

    return None



# ----------------------------
# API routes
# ----------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if _current_role() is not None:
            return redirect(url_for("home"))
        return render_template(
            "login.html",
            error=None,
            next_url=_safe_next_url(request.args.get("next")),
        )

    username = (request.form.get("username") or "").strip()
    access_code = (request.form.get("access_code") or "").strip()
    next_url = _safe_next_url(request.form.get("next") or request.args.get("next"))

    if not _check_login_rate_limit(username):
        return render_template(
            "login.html",
            error="Too many login attempts. Please try again later.",
            next_url=next_url,
        ), 429

    user_record = db.authenticate_user(username, access_code)
    if not user_record:
        return render_template(
            "login.html",
            error="Invalid username or access code.",
            next_url=next_url,
        ), 401

    if user_record.get("role") == "customer" and not user_record.get("is_active", 1):
        return render_template(
            "login.html",
            error="This account has been paused. Contact the administrator.",
            next_url=next_url,
        ), 403

    db.update_last_login(user_record["id"])

    session.clear()
    session["user_id"] = user_record["id"]
    session["username"] = user_record["username"]
    session["role"] = user_record["role"]
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
def home():
    # serve the frontend page
    # pass a couple of flags so the UI can indicate whether auto-export is running
    return render_template(
        "index.html",
        auto_export_enabled=item_export_enabled,
        refresh_interval=ITEM_EXPORT_INTERVAL,
        last_refresh_status=last_refresh_status,
    )
@app.route("/health")
def health():
    db_ok = True
    db_error = None
    try:
        stats = db.get_mapping_stats()
    except Exception as exc:
        db_ok = False
        stats = None
        db_error = str(exc)
        logger.exception("Health check database failure")

    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "database": {"ok": db_ok, "error": db_error},
        "tally_url": TALLY_URL,
        "auto_export_enabled": item_export_enabled,
        "mapping_stats": stats,
    }), (200 if db_ok else 503)



@app.route("/cars")
def cars():
    # return list of car models (column A of car master Excel)
    load_error = ensure_data_loaded()
    if load_error:
        return jsonify({"error": load_error}), 500
    print("/cars endpoint called; returning", len(CAR_GROUPS), "models")
    return jsonify(CAR_GROUPS)


def _find_children_by_qty(car_name: str):
    """Search MAIN_FILE for the row matching `car_name` and return all
    subsequent rows until the summed quantity equals the parent's quantity.

    This implements the new strategy proposed by the user:
    > let the option selected from the dropdown be the key/parent,
    > search it in the main excel sheet, beside the total quantity is known,
    > print the lines below it while add the quantities of each of those lines,
    > print until the total quantity matches the quantity of the key.
    """

    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).upper()

    def _live_designs_for_car():
        if car_name in CAR_DESIGN_MAP and CAR_DESIGN_MAP[car_name]:
            return [
                design
                for design in CAR_DESIGN_MAP[car_name]
                if design.get("qty", 0) > 0
            ]
        return []

    if not PARENT_NAME_SET:
        logger.warning("Skipping child lookup because parent data has not loaded yet")
        live_designs = _live_designs_for_car()
        return (car_name in CAR_DESIGN_MAP), live_designs

    def _to_int(value):
        if value is None:
            return 0
        match = re.search(r"-?\d+", str(value))
        return int(match.group()) if match else 0

    def _load_stock_qty_map_cached():
        json_file = ITEM_STOCK_CACHE_JSON
        if os.path.exists(json_file):
            try:
                with open(json_file, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                qty_map = {}
                for row in payload if isinstance(payload, list) else []:
                    item_name = str(row.get("item_name") or "").strip()
                    try:
                        qty = int(row.get("qty") or 0)
                    except (TypeError, ValueError):
                        continue
                    if item_name and qty > 0:
                        qty_map[_norm(item_name)] = qty
                if qty_map:
                    with DATA_CACHE_LOCK:
                        STOCK_QTY_CACHE["fingerprint"] = _file_fingerprint(json_file)
                        STOCK_QTY_CACHE["qty_map"] = qty_map
                    return qty_map
            except Exception:
                pass

        stock_file = get_latest_stock_file_path()
        fingerprint = _file_fingerprint(stock_file)
        if fingerprint is None:
            return {}

        with DATA_CACHE_LOCK:
            if STOCK_QTY_CACHE["fingerprint"] == fingerprint:
                return STOCK_QTY_CACHE["qty_map"]

        qty_map = {}
        try:
            rows = load_excel_rows(stock_file, usecols=[0, 1], min_row=2)
            for row in rows:
                item_name = str(row[0]).strip() if row and row[0] not in (None, "") else ""
                qty_val = row[1] if len(row) > 1 else None
                if not item_name or qty_val in (None, ""):
                    continue
                try:
                    qty = int(float(qty_val))
                except (ValueError, TypeError):
                    continue
                if qty <= 0:
                    continue
                qty_map[_norm(item_name)] = qty
        except Exception:
            qty_map = {}

        with DATA_CACHE_LOCK:
            STOCK_QTY_CACHE["fingerprint"] = fingerprint
            STOCK_QTY_CACHE["qty_map"] = qty_map
        return qty_map

    def _lookup_from_hierarchy_json(car_name: str):
        if not os.path.exists(MAIN_HIERARCHY_CACHE_JSON):
            return False, None

        try:
            with open(MAIN_HIERARCHY_CACHE_JSON, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return False, None

        car_upper = _norm(car_name)
        for entry in payload if isinstance(payload, list) else []:
            parent_name = str(entry.get("parent") or "").strip()
            if _norm(parent_name) != car_upper:
                continue

            _ = _to_int(entry.get("parent_qty", 0))
            children = entry.get("children", [])
            if not isinstance(children, list):
                children = []

            stock_qty_map = _load_stock_qty_map_cached()
            live_children = []
            for child in children:
                item_name = str(child.get("item_name") or "").strip()
                if not item_name:
                    continue
                live_qty = stock_qty_map.get(_norm(item_name), 0)
                if live_qty > 0:
                    live_children.append({"raw": item_name, "design": item_name, "qty": live_qty})
            return True, live_children

        return False, None

    found_in_json, json_children = _lookup_from_hierarchy_json(car_name)
    if found_in_json:
        return True, json_children

    def _load_main_rows_cached():
        main_file = get_main_file_path()
        fingerprint = _file_fingerprint(main_file)
        if fingerprint is None:
            return [], {}

        with DATA_CACHE_LOCK:
            if MAIN_ROWS_CACHE["fingerprint"] == fingerprint:
                return MAIN_ROWS_CACHE["rows"], MAIN_ROWS_CACHE["exact_index"]

        rows = []
        exact_index = {}
        ignore = {"PARTICULARS", "STOCK SUMMARY", "CLOSING BALANCE", "QUANTITY", ""}
        
        try:
            rows_data = load_excel_rows(main_file, usecols=[0, 1], min_row=1)
            for row in rows_data:
                name = str(row[0]).strip() if row and row[0] not in (None, "") else ""
                if not name:
                    continue
                name_upper = _norm(name)
                if name_upper in ignore:
                    continue
                qty = _to_int(row[1] if len(row) > 1 else None)
                rows.append((name, name_upper, qty))
                exact_index.setdefault(name_upper, len(rows) - 1)
        except Exception:
            return [], {}

        with DATA_CACHE_LOCK:
            MAIN_ROWS_CACHE["fingerprint"] = fingerprint
            MAIN_ROWS_CACHE["rows"] = rows
            MAIN_ROWS_CACHE["exact_index"] = exact_index
        return rows, exact_index

    rows, exact_index = _load_main_rows_cached()
    if not rows:
        live_designs = _live_designs_for_car()
        return (car_name in CAR_DESIGN_MAP), live_designs

    parent_idx = None
    car_upper = _norm(car_name)
    matched_parent_upper = ""
    parent_qty_total = 0

    exact_match_idx = exact_index.get(car_upper)
    if exact_match_idx is not None:
        parent_idx = exact_match_idx
        _, matched_parent_upper, parent_qty_total = rows[parent_idx]
    else:
        for idx, (_, name_upper, qty_total) in enumerate(rows):
            if car_upper in name_upper:
                parent_idx = idx
                matched_parent_upper = name_upper
                parent_qty_total = qty_total
                break

    if parent_idx is None:
        live_designs = _live_designs_for_car()
        return (car_name in CAR_DESIGN_MAP), live_designs

    stock_qty_map = _load_stock_qty_map_cached()

    children = []
    running_qty = 0
    for name, upper_name, _ in rows[parent_idx + 1 :]:

        if parent_qty_total > 0 and running_qty >= parent_qty_total:
            break

        # stop at next parent group to keep only this section's child items
        if upper_name in PARENT_NAME_SET:
            break

        # never return parent labels as designs
        if upper_name == matched_parent_upper:
            continue

        # Lookup quantity from item stock list
        qty = stock_qty_map.get(upper_name, 0)
        if qty > 0:
            children.append({"raw": name, "design": name, "qty": qty})
            running_qty += qty

            if parent_qty_total > 0 and running_qty >= parent_qty_total:
                break
    if children:
        return True, children

    live_designs = _live_designs_for_car()
    if live_designs:
        return True, live_designs
    return True, children


def _coerce_limit(value, default=None, default_value=None):
    # Accept both `default` and legacy `default_value` keyword to remain
    # compatible with older call sites.
    if default_value is not None and default is None:
        default = default_value
    try:
        v = int(value) if value is not None else default
        return max(1, min(v, MAX_IMAGE_RESPONSE_LIMIT)) if v is not None else None
    except (TypeError, ValueError):
        return max(1, min(default, MAX_IMAGE_RESPONSE_LIMIT)) if default is not None else None


def _parse_bool(value):
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


@app.route("/designs")
def designs():
    car = request.args.get("car")
    if not car:
        return jsonify([])

    load_error = ensure_data_loaded()
    if load_error:
        return jsonify({"error": load_error}), 500

    # primary strategy: quantity-sum scanning of the export
    found, children = _find_children_by_qty(car)
    if children:
        return jsonify(_build_design_payload(children))

    if found:
        # Car exists in hierarchy but has zero stock — return empty, do not fall back
        return jsonify([])

    # Car not in main hierarchy - check CAR_DESIGN_MAP as fallback
    # but only return designs that have qty > 0 in the stock cache
    if car in CAR_DESIGN_MAP and CAR_DESIGN_MAP[car]:
        live_designs = [
            d for d in CAR_DESIGN_MAP[car]
            if d.get("qty", 0) > 0
        ]
        if live_designs:
            return jsonify(_build_design_payload(live_designs))

    return jsonify([])


@app.route("/api/resolve_car_from_folder")
def resolve_car_from_folder():
    folder = (request.args.get("folder") or "").strip()
    if not folder:
        return jsonify({"car": None})
    # reuse internal hinting logic to map folder names to car models
    hint = _resolve_car_model_hint({"car_folder": folder})
    return jsonify({"car": hint})


@app.route("/admin/accounts")
@admin_required
def admin_accounts():
    status_message = (request.args.get("status") or "").strip()
    error_message = (request.args.get("error") or "").strip()
    return render_template(
        "accounts.html",
        status_message=status_message,
        error_message=error_message,
    )


@app.route("/admin/create_user", methods=["POST"])
@admin_required
def admin_create_user():
    payload = request.get_json(silent=True) or request.form or {}
    username = (payload.get("username") or "").strip()
    access_code = (payload.get("access_code") or "").strip()

    if not username:
        return jsonify({"success": False, "error": "username is required"}), 400
    if not access_code:
        return jsonify({"success": False, "error": "access code is required"}), 400

    try:
        created_user = db.create_customer_user(username, access_code)
    except ValueError as exc:
        message = str(exc)
        status = 409 if "exists" in message.lower() else 400
        return jsonify({"success": False, "error": message}), status

    db.log_account_action(created_user["id"], "created", _current_user_id())

    return jsonify({
        "success": True,
        "message": f"Account '{created_user['username']}' created successfully",
        "user": {
            "id": created_user["id"],
            "username": created_user["username"],
            "role": created_user.get("role", "customer"),
            "status": "active",
            "created_at": created_user.get("created_at"),
        },
    }), 200


@app.route("/admin/get_all_customers")
@admin_required
def admin_get_all_customers():
    customers = db.get_all_customers_with_details()
    return jsonify([
        {
            "id": customer["id"],
            "username": customer["username"],
            "access_code": customer.get("access_code"),
            "status": "active" if customer.get("is_active", 1) else "paused",
            "is_active": bool(customer.get("is_active", 1)),
            "created_at": customer.get("created_at"),
            "last_login": customer.get("last_login"),
        }
        for customer in customers
    ])


@app.route("/admin/toggle_user_status/<int:user_id>", methods=["POST"])
@admin_required
def admin_toggle_user_status(user_id):
    try:
        updated_user = db.toggle_customer_active_status(user_id)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message.lower() else 400
        return jsonify({"success": False, "error": message}), status

    is_active = bool(updated_user.get("is_active"))
    action = "resumed" if is_active else "paused"
    db.log_account_action(user_id, action, _current_user_id())
    return jsonify({
        "success": True,
        "message": f"Account '{updated_user['username']}' {action}",
        "user_id": user_id,
        "is_active": is_active,
    })


@app.route("/admin/set_all_customer_status", methods=["POST"])
@admin_required
def admin_set_all_customer_status():
    payload = request.get_json(silent=True) or request.form or {}
    is_active = bool(payload.get("is_active", True))
    updated_count = db.set_all_customer_active_status(is_active)
    action = "resumed" if is_active else "paused"
    db.log_account_action(None, f"bulk_{action}", _current_user_id())
    return jsonify({
        "success": True,
        "message": f"{updated_count} account{'s' if updated_count != 1 else ''} {action}",
        "is_active": is_active,
        "updated_count": updated_count,
    })


@app.route("/admin/delete_user/<int:user_id>", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    try:
        deleted_user = db.delete_customer_user(user_id)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message.lower() else 400
        return jsonify({"success": False, "error": message}), status

    db.log_account_action(user_id, "deleted", _current_user_id())
    return jsonify({
        "success": True,
        "message": f"Account '{deleted_user['username']}' deleted successfully",
        "user_id": user_id,
    })


@app.route("/train")
def train():
    load_error = ensure_data_loaded()
    if load_error:
        return jsonify({"error": load_error}), 500

    initial_image = None
    image_id = request.args.get("image_id", type=int)
    stock_item = request.args.get("stock_item", default="")

    if image_id:
        initial_image = db.get_image_by_id(image_id)
    if initial_image is None:
        initial_image = db.get_next_unmapped_image()

    return render_template(
        "train.html",
        initial_image=initial_image,
        target_stock_item=stock_item,
        ss_image_folders=db.get_image_folders(),
        mapping_stats=db.get_mapping_stats(),
        selected_role=_current_role(),
        max_image_size=Config.MAX_IMAGE_SIZE,
        allowed_image_extensions=sorted(Config.ALLOWED_IMAGE_EXTENSIONS),
    )


@app.route("/get_unmapped_images")
def get_unmapped_images_route():
    limit = _coerce_limit(request.args.get("limit", default=1, type=int), default_value=1)
    after = request.args.get("after", type=int)

    if after is not None:
        next_image = db.get_next_unmapped_image(after_image_id=after)
        images = [next_image] if next_image else []
    else:
        images = db.get_unmapped_images(limit=limit)

    return jsonify({
        "count": len(images),
        "first_image": images[0] if images else None,
        "images": images,
        "stats": db.get_mapping_stats(),
    })


@app.route("/train_images")
def train_images():
    # When a car folder is selected, return all images for that folder.
    # Without a folder filter, return only unmapped images (legacy behavior).
    folder = request.args.get("folder") or request.args.get("car")
    limit = _coerce_limit(request.args.get("limit", type=int), default_value=MAX_IMAGE_RESPONSE_LIMIT)

    try:
        if folder:
            images = db.get_images_by_folder(folder, limit=limit)
        else:
            images = db.get_unmapped_images(limit=limit if limit is not None else None)
    except Exception:
        # fallback to safe behavior
        images = db.get_unmapped_images(limit=limit if limit is not None else MAX_IMAGE_RESPONSE_LIMIT)

    return jsonify({
        "count": len(images),
        "images": images,
        "stats": db.get_mapping_stats(),
    })


@app.route("/export_images", methods=["POST"])
@admin_required
def export_images():
    payload = request.get_json(silent=True) or request.form or {}
    raw_image_ids = payload.get("image_ids") or []

    if isinstance(raw_image_ids, str):
        raw_image_ids = [item for item in re.split(r"[\s,]+", raw_image_ids) if item]

    image_ids = []
    for value in raw_image_ids:
        try:
            image_ids.append(int(value))
        except (TypeError, ValueError):
            continue

    if not image_ids:
        return jsonify({"error": "image_ids is required"}), 400

    archive_buffer = BytesIO()
    exported_count = 0

    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for image_id in image_ids:
            image_record = db.get_image_by_id(image_id)
            if not image_record:
                continue

            file_path = _resolve_stored_image_path(image_record.get("filepath"))
            if not file_path:
                continue

            car_folder = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(image_record.get("car_folder") or "images")).strip() or "images"
            image_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(image_record.get("filename") or f"image_{image_id}")).strip() or f"image_{image_id}"
            extension = os.path.splitext(file_path)[1] or ".jpg"
            archive_name = f"{car_folder}/{image_id}_{image_name}{extension}"
            archive.write(file_path, arcname=archive_name)
            exported_count += 1

    if exported_count == 0:
        return jsonify({"error": "No image files were available for export"}), 404

    archive_buffer.seek(0)
    return send_file(
        archive_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name="ss-images.zip",
    )


@app.route("/remove_mapping", methods=["POST"])
@admin_required
def remove_mapping():
    payload = request.get_json(silent=True) or request.form or {}
    image_id = None
    try:
        image_id = int(payload.get("image_id"))
    except (TypeError, ValueError):
        image_id = None

    if not image_id:
        return jsonify({"error": "image_id is required"}), 400

    image_record = db.get_image_by_id(image_id)
    if not image_record:
        return jsonify({"error": "image not found"}), 404

    removed = db.remove_mapping_by_image_id(image_id)
    return jsonify({
        "status": "removed" if removed else "no_mapping",
        "removed": bool(removed),
        "image_id": image_id,
        "stats": db.get_mapping_stats(),
    })


def _confirm_mapping_core(image_id, stock_item_name, car_model="", confidence=1.0, confirmed_by="human"):
    """Save image_id -> stock_item_name and return {image_id, next_image, stats}.

    Shared by the manual /confirm_mapping route and the image upload route so
    both go through the exact same save/overwrite behavior.
    """
    image_record = db.get_image_by_id(image_id)
    if not image_record:
        raise ValueError("image not found")

    resolved_car_model = car_model or _resolve_car_model_hint(image_record) or image_record.get("car_folder")
    if stock_item_name not in ("", "__UNMATCHABLE__"):
        db.remove_mappings_for_stock_item(stock_item_name, exclude_image_id=image_id)
    db.add_mapping(image_id, stock_item_name, resolved_car_model, confidence, confirmed_by=confirmed_by)
    if resolved_car_model and confidence >= 1.0 and stock_item_name not in ("", "__UNMATCHABLE__"):
        db.add_folder_mapping(image_record.get("car_folder", ""), resolved_car_model)

    return {
        "image_id": image_id,
        "next_image": db.get_next_unmapped_image(after_image_id=image_id),
        "stats": db.get_mapping_stats(),
    }


@app.route("/confirm_mapping", methods=["POST"])
@admin_required
def confirm_mapping():
    payload = request.get_json(silent=True) or request.form or {}
    image_id = None
    try:
        image_id = int(payload.get("image_id"))
    except Exception:
        image_id = None
    if not image_id:
        return jsonify({"error": "image_id is required"}), 400

    stock_item_name = payload.get("stock_item_name", "")
    confidence = payload.get("confidence", 1.0)
    confirmed_by = payload.get("confirmed_by", "human")
    car_model = payload.get("car_model", "")

    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 1.0

    try:
        result = _confirm_mapping_core(image_id, stock_item_name, car_model=car_model, confidence=confidence, confirmed_by=confirmed_by)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404

    return jsonify({
        "status": "saved",
        "image_id": result["image_id"],
        "next_image": result["next_image"],
        "stats": result["stats"],
    })


def _sanitize_upload_car_folder(raw_name):
    value = str(raw_name or "").strip()
    if not value:
        raise ValueError("Car folder name is required")
    if "\x00" in value:
        raise ValueError("Car folder name contains invalid characters")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError("Car folder name cannot contain path separators")
    return value


def _sanitize_upload_filename(original_name):
    base = os.path.basename(str(original_name or "").replace("\\", "/")).strip()
    return base or "upload"


def _unique_filename_in_dir(target_dir, base_name):
    name_part, ext_part = os.path.splitext(base_name)
    candidate = base_name
    counter = 1
    while os.path.exists(os.path.join(target_dir, candidate)):
        counter += 1
        candidate = f"{name_part} ({counter}){ext_part}"
    return candidate


@app.route("/admin/upload_image", methods=["POST"])
@admin_required
def admin_upload_image():
    uploaded_file = request.files.get("file")
    stock_item_name = (request.form.get("stock_item") or "").strip()

    if not uploaded_file or not uploaded_file.filename:
        return jsonify({"success": False, "error": "No file was uploaded"}), 400
    if not stock_item_name:
        return jsonify({"success": False, "error": "stock_item is required"}), 400

    try:
        car_folder = _sanitize_upload_car_folder(request.form.get("car_folder", ""))
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    original_name = uploaded_file.filename
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in Config.ALLOWED_IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(Config.ALLOWED_IMAGE_EXTENSIONS))
        return jsonify({"success": False, "error": f"Unsupported file type '{extension or 'unknown'}'. Allowed types: {allowed}"}), 400

    uploaded_file.seek(0, os.SEEK_END)
    file_size = uploaded_file.tell()
    uploaded_file.seek(0)
    if file_size <= 0:
        return jsonify({"success": False, "error": "Uploaded file is empty"}), 400
    if file_size > Config.MAX_IMAGE_SIZE:
        max_mb = Config.MAX_IMAGE_SIZE / (1024 * 1024)
        return jsonify({"success": False, "error": f"File is too large. Maximum size is {max_mb:.0f} MB"}), 400

    target_dir = os.path.join(IMAGE_SCAN_ROOT, car_folder)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        return jsonify({"success": False, "error": f"Could not create folder: {exc}"}), 500

    safe_base_name = _sanitize_upload_filename(original_name)
    final_filename = _unique_filename_in_dir(target_dir, safe_base_name)
    destination_path = os.path.join(target_dir, final_filename)

    try:
        uploaded_file.save(destination_path)
    except OSError as exc:
        return jsonify({"success": False, "error": f"Failed to save file: {exc}"}), 500

    relative_path = os.path.relpath(destination_path, IMAGE_SCAN_ROOT).replace("\\", "/")

    try:
        image_id = db.add_image(car_folder, final_filename, relative_path)
    except Exception as exc:
        try:
            os.remove(destination_path)
        except OSError:
            pass
        logger.exception("Failed to add uploaded image to database")
        return jsonify({"success": False, "error": f"Failed to save image record: {exc}"}), 500

    try:
        confirm_result = _confirm_mapping_core(image_id, stock_item_name, car_model=car_folder, confidence=1.0, confirmed_by="human")
    except ValueError as exc:
        logger.exception("Failed to confirm mapping for uploaded image")
        return jsonify({"success": False, "error": f"Image saved but failed to confirm match: {exc}"}), 500

    return jsonify({
        "success": True,
        "image_id": image_id,
        "image_url": f"/get_image/{image_id}",
        "car_folder": car_folder,
        "stock_item": stock_item_name,
        "mapped": True,
        "stats": confirm_result.get("stats"),
    }), 200


def _resolve_stored_image_path(stored_path):
    if not stored_path:
        return None

    value = str(stored_path).strip().replace("\\", "/")
    if not value:
        return None

    # Preferred portable root: always resolve from app root.
    portable_root = os.path.join(app.root_path, "data", "S.S IMAGE")
    fallback_root = IMAGE_SCAN_ROOT

    def _safe_join(root_dir, relative_path):
        candidate = os.path.normpath(os.path.join(root_dir, relative_path))
        root_norm = os.path.normcase(os.path.normpath(root_dir)) if os.name == "nt" else os.path.normpath(root_dir)
        candidate_norm = os.path.normcase(candidate) if os.name == "nt" else candidate
        if candidate_norm == root_norm or candidate_norm.startswith(root_norm + os.sep):
            return candidate
        return None

    # Handle legacy absolute rows from older scans.
    if os.path.isabs(value):
        if os.path.exists(value):
            return value
        # Salvage by using just the relative suffix from ".../S.S IMAGE/" onward.
        marker = "s.s image/"
        lowered = value.lower().replace("\\", "/")
        marker_index = lowered.find(marker)
        if marker_index != -1:
            value = value[marker_index + len(marker):].replace("\\", "/")
        else:
            value = os.path.basename(value)

    # Strip optional root prefix if present in stored relative value.
    normalized = value.replace("\\", "/").lstrip("/")
    if normalized.lower().startswith("s.s image/"):
        normalized = normalized.split("/", 1)[1] if "/" in normalized else ""
    if not normalized:
        return None

    for root_dir in (portable_root, fallback_root):
        candidate = _safe_join(root_dir, normalized)
        if candidate and os.path.exists(candidate):
            return candidate
    return None


@app.route("/get_image/<int:image_id>")
def get_image(image_id):
    image_record = db.get_image_by_id(image_id)
    if not image_record:
        return _placeholder_response()

    file_path = _resolve_stored_image_path(image_record.get("filepath"))
    if file_path:
        return send_file(file_path, conditional=True)

    return _placeholder_response()


@app.route("/get_stock_image")
def get_stock_image():
    stock_item_name = request.args.get("stock_item", "")
    if not stock_item_name:
        return _placeholder_response()

    mapping = db.get_mapping_for_stock_item(stock_item_name)
    if mapping:
        file_path = _resolve_stored_image_path(mapping.get("filepath"))
        if file_path:
            return send_file(file_path, conditional=True)

    return _placeholder_response()


@app.route("/get_current_mapping_image")
def get_current_mapping_image():
    stock_item_name = request.args.get("stock_item", "")
    if not stock_item_name:
        return jsonify({"has_mapping": False, "image_id": None, "image_url": None, "confidence": None})

    mapping = db.get_mapping_for_stock_item(stock_item_name)
    if not mapping:
        return jsonify({"has_mapping": False, "image_id": None, "image_url": None, "confidence": None})

    image_id = mapping.get("image_id")
    return jsonify({
        "has_mapping": True,
        "image_id": image_id,
        "image_url": f"/get_image/{image_id}",
        "confidence": mapping.get("confidence"),
    })


@app.route("/suggest_match/<int:image_id>")
def suggest_match_route(image_id):
    return jsonify({"error": "AI suggestion is disabled for now"}), 410


@app.route("/scan_images", methods=["POST"])
@admin_required
def scan_images():
    result = image_scanner.scan_ss_image_folder(IMAGE_SCAN_ROOT)
    return jsonify({"status": "scanned", **result, "stats": db.get_mapping_stats()})


@app.route("/mapping_stats")
def mapping_stats():
    return jsonify(db.get_mapping_stats())


def run_full_refresh_job():
    """Fetch car master, main hierarchy, and item stock from Tally, then reload.

    Shared by the manual /full_refresh route and the automatic startup refresh.
    Caller is responsible for holding FULL_REFRESH_LOCK for the duration of this call.
    """
    global full_refresh_status
    try:
        full_refresh_status = {
            "running": True,
            "stage": "car_master",
            "error": None,
            "timestamp": datetime.now().isoformat()
        }

        car_names = fetch_car_master_from_tally()
        save_car_master_to_file(car_names)
        full_refresh_status["stage"] = "main_hierarchy"

        flat_rows = fetch_main_hierarchy_from_tally()
        save_main_hierarchy_to_file(flat_rows)
        full_refresh_status["stage"] = "item_stock"

        fetch_item_stock_flat()
        full_refresh_status["stage"] = "reloading"

        load_data(refresh_first=False)
        full_refresh_status = {
            "running": False,
            "stage": "done",
            "error": None,
            "timestamp": datetime.now().isoformat()
        }
        logger.info("Full refresh completed successfully")

    except Exception as exc:
        error_code = _classify_tally_exception(exc)
        full_refresh_status = {
            "running": False,
            "stage": "error",
            "error": str(exc),
            "error_code": error_code,
            "timestamp": datetime.now().isoformat()
        }
        logger.exception("Full refresh failed: %s", exc)


@app.route("/full_refresh", methods=["POST"])
@admin_required
def full_refresh():
    if not FULL_REFRESH_LOCK.acquire(blocking=False):
        return jsonify({
            "ok": False,
            "busy": True,
            "message": "Full refresh already in progress"
        }), 409

    def _run_full_refresh():
        try:
            run_full_refresh_job()
        finally:
            FULL_REFRESH_LOCK.release()

    thread = threading.Thread(target=_run_full_refresh, daemon=True)
    thread.start()

    return jsonify({
        "ok": True,
        "busy": False,
        "message": "Full refresh started"
    }), 202


@app.route("/full_refresh_status")
def full_refresh_status_route():
    return jsonify(full_refresh_status)


@app.route("/reload", methods=["POST"])
@admin_required
def reload_data():
    """Reload cached data (without reaching out to Tally)."""
    try:
        load_data(refresh_first=False)
    except Exception as exc:
        load_error = str(exc)
        return jsonify({"status": "error", "error": load_error}), 500
    return jsonify({"status": "reloaded"})


@app.route("/update_stock", methods=["POST"])
@admin_required
def update_stock():
    return refresh_stock()

@app.route("/refresh_item_stock", methods=["POST"])
@admin_required
def refresh_item_stock():
    result = _refresh_stock_data()
    status_code = 200
    if not result.get("tally_online"):
        result["status"] = "using_last_saved_upload"
    return jsonify(result), status_code

@app.route("/last_update")
def last_update():
    """Return the last modification time of the item stock export file."""
    try:
        update_file = get_latest_stock_file_path() or ITEM_STOCK_FILE_AUTO
        if os.path.exists(update_file):
            mtime = os.path.getmtime(update_file)
            dt = datetime.fromtimestamp(mtime)
            iso_ts = dt.isoformat()
            return jsonify({
                "last_update": dt.strftime("%d/%m/%Y %H:%M:%S"),
                "timestamp": iso_ts,
                "formatted": dt.strftime("%d/%m/%Y %H:%M:%S"),
                "file": update_file,
            })
        else:
            now_iso = datetime.now().isoformat()
            return jsonify({
                "last_update": "File not found",
                "timestamp": now_iso,
                "formatted": datetime.fromisoformat(now_iso).strftime("%d/%m/%Y %H:%M:%S"),
            })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/refresh_status")
def refresh_status():
    """Return the status of the last automatic or manual refresh."""
    return jsonify(last_refresh_status)


@app.route("/refresh_stock", methods=["POST"])
@admin_required
def refresh_stock():
    """Legacy alias for manual item stock refresh."""
    result = _refresh_stock_data()
    return jsonify(result), 200


set_search_dependencies(
    get_car_models=lambda: list(CAR_GROUPS),
    get_car_folders=db.get_image_folders,
    get_customer_users=db.get_customer_users,
    get_stock_items_for_car_from_hierarchy=get_stock_items_for_training_from_hierarchy,
    get_stock_items_for_car=get_stock_items_for_training,
)


# ----------------------------
# Startup
# ----------------------------
if __name__ == "__main__":
    # Dual-file mode:
    # - main.xlsx/main.xls is manual and holds hierarchy
    # - item stock list.xls is auto-exported and holds quantities
    
    print("=" * 60)
    print("Tally Stock Viewer - Hierarchical Parser")
    print("=" * 60)
    
    main_file = get_main_file_path()
    if not os.path.exists(main_file):
        print(f"\nWARNING: Main file not found. Tried: {', '.join(MAIN_FILE_CANDIDATES)}")
        print("\nmain.xlsx/main.xls is required for parent-child hierarchy.")
        print("Auto-export updates only item stock list.xlsx (quantities).")
        print("Please manually export hierarchy once and save as data/main.xlsx")
    else:
        print(f"Using main hierarchy file: {main_file}")
        print("Starting background data load and image scan...")

    start_background_startup_tasks()
    
    print("\nStarting Flask server on http://localhost:5000")
    print("Press Ctrl+C to stop\n")
    app.run(debug=app.config["DEBUG"])
