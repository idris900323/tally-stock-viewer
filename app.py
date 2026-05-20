from flask import Flask, jsonify, request, render_template, send_file, Response, session, redirect, url_for
import json
import pandas as pd
import os
import requests
import xml.etree.ElementTree as ET
import re
import threading
import time
import glob
import logging
from io import BytesIO
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
from utils.normalize import extract_car_base_name, normalize_text

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = app.config["SECRET_KEY"]
app.register_blueprint(search_bp)


def _configure_logging():
    os.makedirs(app.config["LOG_DIR"], exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    root = logging.getLogger()
    root.setLevel(getattr(logging, app.config["LOG_LEVEL"], logging.INFO))
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        file_handler = RotatingFileHandler(app.config["LOG_FILE"], maxBytes=2 * 1024 * 1024, backupCount=5)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


_configure_logging()
logger = logging.getLogger(__name__)

TALLY_HTTP_SESSION = requests.Session()
EXPORT_LOCK = threading.Lock()
LAST_TALLY_ETAG = {"hash": None, "result": None}
LOGIN_ATTEMPTS = {}

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
IMAGE_SCAN_ROOT = os.path.abspath(
    os.environ.get("IMAGE_SCAN_ROOT", os.path.join(PROJECT_ROOT, "data", "S.S IMAGE"))
)
PLACEHOLDER_SVG = b"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 320 320' role='img' aria-label='No image available'>
<rect width='320' height='320' rx='24' fill='#eef2f7'/>
<rect x='54' y='54' width='212' height='212' rx='18' fill='#d9e2ec'/>
<path d='M84 210l46-46 36 36 26-26 44 44H84z' fill='#8da2b8'/>
<circle cx='121' cy='122' r='20' fill='#8da2b8'/>
<text x='160' y='286' text-anchor='middle' font-family='Arial, sans-serif' font-size='20' fill='#52616b'>No image mapped</text>
</svg>"""

# file locations and polling configuration

CAR_FILE = "data/car master list.xls"  # dropdown list
MAIN_FILE_CANDIDATES = [
    "data/main.xlsx",
    "data/main.xls",
    "main.xlsx",
    "main.xls",
]  # parent-child hierarchy (never overwritten)
ITEM_STOCK_FILE = "data/item stock list.xls"  # flat stock item export (quantities only)
ITEM_STOCK_FILE_XLSX = "data/item stock list.xlsx"  # preferred auto-export target
ITEM_STOCK_FILE_AUTO = "data/item stock list.auto.xlsx"  # runtime auto-export target
ITEM_STOCK_CACHE_JSON = "data/item stock list.auto.json"
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
PUBLIC_ENDPOINTS = {"login", "logout", "static"}

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
    if raw_next and isinstance(raw_next, str) and raw_next.startswith("/") and not raw_next.startswith("//"):
        return raw_next
    return url_for("home")


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
    return normalize_text(text)


def _normalize_lookup_key(text):
    return _normalize_text(text).lower()


def _file_fingerprint(file_path):
    if not file_path or not os.path.exists(file_path):
        return None
    stats = os.stat(file_path)
    return (os.path.abspath(file_path), stats.st_mtime_ns, stats.st_size)


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


def _resolve_prices_for_stock_items(stock_item_names):
    lookup = {}
    default_price = "Contact Us"

    current_user_id = _current_user_id()
    current_role = _current_role()
    if current_user_id is None or current_role is None:
        return lookup, default_price

    user_record = db.get_user_by_id(current_user_id)
    if user_record and bool(user_record.get("force_contact_us")):
        for stock_item_name in stock_item_names:
            lookup[_normalize_lookup_key(stock_item_name)] = default_price
        return lookup, default_price

    custom_lookup = db.get_customer_prices_for_stock_items(current_user_id, stock_item_names)
    base_lookup = db.get_base_prices_for_stock_items(stock_item_names)

    for stock_item_name in stock_item_names:
        key = _normalize_lookup_key(stock_item_name)
        custom_price = custom_lookup.get(key)
        if custom_price:
            lookup[key] = custom_price
            continue

        base_price = base_lookup.get(key)
        if base_price:
            lookup[key] = base_price
            continue

        lookup[key] = default_price

    return lookup, default_price


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
    price_lookup, default_price = _resolve_prices_for_stock_items(stock_item_names)
    payload = []
    for item in designs or []:
        stock_item_name = item.get("design") or item.get("raw") or "Unknown"
        price = price_lookup.get(_normalize_lookup_key(stock_item_name), default_price)
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
        enriched_item["price"] = price
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


def _stock_items_for_train_page():
    items = _all_stock_items()
    if items:
        return items
    return []


def get_stock_items_for_training(car_full_name):
    base_name = extract_car_base_name(car_full_name)
    base_norm = _normalize_text(base_name)
    full_norm = _normalize_text(car_full_name)
    exact_car_norm = _normalize_text(car_full_name)

    items = _all_stock_items()
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
    attempts = LOGIN_ATTEMPTS.get(key, [])
    attempts = [ts for ts in attempts if now - ts < window_seconds]
    if len(attempts) >= max_attempts:
        LOGIN_ATTEMPTS[key] = attempts
        return False
    attempts.append(now)
    LOGIN_ATTEMPTS[key] = attempts
    return True


def _post_tally_with_retry(xml_req):
    request_hash = hash(xml_req)
    if LAST_TALLY_ETAG["hash"] == request_hash and LAST_TALLY_ETAG["result"]:
        return LAST_TALLY_ETAG["result"]

    response = fetch_from_tally_with_retry(
        TALLY_HTTP_SESSION,
        TALLY_URL,
        xml_req,
        timeout=TALLY_TIMEOUT,
        max_retries=TALLY_RETRY_ATTEMPTS,
        logger=logger,
    )
    text = response.text
    LAST_TALLY_ETAG["hash"] = request_hash
    LAST_TALLY_ETAG["result"] = text
    return text


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
    candidates.extend(glob.glob("data/item stock list.auto.*.xlsx"))
    existing = [
        path for path in candidates
        if os.path.exists(path)
        and not str(path).lower().endswith(".tmp.xlsx")
    ]
    if not existing:
        return None
    return max(existing, key=lambda path: os.path.getmtime(path))


def _scan_images_if_database_empty():
    try:
        if not app.config["INITIAL_IMAGE_SCAN"]:
            logger.info("Initial image scan disabled by INITIAL_IMAGE_SCAN")
            return
        if not os.path.exists(IMAGE_SCAN_ROOT):
            return
        if db.get_image_count() > 0:
            logger.info("Skipping startup image scan because database already has indexed images")
            return
        result = image_scanner.scan_ss_image_folder(IMAGE_SCAN_ROOT)
        print(f"scanned image folder: {result['total_images']} images from {result['total_folders']} folders")
    except Exception as exc:
        print("warning: initial image scan skipped:", exc)


def _refresh_stock_data():
    global last_refresh_status
    try:
        export_result = fetch_item_stock_flat()
        load_data()
        msg = "Stock updated successfully"
        if export_result and export_result.get("warning"):
            msg = f"{msg} ({export_result.get('warning')})"
        last_refresh_status = {
            "success": True,
            "message": msg,
            "timestamp": datetime.now().isoformat(),
        }
        timestamp_iso = datetime.now().isoformat()
        return {
            "ok": True,
            "tally_online": True,
            "status": "stock updated",
            "message": msg,
            "file": export_result.get("file") if export_result else None,
            "warning": export_result.get("warning") if export_result else None,
            "timestamp": timestamp_iso,
            "formatted": datetime.fromisoformat(timestamp_iso).strftime("%d/%m/%Y %H:%M:%S"),
        }
    except Exception as exc:
        fallback_message = f"Tally is down; using the last saved upload. ({exc})"
        last_refresh_status = {
            "success": False,
            "message": fallback_message,
            "timestamp": datetime.now().isoformat(),
        }
        timestamp_iso = datetime.now().isoformat()
        return {
            "ok": True,
            "tally_online": False,
            "status": "using_last_saved_upload",
            "message": fallback_message,
            "file": get_latest_stock_file_path(),
            "warning": fallback_message,
            "timestamp": timestamp_iso,
            "formatted": datetime.fromisoformat(timestamp_iso).strftime("%d/%m/%Y %H:%M:%S"),
        }

# ---------- Tally export logic ----------

def fetch_item_stock_from_tally():
    """Backward-compatible wrapper: export flat item stock only.

    Intentionally does NOT touch the main hierarchy file or `car master list.xls`.
    """
    return fetch_item_stock_flat()


def fetch_item_stock_flat():
    """Export item stock from Tally Stock Summary and save as clean Excel.

    This avoids writing Tally XML error text into the spreadsheet file.
    """
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
            df = pd.read_excel(main_file)
            if df.empty:
                return set()
            names = set()
            for raw in df.iloc[:, 0].dropna().astype(str).tolist():
                normalized = _norm(raw)
                if normalized and normalized not in {"PARTICULARS", "STOCK SUMMARY", "CLOSING BALANCE", "QUANTITY"}:
                    names.add(normalized)
            return names
        except Exception:
            return set()

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

        stock_df = pd.DataFrame(rows)
        stock_df = stock_df.drop_duplicates(subset=["item_name"], keep="last")

        temp_file = ITEM_STOCK_FILE_AUTO + ".tmp.xlsx"
        out_file = ITEM_STOCK_FILE_AUTO
        warning = None

        stock_df.to_excel(temp_file, index=False)
        try:
            with open(ITEM_STOCK_CACHE_JSON, "w", encoding="utf-8") as handle:
                json.dump(stock_df.to_dict(orient="records"), handle, ensure_ascii=False)
        except Exception:
            pass

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
                stock_df.to_excel(ITEM_STOCK_FILE_XLSX, index=False)
                out_file = ITEM_STOCK_FILE_XLSX
            except Exception:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_file = f"data/item stock list.auto.{timestamp}.xlsx"
                stock_df.to_excel(out_file, index=False)

        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass

        print(f"exported item stock list to {out_file} ({len(stock_df)} rows)")
        if warning:
            print(warning)
        return {"rows": len(stock_df), "file": out_file, "warning": warning}
    except Exception as exc:
        print(f"item stock export failed: {str(exc)}")
        raise


def schedule_item_export():
    global item_export_timer, last_refresh_status
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
    df = pd.read_excel(file_path)
    
    designs = []
    ignored_labels = {"PARTICULARS", "STOCK SUMMARY", "CLOSING BALANCE", "QUANTITY", ""}
    
    print(f"Reading {len(df)} rows from {file_path}")
    
    for row in df.itertuples(index=False):
        col0 = str(row[0]).strip() if len(row) > 0 and pd.notna(row[0]) else ""
        col1_val = row[1] if len(row) > 1 else None
        
        # Skip headers and empty rows
        if not col0 or _normalize_text(col0) in ignored_labels:
            continue
        
        # Parse quantity
        qty = None
        if pd.notna(col1_val):
            try:
                qty = int(float(col1_val))
            except (ValueError, TypeError):
                pass
        
        # Only include rows with valid quantities
        if qty is not None and qty > 0:
            designs.append({
                "raw": col0,
                "design": col0,  # Clean version, may get simplified later
                "qty": qty
            })
    
    print(f"Loaded {len(designs)} valid designs\n")
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
            fetch_item_stock_from_tally()
        except Exception as exc:
            print("warning: could not refresh stock from Tally:", exc)

    # ---- Load car groups from car master list ----
    try:
        car_df = pd.read_excel(CAR_FILE)
        car_groups = (
            car_df.iloc[:, 0]
            .dropna()
            .astype(str)
            .str.strip()
            .tolist()
        )
        
        # Filter to relevant range
        start_marker = "ACCESSORIES (NECK REST)"
        end_marker = "ZS - EV FOOT MAT"
        if start_marker in car_groups and end_marker in car_groups:
            start_idx = car_groups.index(start_marker)
            end_idx = car_groups.index(end_marker)
            if start_idx <= end_idx:
                car_groups = car_groups[start_idx : end_idx + 1]
            else:
                car_groups = car_groups[end_idx : start_idx + 1]
        
        CAR_GROUPS = car_groups
        PARENT_NAME_SET = {_normalize_text(name) for name in CAR_GROUPS if str(name).strip()}
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
        # build mapping for quicker lookup later (silent)
        # primary matching logic is a plain substring search on the normalized base car name
        for car in CAR_GROUPS:
            matching_designs = []
            base_car = extract_car_base_name(car) or car
            base_car_upper = _normalize_text(base_car)
            for design_item in all_designs:
                design_upper = _normalize_text(design_item["raw"])
                if base_car_upper and (base_car_upper in design_upper or design_upper in base_car_upper):
                    matching_designs.append(design_item)
            if matching_designs:
                CAR_DESIGN_MAP[car] = matching_designs
        # summary log
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

    def _to_int(value):
        if pd.isna(value):
            return 0
        match = re.search(r"-?\d+", str(value))
        return int(match.group()) if match else 0

    def _load_main_rows_cached():
        main_file = get_main_file_path()
        fingerprint = _file_fingerprint(main_file)
        if fingerprint is None:
            return [], {}

        with DATA_CACHE_LOCK:
            if MAIN_ROWS_CACHE["fingerprint"] == fingerprint:
                return MAIN_ROWS_CACHE["rows"], MAIN_ROWS_CACHE["exact_index"]

        try:
            df = pd.read_excel(main_file)
        except Exception:
            return [], {}

        rows = []
        exact_index = {}
        ignore = {"PARTICULARS", "STOCK SUMMARY", "CLOSING BALANCE", "QUANTITY", ""}
        for idx, row in enumerate(df.itertuples(index=False)):
            name = str(row[0]).strip() if len(row) > 0 and pd.notna(row[0]) else ""
            if not name:
                continue
            name_upper = _norm(name)
            if name_upper in ignore:
                continue
            qty = _to_int(row[1] if len(row) > 1 else None)
            rows.append((name, name_upper, qty))
            exact_index.setdefault(name_upper, len(rows) - 1)

        with DATA_CACHE_LOCK:
            MAIN_ROWS_CACHE["fingerprint"] = fingerprint
            MAIN_ROWS_CACHE["rows"] = rows
            MAIN_ROWS_CACHE["exact_index"] = exact_index
        return rows, exact_index

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
            stock_df = pd.read_excel(stock_file)
            for row in stock_df.itertuples(index=False):
                item_name = str(row[0]).strip() if len(row) > 0 and pd.notna(row[0]) else ""
                qty_val = row[1] if len(row) > 1 else None
                if not item_name or pd.isna(qty_val):
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

    rows, exact_index = _load_main_rows_cached()
    if not rows:
        return []

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
        return []

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
        if upper_name in PARENT_NAME_SET or upper_name == matched_parent_upper:
            continue

        # Lookup quantity from item stock list
        qty = stock_qty_map.get(upper_name, 0)
        if qty > 0:
            children.append({"raw": name, "design": name, "qty": qty})
            running_qty += qty

            if parent_qty_total > 0 and running_qty >= parent_qty_total:
                break
    return children


def _coerce_limit(limit_value, default_value=None):
    if limit_value is None:
        limit_value = default_value
    if limit_value is None:
        return None
    try:
        limit = int(limit_value)
    except (TypeError, ValueError):
        limit = default_value if default_value is not None else MAX_IMAGE_RESPONSE_LIMIT
    if limit is None:
        return None
    return max(1, min(limit, MAX_IMAGE_RESPONSE_LIMIT))


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
    children = _find_children_by_qty(car)
    if children:
        return jsonify(_build_design_payload(children))

    # if that failed, keep the old flat lookup as a backup
    if car in CAR_DESIGN_MAP and CAR_DESIGN_MAP[car]:
        return jsonify(_build_design_payload(CAR_DESIGN_MAP[car]))

    return jsonify([])


@app.route("/admin/pricing")
@admin_required
def admin_pricing():
    customers = db.get_customer_users()
    raw_customer = (request.args.get("customer_id") or "").strip()
    selected_mode = "global"
    selected_customer_id = None
    selected_customer = None

    if raw_customer and raw_customer.lower() not in {"global", "0"}:
        try:
            candidate_id = int(raw_customer)
        except (TypeError, ValueError):
            candidate_id = None
        if candidate_id is not None:
            for customer in customers:
                if customer["id"] == candidate_id:
                    selected_mode = "customer"
                    selected_customer_id = candidate_id
                    selected_customer = customer
                    break

    status_message = (request.args.get("status") or "").strip()
    error_message = (request.args.get("error") or "").strip()
    return render_template(
        "pricing.html",
        customers=customers,
        selected_customer=selected_customer,
        selected_customer_id=selected_customer_id,
        selected_mode=selected_mode,
        status_message=status_message,
        error_message=error_message,
    )


@app.route("/admin/pricing_data")
@admin_required
def admin_pricing_data():
    load_error = ensure_data_loaded()
    if load_error:
        return jsonify({"error": load_error}), 500

    customers = db.get_customer_users()
    raw_customer = (request.args.get("customer_id") or "").strip()
    selected_mode = "global"
    selected_customer_id = None
    selected_customer = None

    if raw_customer and raw_customer.lower() not in {"global", "0"}:
        try:
            candidate_id = int(raw_customer)
        except (TypeError, ValueError):
            candidate_id = None
        if candidate_id is not None:
            for customer in customers:
                if customer["id"] == candidate_id:
                    selected_mode = "customer"
                    selected_customer_id = candidate_id
                    selected_customer = customer
                    break

    stock_items = _stock_items_for_train_page()
    stock_item_names = [item.get("stock_item_name", "") for item in stock_items]
    base_prices = db.get_base_prices_for_stock_items(stock_item_names)
    customer_prices = (
        db.get_customer_prices_for_stock_items(selected_customer_id, stock_item_names)
        if selected_mode == "customer" and selected_customer_id is not None
        else {}
    )

    rows = []
    for item in stock_items:
        stock_item_name = item.get("stock_item_name", "")
        key = _normalize_lookup_key(stock_item_name)
        base_price = str(base_prices.get(key, "") or "").strip()
        custom_price = str(customer_prices.get(key, "") or "").strip()
        rows.append({
            "car_model": item.get("car_model", ""),
            "stock_item_name": stock_item_name,
            "base_price": base_price,
            "custom_price": custom_price,
            "default_display": base_price or "Contact Us",
        })

    return jsonify({
        "mode": selected_mode,
        "selected_customer_id": selected_customer_id,
        "selected_customer": selected_customer,
        "rows": rows,
    })


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
            "force_contact_us": bool(created_user.get("force_contact_us")),
            "status": "paused" if bool(created_user.get("force_contact_us")) else "active",
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
            "access_code": customer.get("access_code", ""),
            "force_contact_us": bool(customer.get("force_contact_us")),
            "status": "paused" if bool(customer.get("force_contact_us")) else "active",
            "created_at": customer.get("created_at"),
        }
        for customer in customers
    ])


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


@app.route("/admin/toggle_user_status/<int:user_id>", methods=["POST"])
@admin_required
def admin_toggle_user_status(user_id):
    payload = request.get_json(silent=True) or request.form or {}
    pause = _parse_bool(payload.get("pause"))

    try:
        updated_user = db.set_customer_access_paused(user_id, pause)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message.lower() else 400
        return jsonify({"success": False, "error": message}), status

    db.log_account_action(user_id, "paused" if pause else "resumed", _current_user_id())
    return jsonify({
        "success": True,
        "message": f"Account '{updated_user['username']}' {'paused' if pause else 'resumed'}",
        "status": "paused" if bool(updated_user.get("force_contact_us")) else "active",
        "force_contact_us": bool(updated_user.get("force_contact_us")),
        "user_id": user_id,
    })


@app.route("/admin/set_all_customer_status", methods=["POST"])
@admin_required
def admin_set_all_customer_status():
    payload = request.get_json(silent=True) or request.form or {}
    pause = _parse_bool(payload.get("pause"))
    changed = db.set_all_customer_access_paused(pause)

    customers = db.get_all_customers_with_details()
    action_name = "paused" if pause else "resumed"
    admin_id = _current_user_id()
    for customer in customers:
        db.log_account_action(customer["id"], action_name, admin_id)

    return jsonify({
        "success": True,
        "message": f"All customers {action_name}",
        "updated_count": changed,
        "status": "paused" if pause else "active",
    })


@app.route("/admin/save_price", methods=["POST"])
@admin_required
def admin_save_price():
    payload = request.get_json(silent=True) or request.form or {}

    stock_item_name = str(payload.get("stock_item_name") or "").strip()
    if not stock_item_name:
        return jsonify({"error": "stock_item_name is required"}), 400

    mode = str(payload.get("mode") or "customer").strip().lower()
    base_price = str(payload.get("base_price") or "").strip()
    if mode == "global":
        db.upsert_base_price(stock_item_name, base_price)

    customer_id = None
    custom_price = ""
    if mode != "global":
        customer_id_raw = payload.get("customer_id")
        try:
            customer_id = int(customer_id_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "customer_id is required"}), 400

        customer = db.get_user_by_id(customer_id)
        if not customer or customer.get("role") != "customer":
            return jsonify({"error": "customer not found"}), 404

        custom_price = str(payload.get("custom_price") or "").strip()
        db.upsert_customer_price(customer_id, stock_item_name, custom_price)

    return jsonify({
        "status": "saved",
        "mode": mode,
        "customer_id": customer_id,
        "stock_item_name": stock_item_name,
        "base_price": base_price,
        "custom_price": custom_price,
    })


@app.route("/admin/toggle_contact_us", methods=["POST"])
@admin_required
def admin_toggle_contact_us():
    payload = request.get_json(silent=True) or request.form or {}
    customer_id = payload.get("customer_id")
    try:
        customer_id = int(customer_id)
    except (TypeError, ValueError):
        return jsonify({"error": "customer_id is required"}), 400

    customer = db.get_user_by_id(customer_id)
    if not customer or customer.get("role") != "customer":
        return jsonify({"error": "customer not found"}), 404

    if "force_contact_us" in payload:
        next_value = _parse_bool(payload.get("force_contact_us"))
    else:
        next_value = not bool(customer.get("force_contact_us"))

    db.set_force_contact_us(customer_id, next_value)

    return jsonify({
        "status": "updated",
        "customer_id": customer_id,
        "force_contact_us": bool(next_value),
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

            file_path = image_record.get("filepath")
            if not file_path or not os.path.exists(file_path):
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

    image_record = db.get_image_by_id(image_id)
    if not image_record:
        return jsonify({"error": "image not found"}), 404

    resolved_car_model = car_model or _resolve_car_model_hint(image_record) or image_record.get("car_folder")
    db.add_mapping(image_id, stock_item_name, resolved_car_model, confidence, confirmed_by=confirmed_by)
    if resolved_car_model and confidence >= 1.0 and stock_item_name not in ("", "__UNMATCHABLE__"):
        db.add_folder_mapping(image_record.get("car_folder", ""), resolved_car_model)

    next_image = db.get_next_unmapped_image(after_image_id=image_id)
    return jsonify({
        "status": "saved",
        "image_id": image_id,
        "next_image": next_image,
        "stats": db.get_mapping_stats(),
    })


@app.route("/get_image/<int:image_id>")
def get_image(image_id):
    image_record = db.get_image_by_id(image_id)
    if not image_record:
        return _placeholder_response()

    file_path = image_record.get("filepath")
    if file_path and os.path.exists(file_path):
        return send_file(file_path, conditional=True)

    return _placeholder_response()


@app.route("/get_stock_image")
def get_stock_image():
    stock_item_name = request.args.get("stock_item", "")
    if not stock_item_name:
        return _placeholder_response()

    mapping = db.get_mapping_for_stock_item(stock_item_name)
    if mapping:
        file_path = mapping.get("filepath")
        if file_path and os.path.exists(file_path):
            return send_file(file_path, conditional=True)

    return _placeholder_response()


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
    return jsonify(_refresh_stock_data()), 200

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
        # Try to load data without refresh (use existing file)
        load_data(refresh_first=False)

    _scan_images_if_database_empty()
    
    # auto-export for item stock list only
    if item_export_enabled:
        print("AUTO_EXPORT_ITEM is enabled; scheduling periodic item stock exports")
        print("Only data/item stock list.xlsx will be overwritten")
        try:
            schedule_item_export()
        except Exception as exc:
            print("initial item stock export failed:", exc)
    else:
        print("AUTO_EXPORT_ITEM disabled; use /refresh_item_stock to trigger manually")

    print("\nStarting Flask server on http://localhost:5000")
    print("Press Ctrl+C to stop\n")
    app.run(debug=app.config["DEBUG"])
