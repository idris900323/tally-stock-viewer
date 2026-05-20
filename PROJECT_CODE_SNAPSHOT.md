# Project Code Snapshot
Generated: 2026-05-19 14:21:52 +05:30

---
## File: app.py
```text
from flask import Flask, jsonify, request, render_template, send_file, Response, session, redirect, url_for
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
from config import Config

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = app.config["SECRET_KEY"]


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
ITEM_EXPORT_INTERVAL = app.config["TALLY_EXPORT_INTERVAL"]
item_export_timer = None
item_export_enabled = os.environ.get("AUTO_EXPORT_ITEM", "1").strip().lower() not in ("0", "false")
MAX_IMAGE_RESPONSE_LIMIT = max(1, int(app.config["MAX_IMAGE_RESPONSE_LIMIT"]))

# Configuration for automatic polling of Tally
TALLY_URL = app.config["TALLY_URL"]
TALLY_TIMEOUT = app.config["TALLY_TIMEOUT"]
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
    return re.sub(r"\s+", " ", str(text or "")).strip().upper()


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

    last_error = None
    for attempt in range(TALLY_RETRY_ATTEMPTS):
        try:
            resp = TALLY_HTTP_SESSION.post(TALLY_URL, data=xml_req, timeout=TALLY_TIMEOUT)
            resp.raise_for_status()
            text = resp.text
            LAST_TALLY_ETAG["hash"] = request_hash
            LAST_TALLY_ETAG["result"] = text
            return text
        except Exception as exc:
            last_error = exc
            backoff = 0.2 * (2 ** attempt)
            time.sleep(backoff)
    raise last_error if last_error is not None else Exception("Tally request failed")


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
        msg = "Item stock refreshed successfully"
        if export_result and export_result.get("warning"):
            msg = f"{msg} ({export_result.get('warning')})"
        last_refresh_status = {
            "success": True,
            "message": msg,
            "timestamp": datetime.now().isoformat(),
        }
        return {
            "ok": True,
            "tally_online": True,
            "status": "item stock refreshed",
            "message": msg,
            "file": export_result.get("file") if export_result else None,
            "warning": export_result.get("warning") if export_result else None,
        }
    except Exception as exc:
        fallback_message = f"Tally is down; using the last saved upload. ({exc})"
        last_refresh_status = {
            "success": False,
            "message": fallback_message,
            "timestamp": datetime.now().isoformat(),
        }
        return {
            "ok": True,
            "tally_online": False,
            "status": "using_last_saved_upload",
            "message": fallback_message,
            "file": get_latest_stock_file_path(),
            "warning": fallback_message,
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
        msg = "Item stock refreshed successfully"
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
        # primary matching logic is a plain substring search on the base car name
        for car in CAR_GROUPS:
            matching_designs = []
            base_car = re.sub(r'\*\*.*?\*\*', '', car).strip()
            base_car_upper = base_car.upper()
            for design_item in all_designs:
                if base_car_upper in design_item["raw"].upper():
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
        return redirect(url_for("admin_pricing", error="Username is required."))
    if not access_code:
        return redirect(url_for("admin_pricing", error="Access code is required."))

    try:
        created_user = db.create_customer_user(username, access_code)
    except ValueError as exc:
        return redirect(url_for("admin_pricing", error=str(exc)))

    return redirect(
        url_for(
            "admin_pricing",
            customer_id=created_user["id"],
            status=f"Created customer account: {created_user['username']}",
        )
    )


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
        car_models=CAR_GROUPS,
        ss_image_folders=db.get_image_folders(),
        stock_items=_stock_items_for_train_page(),
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
            return jsonify({
                "last_update": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp": mtime,
                "file": update_file,
            })
        else:
            return jsonify({"last_update": "File not found", "timestamp": None})
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
```
---
## File: database.py
```text
import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime

from config import Config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = Config.DB_PATH if os.path.isabs(Config.DB_PATH) else os.path.join(BASE_DIR, Config.DB_PATH)
logger = logging.getLogger(__name__)


def _canonicalize_filepath(filepath):
    value = str(filepath or "").strip()
    if not value:
        return value
    normalized = os.path.normpath(os.path.abspath(value))
    if os.name == "nt":
        normalized = os.path.normcase(normalized)
    return normalized


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


def _validate_filepath(filepath):
    value = _canonicalize_filepath(filepath)
    if not value:
        raise ValueError("filepath is required")
    if "\x00" in value:
        raise ValueError("filepath contains null byte")
    if not os.path.exists(value):
        raise ValueError(f"filepath does not exist: {value}")
    if not os.access(value, os.R_OK):
        raise ValueError(f"filepath is not readable: {value}")
    if os.path.getsize(value) > Config.MAX_IMAGE_SIZE:
        raise ValueError(f"filepath exceeds max image size: {value}")
    return value


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=Config.DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
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
    with _connect() as conn:
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
                image_id INTEGER NOT NULL UNIQUE,
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
                force_contact_us INTEGER NOT NULL DEFAULT 0
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_filepath ON images(filepath)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_folder_car_mapping ON folder_car_mapping(folder_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_prices_user_stock ON customer_prices(user_id, stock_item_name)")
        seed_default_data(conn)


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
            INSERT INTO users (username, access_code, role, force_contact_us)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("admin", "idris123", "admin", 0),
                ("star", "111", "customer", 0),
                ("jeewajee", "222", "customer", 0),
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
                ON CONFLICT(image_id) DO UPDATE SET
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
    cleaned = []
    seen = set()
    for stock_item_name in stock_item_names or []:
        try:
            value = _validate_stock_item_name(stock_item_name).strip()
        except ValueError:
            continue
        if not value:
            continue
        lookup_key = value.lower()
        if lookup_key in seen:
            continue
        seen.add(lookup_key)
        cleaned.append(lookup_key)

    if not cleaned:
        return {}

    placeholders = ", ".join(["?"] * len(cleaned))
    query = f"""
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
        WHERE LOWER(m.stock_item_name) IN ({placeholders})
          AND m.id = (
              SELECT m2.id
              FROM mappings m2
              WHERE LOWER(m2.stock_item_name) = LOWER(m.stock_item_name)
              ORDER BY m2.created_at DESC, m2.id DESC
              LIMIT 1
          )
    """
    with _connect() as conn:
        rows = conn.execute(query, cleaned).fetchall()

    result = {}
    for row in rows:
        record = _row_to_dict(row)
        key = str(record.get("stock_item_name") or "").strip().lower()
        if key:
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
        ORDER BY id ASC
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
        ORDER BY id ASC
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
        ORDER BY id ASC
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


def authenticate_user(username, access_code):
    username_value = str(username or "").strip()
    access_code_value = str(access_code or "").strip()
    if not username_value or not access_code_value:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, username, role, force_contact_us
            FROM users
            WHERE LOWER(username) = LOWER(?) AND access_code = ?
            LIMIT 1
            """,
            (username_value, access_code_value),
        ).fetchone()
    return _row_to_dict(row)


def get_user_by_id(user_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, username, role, force_contact_us FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
    return _row_to_dict(row)


def get_customer_users():
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, username, role, force_contact_us
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
            cursor = conn.execute(
                """
                INSERT INTO users (username, access_code, role, force_contact_us)
                VALUES (?, ?, 'customer', 0)
                """,
                (username_value, access_code_value),
            )
            user_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise ValueError("username already exists") from exc

    return get_user_by_id(user_id)


def set_force_contact_us(user_id, enabled):
    force_value = 1 if bool(enabled) else 0
    with _connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET force_contact_us = ?
            WHERE id = ? AND role = 'customer'
            """,
            (force_value, int(user_id)),
        )


def upsert_base_price(stock_item_name, price):
    item_name = _validate_stock_item_name(stock_item_name).strip()
    if not item_name:
        return
    price_value = str(price or "").strip()
    with _connect() as conn:
        if price_value:
            conn.execute(
                """
                INSERT INTO base_prices (stock_item_name, price)
                VALUES (?, ?)
                ON CONFLICT(stock_item_name) DO UPDATE SET
                    price = excluded.price
                """,
                (item_name, price_value),
            )
        else:
            conn.execute(
                "DELETE FROM base_prices WHERE LOWER(stock_item_name) = LOWER(?)",
                (item_name,),
            )


def upsert_customer_price(user_id, stock_item_name, price):
    item_name = _validate_stock_item_name(stock_item_name).strip()
    if not item_name:
        return
    price_value = str(price or "").strip()
    with _connect() as conn:
        if price_value:
            conn.execute(
                """
                INSERT INTO customer_prices (user_id, stock_item_name, price)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, stock_item_name) DO UPDATE SET
                    price = excluded.price
                """,
                (int(user_id), item_name, price_value),
            )
        else:
            conn.execute(
                """
                DELETE FROM customer_prices
                WHERE user_id = ? AND LOWER(stock_item_name) = LOWER(?)
                """,
                (int(user_id), item_name),
            )


def get_base_prices_for_stock_items(stock_item_names):
    cleaned = []
    seen = set()
    for stock_item_name in stock_item_names or []:
        item_name = str(stock_item_name or "").strip()
        if not item_name:
            continue
        key = item_name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(key)
    if not cleaned:
        return {}

    placeholders = ", ".join(["?"] * len(cleaned))
    query = f"""
        SELECT stock_item_name, price
        FROM base_prices
        WHERE LOWER(stock_item_name) IN ({placeholders})
    """
    with _connect() as conn:
        rows = conn.execute(query, cleaned).fetchall()
    return {str(row["stock_item_name"]).strip().lower(): str(row["price"] or "") for row in rows}


def get_customer_prices_for_stock_items(user_id, stock_item_names):
    cleaned = []
    seen = set()
    for stock_item_name in stock_item_names or []:
        item_name = str(stock_item_name or "").strip()
        if not item_name:
            continue
        key = item_name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(key)
    if not cleaned:
        return {}

    placeholders = ", ".join(["?"] * len(cleaned))
    params = [int(user_id)]
    params.extend(cleaned)
    query = f"""
        SELECT stock_item_name, price
        FROM customer_prices
        WHERE user_id = ? AND LOWER(stock_item_name) IN ({placeholders})
    """
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return {str(row["stock_item_name"]).strip().lower(): str(row["price"] or "") for row in rows}
```
---
## File: config.py
```text
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
```
---
## File: image_scanner.py
```text
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
```
---
## File: matcher.py
```text
import re
from difflib import SequenceMatcher

from database import get_confirmed_mappings, get_folder_car_model


CODE_PATTERN = re.compile(r"(?<!\d)(\d{3,5}-\d{3,5}(?:-\d{3,5})?)(?!\d)")


def normalize_text(text):
    return re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).strip()


def extract_codes(text):
    return CODE_PATTERN.findall(str(text or ""))


def _candidate_stock_items(available_stock_items):
    candidates = []
    for item in available_stock_items or []:
        if isinstance(item, dict):
            stock_item = item.get("design") or item.get("raw") or item.get("stock_item_name") or item.get("name")
        else:
            stock_item = item
        if stock_item:
            candidates.append(str(stock_item))
    return candidates


def _similarity(left, right):
    return SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()


def _image_text(image_record):
    return " ".join(
        str(part)
        for part in [
            image_record.get("car_folder", ""),
            image_record.get("filename", ""),
            image_record.get("filepath", ""),
        ]
        if part
    )


def _match_by_code(image_record, stock_items):
    image_codes = set(extract_codes(_image_text(image_record)))
    if not image_codes:
        return None, 0.0

    best_item = None
    best_score = 0.0
    for stock_item in stock_items:
        stock_codes = set(extract_codes(stock_item))
        if not stock_codes:
            continue
        if image_codes & stock_codes:
            return stock_item, 0.95

        image_code_text = next(iter(image_codes))
        stock_text = normalize_text(stock_item)
        if normalize_text(image_code_text) in stock_text and 0.85 > best_score:
            best_item = stock_item
            best_score = 0.85
    return best_item, best_score


def _match_by_car_folder(image_record, stock_items):
    folder_name = normalize_text(image_record.get("car_folder", ""))
    if not folder_name:
        return None, 0.0

    folder_mapping = get_folder_car_model(image_record.get("car_folder", ""))
    folder_hint = normalize_text(folder_mapping["car_model_name"]) if folder_mapping else folder_name

    best_item = None
    best_score = 0.0
    for stock_item in stock_items:
        stock_text = normalize_text(stock_item)
        if not stock_text:
            continue

        if folder_hint and (folder_hint in stock_text or stock_text in folder_hint):
            return stock_item, 0.7

        common_tokens = set(folder_name.split()) & set(stock_text.split())
        if common_tokens:
            similarity = _similarity(folder_name, stock_text)
            if similarity > best_score:
                best_item = stock_item
                best_score = max(0.6, similarity)

    return best_item, best_score


def _match_by_learned_patterns(image_record, stock_items):
    confirmed_mappings = get_confirmed_mappings()
    if not confirmed_mappings:
        return None, 0.0

    current_filename = normalize_text(image_record.get("filename", ""))
    if not current_filename:
        current_filename = normalize_text(_image_text(image_record))

    best_item = None
    best_score = 0.0

    for mapping in confirmed_mappings:
        learned_filename = normalize_text(mapping.get("filename", ""))
        learned_stock_item = mapping.get("stock_item_name")
        if not learned_stock_item:
            continue

        score = _similarity(current_filename, learned_filename)
        if score < 0.55:
            continue

        for stock_item in stock_items:
            if normalize_text(stock_item) != normalize_text(learned_stock_item):
                continue
            if score > best_score:
                best_item = stock_item
                best_score = min(0.75, round(score, 2))

    return best_item, best_score


def suggest_match(image_record, available_stock_items):
    stock_items = _candidate_stock_items(available_stock_items)
    if not stock_items:
        return None, 0.0

    exact_code_item, exact_code_score = _match_by_code(image_record, stock_items)
    if exact_code_item:
        return exact_code_item, exact_code_score

    folder_item, folder_score = _match_by_car_folder(image_record, stock_items)
    learned_item, learned_score = _match_by_learned_patterns(image_record, stock_items)

    ranked_candidates = [
        (folder_item, folder_score),
        (learned_item, learned_score),
    ]
    ranked_candidates = [candidate for candidate in ranked_candidates if candidate[0]]
    if ranked_candidates:
        ranked_candidates.sort(key=lambda candidate: candidate[1], reverse=True)
        return ranked_candidates[0]

    return None, 0.0
```
---
## File: test_tally.py
```text
import requests
import xml.etree.ElementTree as ET
import re

TALLY_URL = "http://localhost:9000"

xml_request = """
<ENVELOPE>
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
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>
"""

response = requests.post(TALLY_URL, data=xml_request)
root = ET.fromstring(response.text)

available_items = []

names = root.findall(".//DSPACCNAME")
stocks = root.findall(".//DSPSTKINFO")

for name_node, stock_node in zip(names, stocks):
    name = name_node.find("DSPDISPNAME")
    qty_node = stock_node.find(".//DSPCLQTY")

    item_name = name.text if name is not None else "UNKNOWN"
    qty_text = qty_node.text if (qty_node is not None and qty_node.text) else "0"


    # extract number from "18 NOS"
    match = re.search(r"-?\d+", qty_text)
    qty = int(match.group()) if match else 0

    if qty > 0:
        available_items.append((item_name, qty))

print("TOTAL ITEMS WITH STOCK > 0:", len(available_items))
print("\nFIRST 10 AVAILABLE ITEMS:\n")

for item in available_items[:10]:
    print(item[0], "→", item[1])
```
---
## File: templates/index.html
```text
<!DOCTYPE html>
<html>
<head>
    <title>Tally Stock Viewer</title>
    <style>
        :root {
            --bg: #f3f6fb;
            --panel: #ffffff;
            --text: #132238;
            --muted: #5d6b82;
            --border: #d9e2ec;
            --accent: #1f6feb;
            --accent-dark: #1558c0;
            --success-bg: #e8f5e9;
            --success-text: #276749;
            --error-bg: #fdecea;
            --error-text: #b42318;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            font-family: Arial, sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(31, 111, 235, 0.14), transparent 24%),
                radial-gradient(circle at top right, rgba(20, 184, 166, 0.12), transparent 20%),
                var(--bg);
        }

        .page {
            max-width: 1100px;
            margin: 0 auto;
            padding: 32px 20px 40px;
        }

        .hero {
            margin-bottom: 20px;
        }

        .hero h1 {
            margin: 0 0 8px;
            font-size: 34px;
        }

        .hero p {
            margin: 0;
            color: var(--muted);
        }

        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 20px;
            box-shadow: 0 14px 40px rgba(19, 34, 56, 0.08);
            margin-bottom: 18px;
        }

        .toolbar {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
        }

        label {
            font-weight: 700;
        }

        select, button {
            padding: 11px 14px;
            font-size: 14px;
            border-radius: 10px;
            border: 1px solid var(--border);
        }

        select {
            min-width: 260px;
            background: #fff;
        }

        button {
            background: var(--accent);
            color: white;
            border: 0;
            cursor: pointer;
            transition: transform 0.12s ease, background-color 0.12s ease;
        }

        button:hover {
            background: var(--accent-dark);
            transform: translateY(-1px);
        }

        button.secondary {
            background: #e9eef6;
            color: var(--text);
        }

        button.secondary:hover {
            background: #dbe4f0;
        }

        .meta-row {
            margin-top: 14px;
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            color: var(--muted);
            font-size: 13px;
        }

        .status-line {
            margin-top: 8px;
            font-size: 12px;
        }

        .notice {
            padding: 10px 12px;
            border-radius: 10px;
            margin-bottom: 14px;
            display: none;
        }

        .error {
            color: var(--error-text);
            background: var(--error-bg);
        }

        .success {
            color: var(--success-text);
            background: var(--success-bg);
        }

        .designs-container {
            display: none;
        }

        .designs-container.show {
            display: block;
        }

        .designs-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
            margin-bottom: 14px;
        }

        #designsList {
            list-style: none;
            padding: 0;
            margin: 0;
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 12px;
        }

        .design-item {
            display: flex;
            gap: 14px;
            align-items: flex-start;
            padding: 14px;
            border: 1px solid var(--border);
            border-radius: 14px;
            background: linear-gradient(180deg, #fff, #fafcff);
        }

        .design-thumbnail {
            width: 92px;
            height: 92px;
            border-radius: 12px;
            object-fit: cover;
            flex: 0 0 auto;
            background: #eef2f7;
            border: 1px solid var(--border);
            cursor: zoom-in;
        }

        .thumbnail-button {
            appearance: none;
            border: 0;
            background: transparent;
            padding: 0;
            line-height: 0;
            flex: 0 0 auto;
        }

        .thumbnail-actions {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-top: 8px;
        }

        .topbar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 12px;
        }

        .role-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: #e8eef7;
            color: var(--text);
            font-size: 12px;
            font-weight: 700;
        }

        .design-text {
            flex: 1;
            min-width: 0;
        }

        .design-title {
            font-weight: 700;
            line-height: 1.35;
            margin-bottom: 6px;
            word-break: break-word;
        }

        .design-subtext {
            color: var(--muted);
            font-size: 12px;
            margin-bottom: 8px;
        }

        .design-price {
            color: #0f766e;
            font-size: 13px;
            font-weight: 700;
            margin-bottom: 8px;
        }

        .fix-link {
            display: inline-block;
            text-decoration: none;
            color: var(--accent-dark);
            font-weight: 700;
            font-size: 12px;
        }

        .badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 11px;
            font-weight: 700;
            padding: 5px 9px;
            border-radius: 999px;
            margin-bottom: 8px;
        }

        .badge.mapped {
            color: #166534;
            background: #dcfce7;
        }

        .badge.unmapped {
            color: #92400e;
            background: #fef3c7;
        }

        .empty-state {
            padding: 20px;
            text-align: center;
            color: var(--muted);
        }

        .modal {
            position: fixed;
            inset: 0;
            display: none;
            z-index: 1000;
        }

        .modal.open {
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .modal-backdrop {
            position: absolute;
            inset: 0;
            background: rgba(2, 6, 23, 0.78);
        }

        .modal-panel {
            position: relative;
            z-index: 1;
            width: min(94vw, 1200px);
            max-height: 92vh;
            background: #fff;
            border-radius: 18px;
            overflow: hidden;
            box-shadow: 0 24px 80px rgba(15, 23, 42, 0.35);
            display: grid;
            grid-template-rows: auto 1fr auto;
        }

        .modal-head {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: center;
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
        }

        .modal-body {
            background: #0f172a;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 14px;
        }

        .modal-body img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
        }

        .modal-actions {
            display: flex;
            justify-content: flex-end;
            gap: 10px;
            padding: 12px 16px;
            border-top: 1px solid var(--border);
            background: #f8fafc;
        }

        .viewer-note {
            padding: 12px 14px;
            border-radius: 12px;
            border: 1px solid var(--border);
            background: #f8fafc;
            color: var(--muted);
            font-size: 13px;
            line-height: 1.5;
        }

        @media (max-width: 640px) {
            .page {
                padding: 18px 12px 28px;
            }

            .hero h1 {
                font-size: 28px;
            }

            select {
                min-width: 100%;
                width: 100%;
            }

            button {
                width: 100%;
            }
        }
    </style>
</head>
<body>
<div class="page">
    <div class="hero">
        <div class="topbar">
            <div class="role-badge">{{ current_role|default('customer')|title }} mode</div>
            <a href="/logout" class="fix-link">Logout</a>
        </div>
        <h1>Tally Stock Viewer</h1>
        <p>Browse stock designs and mapped images. Admin can scan, refresh, train mappings, and manage customer pricing.</p>
    </div>

    <div class="panel">
        <div class="toolbar">
            <label for="carSelect">Select Car Model:</label>
            <select id="carSelect">
                <option value="">-- Loading cars --</option>
            </select>
            <button onclick="loadDesigns()">Load Designs</button>
            {% if is_admin %}
            <button class="secondary" onclick="shareVisibleImages()">Share Visible Images</button>
            <button class="secondary" onclick="reloadData()">Reload Data</button>
            <button class="secondary" onclick="refreshStock()">Refresh Stock</button>
            <button onclick="window.location.href='/train'">Train Mappings</button>
            <button class="secondary" onclick="window.location.href='/admin/pricing'">Manage Prices &amp; Customers</button>
            {% endif %}
        </div>

        {% if auto_export_enabled %}
        <p style="margin-top:10px; font-size:12px; color:#0f766e;">Auto-export enabled every {{ refresh_interval }} seconds.</p>
        {% else %}
        <p style="margin-top:10px; font-size:12px; color:#64748b;">Auto-export disabled. Use Refresh Stock to update manually.</p>
        {% endif %}

        {% if is_admin %}
        <div class="meta-row">
            <div id="lastUpdate"></div>
            <div id="statusMessage" class="status-line"></div>
        </div>
        {% else %}
        <div class="viewer-note" style="margin-top:14px;">Customer mode is read-only. You can browse designs and open image previews, but only admin can refresh stock or save mappings.</div>
        {% endif %}
    </div>

    <div id="errorMsg" class="notice error"></div>
    <div id="infoMsg" class="notice success"></div>

    <div id="designsContainer" class="panel designs-container">
        <div class="designs-header">
            <h2 style="margin:0;">Available Designs for <span id="selectedCarName"></span></h2>
            <p id="designCount" style="margin:0; color: var(--muted);"></p>
        </div>
        <ul id="designsList"></ul>
    </div>
</div>

<div class="modal" id="imageModal" aria-hidden="true">
    <div class="modal-backdrop" onclick="closeImageModal()"></div>
    <div class="modal-panel" role="dialog" aria-modal="true" aria-label="Full screen image preview">
        <div class="modal-head">
            <strong id="modalTitle">Image preview</strong>
            <button class="secondary" onclick="closeImageModal()">Close</button>
        </div>
        <div class="modal-body">
            <img id="modalImage" alt="Full screen preview">
        </div>
        <div class="modal-actions">
            <button class="secondary" onclick="copyModalImage()">Copy Image</button>
        </div>
    </div>
</div>

<script>
const isAdmin = {{ is_admin|tojson }};
const isCustomer = {{ is_customer|tojson }};

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function clearMessages() {
    document.getElementById("errorMsg").style.display = "none";
    document.getElementById("infoMsg").style.display = "none";
}

function showError(msg) {
    const el = document.getElementById("errorMsg");
    el.textContent = "❌ " + msg;
    el.style.display = "block";
}

function showInfo(msg) {
    const el = document.getElementById("infoMsg");
    el.textContent = "✓ " + msg;
    el.style.display = "block";
}

function getImageSource(src) {
    return src || "/get_stock_image";
}

function openImageModal(src, label) {
    const modal = document.getElementById("imageModal");
    const modalImage = document.getElementById("modalImage");
    document.getElementById("modalTitle").textContent = label || "Image preview";
    modalImage.src = getImageSource(src);
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
}

function closeImageModal() {
    const modal = document.getElementById("imageModal");
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
}

async function copyImageToClipboard(src) {
    const response = await fetch(getImageSource(src));
    if (!response.ok) {
        throw new Error(`Copy failed (HTTP ${response.status})`);
    }

    const blob = await response.blob();
    if (navigator.clipboard && window.ClipboardItem) {
        try {
            const pngBlob = await convertImageBlobToPng(blob);
            await navigator.clipboard.write([new ClipboardItem({ "image/png": pngBlob })]);
            return;
        } catch (error) {
            console.warn("Clipboard image copy failed, falling back to download.", error);
        }
    }

    const link = document.createElement("a");
    link.href = getImageSource(src);
    link.download = "image";
    document.body.appendChild(link);
    link.click();
    link.remove();
}

async function convertImageBlobToPng(blob) {
    if (blob.type === "image/png") {
        return blob;
    }

    const objectUrl = URL.createObjectURL(blob);
    try {
        const image = new Image();
        image.crossOrigin = "anonymous";
        const loaded = new Promise((resolve, reject) => {
            image.onload = resolve;
            image.onerror = reject;
        });
        image.src = objectUrl;
        await loaded;

        const canvas = document.createElement("canvas");
        canvas.width = image.naturalWidth || image.width;
        canvas.height = image.naturalHeight || image.height;
        const context = canvas.getContext("2d");
        context.drawImage(image, 0, 0);

        const pngBlob = await new Promise((resolve, reject) => {
            canvas.toBlob(result => {
                if (result) {
                    resolve(result);
                } else {
                    reject(new Error("Could not convert image for clipboard."));
                }
            }, "image/png");
        });

        return pngBlob;
    } finally {
        URL.revokeObjectURL(objectUrl);
    }
}

async function shareVisibleImages() {
    try {
        clearMessages();
        if (!isAdmin) {
            showError("Sharing is available for admin users only.");
            return;
        }

        const visibleButtons = Array.from(document.querySelectorAll("#designsList .thumbnail-button[data-src][data-image-id]"))
            .filter(button => {
                const imageId = Number(button.dataset.imageId);
                if (!Number.isFinite(imageId) || imageId <= 0) {
                    return false;
                }
                if (button.offsetParent === null) {
                    return false;
                }
                const style = window.getComputedStyle(button);
                return style.display !== "none" && style.visibility !== "hidden";
            });

        const urls = Array.from(new Set(
            visibleButtons
                .map(button => button.dataset.src || "")
                .filter(Boolean)
        ));

        if (!urls.length) {
            showError("No mapped images are visible to share.");
            return;
        }

        const files = [];
        for (let i = 0; i < urls.length; i += 1) {
            const url = urls[i];
            const response = await fetch(getImageSource(url));
            if (!response.ok) {
                throw new Error(`Image fetch failed (HTTP ${response.status})`);
            }

            const blob = await response.blob();
            const mime = blob.type || "image/jpeg";
            const extension = (mime.split("/")[1] || "jpg").toLowerCase();
            files.push(new File([blob], `image_${i + 1}.${extension}`, { type: mime }));
        }

        if (
            navigator.share &&
            navigator.canShare &&
            navigator.canShare({ files })
        ) {
            await navigator.share({ files, title: "Stock Images" });
            showInfo(`Shared ${files.length} image${files.length === 1 ? "" : "s"}.`);
            return;
        }

        for (let i = 0; i < urls.length; i += 1) {
            const link = document.createElement("a");
            link.href = getImageSource(urls[i]);
            link.download = `image_${i + 1}`;
            document.body.appendChild(link);
            link.click();
            link.remove();
        }
        showInfo(`Share not supported on this browser. Downloaded ${urls.length} image${urls.length === 1 ? "" : "s"} instead.`);
    } catch (error) {
        if (error && error.name === "AbortError") {
            showInfo("Share canceled.");
            return;
        }
        showError(error.message);
    }
}

async function copyModalImage() {
    try {
        const modalImage = document.getElementById("modalImage");
        await copyImageToClipboard(modalImage.src);
        showInfo("Image copied");
    } catch (error) {
        showError(error.message);
    }
}

async function loadCars() {
    try {
        clearMessages();
        const res = await fetch("/cars");

        if (!res.ok) {
            showError(`Failed to load cars (HTTP ${res.status})`);
            return;
        }

        const data = await res.json();
        const select = document.getElementById("carSelect");
        select.innerHTML = '<option value="">-- Select a car --</option>';

        if (Array.isArray(data)) {
            data.forEach(car => {
                const option = document.createElement("option");
                option.value = car;
                option.textContent = car;
                select.appendChild(option);
            });
            showInfo(`Loaded ${data.length} car models`);
        } else {
            showError("Unexpected response format from server");
        }
    } catch (error) {
        showError(`Error loading cars: ${error.message}`);
        document.getElementById("carSelect").innerHTML = '<option value="">Error loading cars</option>';
    }

    updateTimestamp();
}

async function updateTimestamp() {
    try {
        const lastUpdateEl = document.getElementById("lastUpdate");
        const statusEl = document.getElementById("statusMessage");
        if (!lastUpdateEl || !statusEl) {
            return;
        }

        const res = await fetch("/last_update");
        if (res.ok) {
            const data = await res.json();
            if (data.last_update) {
                lastUpdateEl.textContent = `Last stock update: ${data.last_update}`;
            }
        }
    } catch (error) {
        console.log("Could not fetch last update time", error);
    }

    try {
        const statusRes = await fetch("/refresh_status");
        if (statusRes.ok) {
            const status = await statusRes.json();
            if (status.success === false) {
                statusEl.textContent = `Last error: ${status.message}`;
                statusEl.style.color = "#b42318";
            } else if (status.success === true) {
                statusEl.textContent = `Last refresh: ${status.message}`;
                statusEl.style.color = "#475569";
            }
        }
    } catch (error) {
        console.log("Could not fetch refresh status", error);
    }
}

function renderDesignItem(item) {
    const stockItem = item.design || item.raw || "Unknown";
    const mapped = Boolean(item.mapped);
    const thumbnailUrl = item.thumbnail_url || `/get_stock_image?stock_item=${encodeURIComponent(stockItem)}`;
    const fixUrl = item.fix_url || `/train?stock_item=${encodeURIComponent(stockItem)}`;
    const confidence = typeof item.confidence === "number" ? Math.round(item.confidence * 100) : 0;
    const priceText = item.price || "Contact Us";
    const escapedTitle = escapeHtml(stockItem);
    const safeThumbnail = escapeHtml(thumbnailUrl);
    const imageId = mapped && item.image_id ? String(item.image_id) : "";
    const detailText = isCustomer
        ? `<div class="design-price">Price: ${escapeHtml(priceText)}</div>`
        : `
            <div class="design-subtext">Quantity: ${item.qty ?? 0}${mapped ? ` - Confidence: ${confidence}%` : ''}</div>
            <div class="design-price">Price: ${escapeHtml(priceText)}</div>
        `;

    return `
        <li>
            <div class="design-item">
                <button type="button" class="thumbnail-button" data-image-id="${imageId}" data-src="${safeThumbnail}" data-label="${escapedTitle}" onclick="openImageModal(this.dataset.src, this.dataset.label)">
                    <img src="${thumbnailUrl}" alt="${escapedTitle}" class="design-thumbnail">
                </button>
                <div class="design-text">
                    <div class="badge ${mapped ? 'mapped' : 'unmapped'}">${mapped ? 'Mapped' : 'Needs mapping'}</div>
                    <div class="design-title">${escapeHtml(stockItem)}</div>
                    ${detailText}
                    ${mapped ? '' : `<a href="${fixUrl}" class="fix-link">Fix Mapping</a>`}
                    <div class="thumbnail-actions">
                        <button class="secondary" data-src="${safeThumbnail}" onclick="copyImageToClipboard(this.dataset.src)">Copy Image</button>
                    </div>
                </div>
            </div>
        </li>
    `;
}

async function loadDesigns() {
    const car = document.getElementById("carSelect").value;

    if (!car) {
        showError("Please select a car first");
        return;
    }

    try {
        clearMessages();
        const res = await fetch(`/designs?car=${encodeURIComponent(car)}`);

        if (!res.ok) {
            showError(`Failed to load designs (HTTP ${res.status})`);
            return;
        }

        const data = await res.json();
        const list = document.getElementById("designsList");
        const container = document.getElementById("designsContainer");
        list.innerHTML = "";

        if (Array.isArray(data) && data.length > 0) {
            document.getElementById("selectedCarName").textContent = car;
            list.innerHTML = data.map(renderDesignItem).join("");
            document.getElementById("designCount").textContent = `Total: ${data.length} designs with stock`;
            container.classList.add("show");
            showInfo(`Showing ${data.length} designs for ${car}`);
        } else {
            list.innerHTML = '<li><div class="empty-state">No designs found for this car.</div></li>';
            container.classList.add("show");
            showInfo("No designs found");
        }
    } catch (error) {
        showError(`Error loading designs: ${error.message}`);
    }
}

async function reloadData() {
    try {
        clearMessages();
        const res = await fetch("/reload", { method: "POST" });

        if (!res.ok) {
            showError(`Failed to reload data (HTTP ${res.status})`);
            return;
        }

        showInfo("Data reloaded successfully");
        loadCars();
    } catch (error) {
        showError(`Error reloading data: ${error.message}`);
    }
}

async function refreshStock() {
    try {
        clearMessages();
        const res = await fetch("/refresh_stock", { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
            showError(`Stock refresh failed: ${data.error || res.status}`);
            return;
        }

        if (data.tally_online === false) {
            showError(data.warning || "Tally is down; using the last saved upload.");
            return;
        }

        showInfo(data.message || "Stock exported and updated successfully");
        loadCars();
        updateTimestamp();
    } catch (error) {
        showError(`Error refreshing stock: ${error.message}`);
    }
}

window.addEventListener("DOMContentLoaded", loadCars);

document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
        closeImageModal();
    }
});

document.getElementById("imageModal").addEventListener("click", event => {
    if (event.target.id === "imageModal") {
        closeImageModal();
    }
});
</script>
</body>
</html>


```
---
## File: templates/train.html
```text
<!DOCTYPE html>
<html>
<head>
    <title>Training Mode - Image Mapping</title>
    <style>
        :root {
            --bg: #f4f7fb;
            --panel: #ffffff;
            --text: #14213d;
            --muted: #607086;
            --border: #d6e0ea;
            --accent: #0f766e;
            --accent-2: #1d4ed8;
            --warning: #b45309;
            --danger: #b91c1c;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background:
                radial-gradient(circle at top left, rgba(15, 118, 110, 0.14), transparent 26%),
                radial-gradient(circle at top right, rgba(29, 78, 216, 0.12), transparent 22%),
                var(--bg);
            color: var(--text);
        }

        .page {
            max-width: 1200px;
            margin: 0 auto;
            padding: 28px 20px 40px;
        }

        .header {
            margin-bottom: 18px;
        }

        .header h1 {
            margin: 0 0 8px;
            font-size: 34px;
        }

        .header p {
            margin: 0;
            color: var(--muted);
        }

        .grid {
            display: grid;
            grid-template-columns: 1.1fr 0.9fr;
            gap: 18px;
        }

        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 18px;
            box-shadow: 0 14px 40px rgba(20, 33, 61, 0.08);
            padding: 20px;
        }

        .progress-card {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin-bottom: 18px;
        }

        .metric {
            background: linear-gradient(180deg, #fff, #f8fbff);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 14px;
        }

        .metric .label {
            display: block;
            color: var(--muted);
            font-size: 12px;
            margin-bottom: 6px;
        }

        .metric .value {
            font-size: 22px;
            font-weight: 700;
        }

        .preview-wrap {
            display: grid;
            grid-template-columns: 1fr;
            gap: 16px;
        }

        .image-frame {
            position: relative;
            width: 100%;
            aspect-ratio: 1 / 1;
            border-radius: 18px;
            overflow: hidden;
            border: 1px solid var(--border);
            background: #eef2f7;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .image-frame img {
            width: 100%;
            height: 100%;
            object-fit: contain;
            background: linear-gradient(180deg, #ffffff, #eef4fb);
            cursor: zoom-in;
        }

        .image-overlay {
            position: absolute;
            inset: auto 12px 12px 12px;
            display: flex;
            justify-content: space-between;
            gap: 10px;
            flex-wrap: wrap;
            background: rgba(255, 255, 255, 0.88);
            backdrop-filter: blur(8px);
            border: 1px solid rgba(214, 224, 234, 0.8);
            border-radius: 12px;
            padding: 10px 12px;
        }

        .pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            font-weight: 700;
            border-radius: 999px;
            padding: 6px 10px;
            background: #edf2f7;
            color: #334155;
        }

        .details {
            display: grid;
            gap: 8px;
            color: var(--muted);
            font-size: 14px;
        }

        .details strong {
            color: var(--text);
        }

        .form-grid {
            display: grid;
            gap: 14px;
        }

        .field label {
            display: block;
            font-size: 12px;
            font-weight: 700;
            margin-bottom: 8px;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        select, input[type="text"] {
            width: 100%;
            padding: 12px 14px;
            border-radius: 12px;
            border: 1px solid var(--border);
            background: #fff;
            font-size: 14px;
        }

        .suggestion-box {
            border: 1px dashed var(--border);
            border-radius: 14px;
            padding: 14px;
            background: #fbfdff;
        }

        .suggestion-box .title {
            font-size: 12px;
            font-weight: 700;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 8px;
        }

        .suggestion-value {
            font-size: 16px;
            font-weight: 700;
            margin-bottom: 6px;
        }

        .suggestion-meta {
            color: var(--muted);
            font-size: 13px;
        }

        .actions {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }

        button, .link-button {
            padding: 11px 14px;
            border-radius: 12px;
            border: 0;
            font-size: 14px;
            font-weight: 700;
            cursor: pointer;
            text-decoration: none;
            display: inline-flex;
            justify-content: center;
            align-items: center;
        }

        button.primary {
            background: var(--accent);
            color: #fff;
        }

        button.secondary {
            background: #e8eef7;
            color: var(--text);
        }

        button.ghost {
            background: #f3f4f6;
            color: var(--text);
        }

        .link-button {
            background: #f8fafc;
            color: var(--accent-2);
            border: 1px solid var(--border);
        }

        .notice {
            display: none;
            margin-top: 14px;
            padding: 12px 14px;
            border-radius: 12px;
            font-size: 13px;
        }

        .notice.success {
            display: none;
            background: #ecfdf3;
            color: #166534;
        }

        .notice.error {
            display: none;
            background: #fef2f2;
            color: var(--danger);
        }

        .footer-actions {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            margin-top: 18px;
        }

        .topbar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 12px;
        }

        .role-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: #e8eef7;
            color: var(--text);
            font-size: 12px;
            font-weight: 700;
        }

        .viewer-note {
            margin-top: 12px;
            padding: 12px 14px;
            border-radius: 12px;
            background: #f8fafc;
            border: 1px solid var(--border);
            color: var(--muted);
            font-size: 13px;
            line-height: 1.5;
        }

        .modal {
            position: fixed;
            inset: 0;
            display: none;
            z-index: 1000;
        }

        .modal.open {
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .modal-backdrop {
            position: absolute;
            inset: 0;
            background: rgba(2, 6, 23, 0.78);
        }

        .modal-panel {
            position: relative;
            z-index: 1;
            width: min(94vw, 1200px);
            max-height: 92vh;
            background: #fff;
            border-radius: 18px;
            overflow: hidden;
            box-shadow: 0 24px 80px rgba(15, 23, 42, 0.35);
            display: grid;
            grid-template-rows: auto 1fr auto;
        }

        .modal-head {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: center;
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
        }

        .modal-body {
            background: #0f172a;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 14px;
        }

        .modal-body img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
        }

        .modal-actions {
            display: flex;
            justify-content: flex-end;
            gap: 10px;
            padding: 12px 16px;
            border-top: 1px solid var(--border);
            background: #f8fafc;
        }

        .thumbnail-actions {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-top: 8px;
        }

        .muted {
            color: var(--muted);
        }

        @media (max-width: 980px) {
            .grid {
                grid-template-columns: 1fr;
            }

            .progress-card {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        @media (max-width: 640px) {
            .page {
                padding: 18px 12px 28px;
            }

            .header h1 {
                font-size: 28px;
            }

            .progress-card {
                grid-template-columns: 1fr;
            }

            button, .link-button {
                width: 100%;
            }
        }
    </style>
</head>
<body>
<div class="page">
    <div class="header">
        <div class="topbar">
            <div class="role-badge">{{ current_role|default('viewer')|title }} mode</div>
            <a href="/logout" class="link-button">Logout</a>
        </div>
        <h1>Training Mode - Image Mapping</h1>
        <p>Confirm which stock item belongs to each scanned image. Confirmed mappings are saved directly to the local mapping database.</p>
    </div>

    <div class="progress-card" id="progressCard">
        <div class="metric">
            <span class="label">Mapped</span>
            <span class="value" id="mappedCount">{{ mapping_stats.mapped_images }}</span>
        </div>
        <div class="metric">
            <span class="label">Total Images</span>
            <span class="value" id="totalCount">{{ mapping_stats.total_images }}</span>
        </div>
        <div class="metric">
            <span class="label">Remaining</span>
            <span class="value" id="remainingCount">{{ mapping_stats.unmapped_images }}</span>
        </div>
        <div class="metric">
            <span class="label">Complete</span>
            <span class="value" id="percentComplete">{{ mapping_stats.percent_complete }}%</span>
        </div>
    </div>

    <div class="grid">
        <div class="panel preview-wrap">
            <div class="field">
                <label for="ssCarQueueSelect">SS Car Queue</label>
                <select id="ssCarQueueSelect" onchange="loadImageQueue(this.value)"></select>
            </div>

            <div class="field">
                <label for="imageSelect">Images in Selected Car</label>
                <select id="imageSelect" onchange="handleImageSelection()"></select>
            </div>

            <div class="image-frame">
                <img id="imagePreview" alt="Training preview" onclick="openFullscreenImage()">
                <div class="image-overlay">
                    <span class="pill" id="imageIdPill">Image #-</span>
                    <span class="pill" id="imageStatusPill">Awaiting image</span>
                </div>
            </div>

            <div class="details">
                <div>Car Folder: <strong id="carFolderText">-</strong></div>
                <div>Image Name: <strong id="imageNameText">-</strong></div>
                <div>Scan Date: <strong id="scanDateText">-</strong></div>
            </div>

            <div class="footer-actions">
                <a href="/" class="link-button">Back to Viewer</a>
            </div>
        </div>

        <div class="panel">
            <div class="form-grid">
                <div class="field">
                    <label for="carModelFilter">Car Filter</label>
                        <select id="carModelFilter" onchange="applyStockFilter(targetStockItem);">
                        <option value="">All cars</option>
                    </select>
                </div>

                <div class="field">
                    <label for="stockItemSelect">Select Matching Stock Item</label>
                    <select id="stockItemSelect"></select>
                </div>

                {% if is_admin %}
                <div class="actions">
                    <button class="primary" onclick="confirmSelectedMapping()">Confirm Mapping</button>
                    <button class="ghost" onclick="skipCurrentImage()">Skip Image</button>
                    <button class="ghost" onclick="markUnmatchable()">Mark as Unmatchable</button>
                </div>
                {% else %}
                <div class="viewer-note">
                    Viewer mode is read-only. You can browse images and stock items, but only admin can save mappings or refresh stock.
                </div>
                {% endif %}

                <div class="notice success" id="successNotice"></div>
                <div class="notice error" id="errorNotice"></div>

                <div class="muted" style="font-size:12px; line-height:1.5;">
                    Pick an SS image from the queue on the left, choose the matching stock item on the right, then confirm. Skipped and unmatchable images stay out of the queue.
                </div>
            </div>
        </div>
    </div>
</div>

<div class="modal" id="imageModal" aria-hidden="true">
    <div class="modal-backdrop" onclick="closeFullscreenImage()"></div>
    <div class="modal-panel" role="dialog" aria-modal="true" aria-label="Full screen image preview">
        <div class="modal-head">
            <strong id="modalTitle">Image preview</strong>
            <button class="secondary" onclick="closeFullscreenImage()">Close</button>
        </div>
        <div class="modal-body">
            <img id="modalImage" alt="Full screen preview">
        </div>
        <div class="modal-actions">
            <button class="secondary" onclick="copyModalImage()">Copy Image</button>
        </div>
    </div>
</div>

<script>
const initialImage = {{ initial_image | tojson }};
const targetStockItem = {{ target_stock_item | tojson }};
const carModels = {{ car_models | tojson }};
const ssImageFolders = {{ ss_image_folders | tojson }};
const stockItems = {{ stock_items | tojson }};
const isAdmin = {{ is_admin|tojson }};

let imageQueue = [];
let currentImage = null;
let selectedQueueFolder = "";

function normalize(value) {
    return String(value || "").trim().toUpperCase();
}

function showNotice(type, message) {
    const notice = document.getElementById(type === "error" ? "errorNotice" : "successNotice");
    notice.textContent = message;
    notice.style.display = "block";
}

function clearNotices() {
    document.getElementById("successNotice").style.display = "none";
    document.getElementById("errorNotice").style.display = "none";
}

function updateProgress(stats) {
    if (!stats) return;
    document.getElementById("mappedCount").textContent = stats.mapped_images;
    document.getElementById("totalCount").textContent = stats.total_images;
    document.getElementById("remainingCount").textContent = stats.unmapped_images;
    document.getElementById("percentComplete").textContent = `${stats.percent_complete}%`;
}

function getPlaceholderImage() {
    return "data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHZpZXdCb3g9JzAgMCAzMjAgMzIwJz48cmVjdCB3aWR0aD0nMzIwJyBoZWlnaHQ9JzMyMCcgcng9JzI0JyBmaWxsPScjZWVmMmY3Jy8+PHRleHQgeD0nMTYwJyB5PScxNjgnIHRleHQtYW5jaG9yPSdtaWRkbGUnIGZvbnQtZmFtaWx5PSdBcmlhbCcgc3R5bGU9J2ZvbnQtc2l6ZToxOHB4O2ZpbGw6IzU2Njk4MCc+Tm8gaW1hZ2U8L3RleHQ+PC9zdmc+";
}

function getCurrentImageSource() {
    return currentImage ? `/get_image/${currentImage.id}` : getPlaceholderImage();
}

function openFullscreenImage() {
    const modal = document.getElementById("imageModal");
    const modalImage = document.getElementById("modalImage");
    const modalTitle = document.getElementById("modalTitle");
    modalImage.src = getCurrentImageSource();
    modalTitle.textContent = currentImage ? `${currentImage.car_folder || "Image"} · ${currentImage.filename || "Preview"}` : "Image preview";
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
}

function closeFullscreenImage() {
    const modal = document.getElementById("imageModal");
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
}

async function copyModalImage() {
    if (!currentImage) {
        return;
    }

    try {
        const sourceUrl = getCurrentImageSource();
        const response = await fetch(sourceUrl);
        if (!response.ok) {
            throw new Error(`Copy failed (HTTP ${response.status})`);
        }

        const blob = await response.blob();
        let copied = false;
        if (navigator.clipboard && window.ClipboardItem) {
            try {
                await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]);
                copied = true;
            } catch (error) {
                console.warn("Clipboard image copy failed, falling back to download.", error);
            }
        }

        if (!copied) {
            const objectUrl = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = objectUrl;
            link.download = currentImage?.filename || "image";
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(objectUrl);
        }

        showNotice("success", "Image copied.");
    } catch (error) {
        showNotice("error", error.message);
    }
}

function populateCarFilter() {
    const select = document.getElementById("carModelFilter");
    select.innerHTML = '<option value="">All cars</option>';
    carModels.forEach(carModel => {
        const option = document.createElement("option");
        option.value = carModel;
        option.textContent = carModel;
        select.appendChild(option);
    });
}

function populateQueueFolders(selectedFolder = "") {
    const select = document.getElementById("ssCarQueueSelect");
    select.innerHTML = "";

    if (!ssImageFolders.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No SS image folders available";
        select.appendChild(option);
        select.disabled = true;
        return "";
    }

    select.disabled = false;

    ssImageFolders.forEach(folderName => {
        const option = document.createElement("option");
        option.value = folderName;
        option.textContent = folderName;
        select.appendChild(option);
    });

    if (selectedFolder && Array.from(select.options).some(option => option.value === selectedFolder)) {
        select.value = selectedFolder;
    } else {
        select.selectedIndex = 0;
    }

    return select.value || "";
}

function applyStockFilter(preferredStockItem = "") {
    const filterCar = document.getElementById("carModelFilter").value;
    const select = document.getElementById("stockItemSelect");
    const filteredItems = stockItems.filter(item => !filterCar || item.car_model === filterCar);

    select.innerHTML = "";

    if (filteredItems.length === 0) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No stock items available";
        select.appendChild(option);
        return;
    }

    filteredItems.forEach(item => {
        const option = document.createElement("option");
        option.value = item.stock_item_name;
        option.textContent = `${item.stock_item_name} ${item.qty ? `(${item.qty})` : ""}`.trim();
        if (normalize(item.stock_item_name) === normalize(preferredStockItem || targetStockItem)) {
            option.selected = true;
        }
        select.appendChild(option);
    });

    if (select.selectedIndex < 0 && select.options.length > 0) {
        select.selectedIndex = 0;
    }
}

function populateImageDropdown(selectedId = "") {
    const select = document.getElementById("imageSelect");
    select.innerHTML = "";

    // Deduplicate queue by id (preserve order)
    const seen = new Set();
    const uniqueQueue = [];
    for (const img of imageQueue) {
        if (!img || !img.id) continue;
        if (seen.has(String(img.id))) continue;
        seen.add(String(img.id));
        uniqueQueue.push(img);
    }

    imageQueue = uniqueQueue;

    if (!uniqueQueue.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No SS images available in this folder";
        select.appendChild(option);
        select.disabled = true;
        return;
    }

    select.disabled = false;

    uniqueQueue.forEach((image, index) => {
        const option = document.createElement("option");
        option.value = String(image.id);
        const statusLabel = image.mapped ? "Mapped" : "Unmapped";
        option.textContent = `${String(index + 1).padStart(4, "0")} | ${image.filename || "Unnamed"} | ${statusLabel}`;
        select.appendChild(option);
    });

    // If a requested selectedId exists in the options, choose it; otherwise default to first.
    if (selectedId && Array.from(select.options).some(o => o.value === String(selectedId))) {
        select.value = String(selectedId);
    } else if (select.options.length > 0) {
        select.selectedIndex = 0;
    }
}

function updateImageView(imageRecord) {
    currentImage = imageRecord;
    if (!imageRecord) {
        document.getElementById("imagePreview").src = getPlaceholderImage();
        document.getElementById("imageIdPill").textContent = "Queue empty";
        document.getElementById("imageStatusPill").textContent = "No unmapped images remain";
        document.getElementById("carFolderText").textContent = "-";
        document.getElementById("imageNameText").textContent = "-";
        document.getElementById("scanDateText").textContent = "-";
        return;
    }

    document.getElementById("imagePreview").src = `/get_image/${imageRecord.id}`;
    document.getElementById("imageIdPill").textContent = `Image #${imageRecord.id}`;
    document.getElementById("imageStatusPill").textContent = "Ready for training";
    document.getElementById("carFolderText").textContent = imageRecord.car_folder || "-";
    document.getElementById("imageNameText").textContent = imageRecord.filename || "-";
    document.getElementById("scanDateText").textContent = imageRecord.scan_date || "-";
}

function maybeSyncCarFilter(imageRecord) {
    if (!imageRecord || !imageRecord.car_folder) {
        return;
    }

    const select = document.getElementById("carModelFilter");
    if (select.value) {
        return;
    }

    const exactMatch = carModels.find(carModel => normalize(carModel) === normalize(imageRecord.car_folder));
    if (exactMatch) {
        select.value = exactMatch;
    }
    applyStockFilter(targetStockItem);
}

async function openImage(imageRecord, pushHistory = true) {
    if (!imageRecord) {
        updateImageView(null);
        return;
    }

    updateImageView(imageRecord);
    maybeSyncCarFilter(imageRecord);

    if (pushHistory) {
        const select = document.getElementById("imageSelect");
        select.value = String(imageRecord.id);
    }
}

function handleImageSelection() {
    clearNotices();
    const select = document.getElementById("imageSelect");
    const raw = select.value || "";
    const imageId = Number(raw);
    if (!raw || !Number.isFinite(imageId)) {
        showNotice("error", "Selected image is not available in the queue.");
        return;
    }

    const imageRecord = imageQueue.find(image => Number(image.id) === imageId);
    if (!imageRecord) {
        // If not found in current array (race), try reloading the queue once
        loadImageQueue(selectedQueueFolder).catch(() => {});
        showNotice("error", "Selected image not found in current queue; reloading.");
        return;
    }

    openImage(imageRecord, false);
}

async function submitMapping(stockItemName, confidence, confirmedBy) {
    if (!isAdmin) {
        showNotice("error", "Viewer mode is read-only.");
        return;
    }

    if (!currentImage || !currentImage.id) {
        showNotice("error", "No current image loaded.");
        return;
    }

    const response = await fetch("/confirm_mapping", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            image_id: currentImage.id,
            stock_item_name: stockItemName || "",
            confidence: confidence,
            confirmed_by: confirmedBy,
            car_model: document.getElementById("carModelFilter").value || currentImage.car_folder || ""
        })
    });

    const payload = await response.json();
    if (!response.ok) {
        throw new Error(payload.error || `Save failed (HTTP ${response.status})`);
    }

    updateProgress(payload.stats || null);
    showNotice("success", "Mapping saved.");

    const queueFolder = document.getElementById("ssCarQueueSelect").value || "";
    const showingFolderQueue = Boolean(queueFolder);
    const currentIndex = imageQueue.findIndex(image => image.id === currentImage.id);
    const nextImage = imageQueue[currentIndex + 1] || imageQueue[currentIndex - 1] || null;

    if (!showingFolderQueue) {
        imageQueue = imageQueue.filter(image => image.id !== currentImage.id);
        populateImageDropdown(nextImage ? nextImage.id : "");
    } else {
        // In folder mode we show all images, so refresh and keep browsing.
        await loadImageQueue(queueFolder);
    }

    if (nextImage) {
        await openImage(nextImage, true);
        return;
    }

    updateImageView(null);
}

async function confirmSelectedMapping() {
    try {
        const stockItem = document.getElementById("stockItemSelect").value;
        if (!stockItem) {
            showNotice("error", "Select a stock item first.");
            return;
        }
        await submitMapping(stockItem, 1.0, "human");
    } catch (error) {
        showNotice("error", error.message);
    }
}

async function skipCurrentImage() {
    try {
        await submitMapping("", 0.0, "skipped");
    } catch (error) {
        showNotice("error", error.message);
    }
}

async function markUnmatchable() {
    try {
        await submitMapping("__UNMATCHABLE__", 0.0, "unmatchable");
    } catch (error) {
        showNotice("error", error.message);
    }
}

async function loadImageQueue(folderName = "") {
    // Only request images for the selected SS car folder to avoid loading the full queue.
    const queueFolder = folderName || document.getElementById("ssCarQueueSelect").value || "";
    selectedQueueFolder = queueFolder;
    const url = queueFolder ? `/train_images?folder=${encodeURIComponent(queueFolder)}` : `/train_images?limit=200`;
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`Could not load image queue (HTTP ${response.status})`);
    }

    const payload = await response.json();
    imageQueue = payload.images || [];
    updateProgress(payload.stats || null);

    const preferredId = initialImage && initialImage.id ? initialImage.id : (imageQueue[0] && imageQueue[0].id) || "";
    populateImageDropdown(preferredId);

    if (initialImage && initialImage.id) {
        const initialMatch = imageQueue.find(image => image.id === initialImage.id);
        if (initialMatch) {
            await openImage(initialMatch, true);
            return;
        }
    }

    if (imageQueue.length > 0) {
        await openImage(imageQueue[0], true);
        return;
    }

    updateImageView(null);
}

async function initializePage() {
    const initialFolder = initialImage && initialImage.car_folder ? initialImage.car_folder : "";
    const selectedFolder = populateQueueFolders(initialFolder);
    populateCarFilter();
    applyStockFilter(targetStockItem);
    await loadImageQueue(selectedFolder);
}

window.addEventListener("DOMContentLoaded", () => {
    initializePage().catch(error => {
        showNotice("error", error.message);
    });

    document.getElementById("imageModal").addEventListener("click", event => {
        if (event.target.id === "imageModal") {
            closeFullscreenImage();
        }
    });

    document.addEventListener("keydown", event => {
        if (event.key === "Escape") {
            closeFullscreenImage();
        }
    });
});
</script>
</body>
</html>
```
---
## File: templates/login.html
```text
<!DOCTYPE html>
<html>
<head>
    <title>Login - Tally Stock Viewer</title>
    <style>
        :root {
            --bg: #f4f7fb;
            --panel: #ffffff;
            --text: #14213d;
            --muted: #607086;
            --border: #d6e0ea;
            --accent: #0f766e;
            --danger: #b91c1c;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background:
                radial-gradient(circle at top left, rgba(15, 118, 110, 0.14), transparent 26%),
                radial-gradient(circle at top right, rgba(29, 78, 216, 0.12), transparent 22%),
                var(--bg);
            color: var(--text);
        }

        .page {
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 24px;
        }

        .panel {
            width: min(100%, 520px);
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 20px;
            padding: 28px;
            box-shadow: 0 18px 50px rgba(20, 33, 61, 0.1);
        }

        h1 {
            margin: 0 0 8px;
            font-size: 32px;
        }

        p {
            margin: 0 0 18px;
            color: var(--muted);
            line-height: 1.5;
        }

        .field {
            margin-bottom: 14px;
        }

        label {
            display: block;
            font-size: 12px;
            font-weight: 700;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 8px;
        }

        input {
            width: 100%;
            padding: 12px 14px;
            border-radius: 12px;
            border: 1px solid var(--border);
            background: #fff;
            font-size: 14px;
        }

        .notice {
            margin-bottom: 14px;
            padding: 12px 14px;
            border-radius: 12px;
            background: #fef2f2;
            color: var(--danger);
            border: 1px solid #fecaca;
            font-size: 13px;
        }

        .hint {
            margin-top: 10px;
            color: var(--muted);
            font-size: 13px;
            line-height: 1.5;
        }

        .actions {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-top: 18px;
        }

        button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            text-decoration: none;
            border-radius: 12px;
            border: 0;
            padding: 12px 16px;
            font-size: 14px;
            font-weight: 700;
            cursor: pointer;
            background: var(--accent);
            color: #fff;
        }

        @media (max-width: 640px) {
            .page {
                padding: 14px;
            }

            .panel {
                padding: 20px;
            }

            .actions button {
                width: 100%;
            }

            h1 {
                font-size: 28px;
            }
        }
    </style>
</head>
<body>
<div class="page">
    <form class="panel" method="post" action="/login">
        <h1>Sign in</h1>
        <p>Use your assigned username and access code.</p>

        {% if error %}
        <div class="notice">{{ error }}</div>
        {% endif %}

        <input type="hidden" name="next" value="{{ next_url }}">

        <div class="field">
            <label for="username">Username</label>
            <input id="username" name="username" type="text" autocomplete="username" required>
        </div>

        <div class="field">
            <label for="access_code">Access Code</label>
            <input id="access_code" name="access_code" type="password" autocomplete="current-password" required>
        </div>

        <div class="hint">
            Ask admin if you need account access or code reset.
        </div>

        <div class="actions">
            <button type="submit">Continue</button>
        </div>
    </form>
</div>
</body>
</html>
```
---
## File: templates/pricing.html
```text
<!DOCTYPE html>
<html>
<head>
    <title>Pricing Admin - Tally Stock Viewer</title>
    <style>
        :root {
            --bg: #f3f6fb;
            --panel: #ffffff;
            --text: #132238;
            --muted: #5d6b82;
            --border: #d9e2ec;
            --accent: #1f6feb;
            --success-bg: #e8f5e9;
            --success-text: #276749;
            --error-bg: #fdecea;
            --error-text: #b42318;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            font-family: Arial, sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(31, 111, 235, 0.14), transparent 24%),
                radial-gradient(circle at top right, rgba(20, 184, 166, 0.12), transparent 20%),
                var(--bg);
        }

        .page {
            max-width: 1320px;
            margin: 0 auto;
            padding: 28px 20px 40px;
        }

        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 18px;
            box-shadow: 0 14px 40px rgba(19, 34, 56, 0.08);
            margin-bottom: 16px;
        }

        .topbar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 12px;
        }

        .role-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: #e8eef7;
            color: var(--text);
            font-size: 12px;
            font-weight: 700;
        }

        .toolbar {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
        }

        .grid {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 16px;
        }

        label {
            font-weight: 700;
            color: var(--muted);
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        select, input, button {
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid var(--border);
            font-size: 14px;
        }

        select, input {
            background: #fff;
        }

        button {
            background: var(--accent);
            color: #fff;
            border: 0;
            cursor: pointer;
        }

        button.secondary {
            background: #e8eef7;
            color: var(--text);
        }

        .notice {
            display: none;
            padding: 10px 12px;
            border-radius: 10px;
            margin-bottom: 14px;
            font-size: 13px;
        }

        .notice.success {
            color: var(--success-text);
            background: var(--success-bg);
        }

        .notice.error {
            color: var(--error-text);
            background: var(--error-bg);
        }

        table {
            width: 100%;
            border-collapse: collapse;
            min-width: 980px;
        }

        th, td {
            padding: 10px;
            border-bottom: 1px solid var(--border);
            vertical-align: top;
            text-align: left;
        }

        th {
            color: var(--muted);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .table-wrap {
            overflow-x: auto;
        }

        .muted {
            color: var(--muted);
            font-size: 13px;
        }

        .field-row {
            display: grid;
            gap: 8px;
            margin-bottom: 12px;
        }

        .inline {
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }

        @media (max-width: 980px) {
            .grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
<div class="page">
    <div class="topbar">
        <div class="role-badge">{{ current_role|default('admin')|title }} mode</div>
        <div class="toolbar">
            <button class="secondary" onclick="window.location.href='/'">Back to Viewer</button>
            <button class="secondary" onclick="window.location.href='/train'">Train Mappings</button>
            <button class="secondary" onclick="window.location.href='/logout'">Logout</button>
        </div>
    </div>

    <div class="grid">
        <div class="panel">
            <h1 style="margin:0 0 10px;">Manage Prices &amp; Customers</h1>
            <p class="muted" style="margin:0 0 16px;">Use Global mode to set base prices. Switch to a customer to set customer-specific prices.</p>

            <div class="toolbar" style="margin-bottom:10px;">
                <label for="customerSelect">Pricing Mode</label>
                <select id="customerSelect" onchange="switchCustomer(this.value)">
                    <option value="global" {% if selected_mode == 'global' %}selected{% endif %}>Global (Base Prices)</option>
                    {% for customer in customers %}
                    <option value="{{ customer.id }}" {% if selected_mode == 'customer' and selected_customer_id == customer.id %}selected{% endif %}>{{ customer.username }}</option>
                    {% endfor %}
                </select>
            </div>

            <div id="customerToggleRow" class="toolbar" {% if selected_mode != 'customer' %}style="display:none;"{% endif %}>
                <button id="toggleContactBtn" type="button" onclick="toggleContactUs()">Toggle Contact Us</button>
                <span class="muted" id="contactState"></span>
            </div>
        </div>

        <div class="panel">
            <h2 style="margin:0 0 12px;">Create New Customer Account</h2>
            <form method="post" action="/admin/create_user">
                <div class="field-row">
                    <label for="new_username">Username</label>
                    <input id="new_username" name="username" type="text" required>
                </div>
                <div class="field-row">
                    <label for="new_access_code">Access Code</label>
                    <input id="new_access_code" name="access_code" type="text" required>
                </div>
                <button type="submit">Create Account</button>
            </form>
        </div>
    </div>

    <div id="successMsg" class="notice success"></div>
    <div id="errorMsg" class="notice error"></div>

    <div class="panel table-wrap">
        <table>
            <thead id="pricingHead"></thead>
            <tbody id="pricingBody"></tbody>
        </table>
        <div id="emptyState" class="muted" style="display:none; margin-top:10px;">No stock items found.</div>
    </div>
</div>

<script>
const selectedMode = {{ selected_mode|tojson }};
const selectedCustomerId = {{ selected_customer_id|tojson }};
const initialStatus = {{ status_message|tojson }};
const initialError = {{ error_message|tojson }};
let currentCustomer = null;
let currentRows = [];

function showSuccess(message) {
    const el = document.getElementById("successMsg");
    el.textContent = message;
    el.style.display = "block";
    document.getElementById("errorMsg").style.display = "none";
}

function showError(message) {
    const el = document.getElementById("errorMsg");
    el.textContent = message;
    el.style.display = "block";
    document.getElementById("successMsg").style.display = "none";
}

function switchCustomer(customerId) {
    const value = (customerId || "global").trim();
    window.location.href = `/admin/pricing?customer_id=${encodeURIComponent(value)}`;
}

async function toggleContactUs() {
    if (selectedMode !== "customer" || !selectedCustomerId) {
        return;
    }

    try {
        const response = await fetch('/admin/toggle_contact_us', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ customer_id: selectedCustomerId })
        });

        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error || `Toggle failed (HTTP ${response.status})`);
        }

        showSuccess('Contact Us mode updated.');
        const forceEnabled = Boolean(payload.force_contact_us);
        const button = document.getElementById('toggleContactBtn');
        const state = document.getElementById('contactState');
        if (button) {
            button.textContent = forceEnabled ? 'Disable Contact Us' : 'Enable Contact Us';
        }
        if (state) {
            state.textContent = `Current: ${forceEnabled ? 'Contact Us ON' : 'Contact Us OFF'}`;
        }
    } catch (error) {
        showError(error.message);
    }
}

function renderPricingTable(mode, rows) {
    const head = document.getElementById('pricingHead');
    const body = document.getElementById('pricingBody');
    const empty = document.getElementById('emptyState');

    if (!Array.isArray(rows) || rows.length === 0) {
        head.innerHTML = '';
        body.innerHTML = '';
        empty.style.display = 'block';
        return;
    }

    empty.style.display = 'none';

    if (mode === 'global') {
        head.innerHTML = `
            <tr>
                <th style="width:120px;">Car Model</th>
                <th>Stock Item</th>
                <th style="width:280px;">Base Price</th>
                <th style="width:130px;">Action</th>
            </tr>
        `;

        body.innerHTML = rows.map(row => `
            <tr data-stock-item="${escapeHtml(row.stock_item_name)}">
                <td>${escapeHtml(row.car_model || '')}</td>
                <td>${escapeHtml(row.stock_item_name || '')}</td>
                <td><input type="text" class="base-price" value="${escapeHtml(row.base_price || '')}" placeholder="e.g. Rs 1500"></td>
                <td><button type="button" class="secondary" onclick="saveRow(this)">Save</button></td>
            </tr>
        `).join('');
        return;
    }

    const customerLabel = currentCustomer && currentCustomer.username ? currentCustomer.username : 'Customer';
    head.innerHTML = `
        <tr>
            <th style="width:120px;">Car Model</th>
            <th>Stock Item</th>
            <th style="width:220px;">Custom Price (${escapeHtml(customerLabel)})</th>
            <th style="width:220px;">Default Base Price</th>
            <th style="width:130px;">Action</th>
        </tr>
    `;

    body.innerHTML = rows.map(row => {
        const defaultText = row.default_display || 'Contact Us';
        return `
            <tr data-stock-item="${escapeHtml(row.stock_item_name)}">
                <td>${escapeHtml(row.car_model || '')}</td>
                <td>${escapeHtml(row.stock_item_name || '')}</td>
                <td><input type="text" class="custom-price" value="${escapeHtml(row.custom_price || '')}" placeholder="Leave blank to use default"></td>
                <td><span class="muted">Default: ${escapeHtml(defaultText)}</span></td>
                <td><button type="button" class="secondary" onclick="saveRow(this)">Save</button></td>
            </tr>
        `;
    }).join('');
}

function escapeHtml(value) {
    return String(value || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

async function loadPricingData() {
    try {
        const customerParam = selectedMode === 'customer' && selectedCustomerId ? String(selectedCustomerId) : 'global';
        const response = await fetch(`/admin/pricing_data?customer_id=${encodeURIComponent(customerParam)}`);
        const payload = await response.json();

        if (!response.ok) {
            throw new Error(payload.error || `Failed to load pricing data (HTTP ${response.status})`);
        }

        currentCustomer = payload.selected_customer || null;
        currentRows = Array.isArray(payload.rows) ? payload.rows : [];

        const toggleRow = document.getElementById('customerToggleRow');
        const toggleBtn = document.getElementById('toggleContactBtn');
        const contactState = document.getElementById('contactState');
        if (payload.mode === 'customer' && currentCustomer && toggleRow) {
            toggleRow.style.display = '';
            const forceEnabled = Boolean(currentCustomer.force_contact_us);
            if (toggleBtn) {
                toggleBtn.textContent = forceEnabled ? 'Disable Contact Us' : 'Enable Contact Us';
            }
            if (contactState) {
                contactState.textContent = `Current: ${forceEnabled ? 'Contact Us ON' : 'Contact Us OFF'}`;
            }
        } else if (toggleRow) {
            toggleRow.style.display = 'none';
        }

        renderPricingTable(payload.mode || 'global', currentRows);
    } catch (error) {
        showError(error.message);
    }
}

async function saveRow(button) {
    const row = button.closest('tr');
    if (!row) {
        return;
    }

    const stockItem = row.dataset.stockItem || '';
    if (!stockItem) {
        showError('Missing stock item name.');
        return;
    }

    try {
        const payload = {
            mode: selectedMode,
            stock_item_name: stockItem,
        };

        if (selectedMode === 'global') {
            payload.base_price = row.querySelector('.base-price')?.value || '';
            payload.custom_price = '';
        } else {
            payload.customer_id = selectedCustomerId;
            payload.base_price = '';
            payload.custom_price = row.querySelector('.custom-price')?.value || '';
        }

        const response = await fetch('/admin/save_price', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.error || `Save failed (HTTP ${response.status})`);
        }

        showSuccess(`Saved price for ${stockItem}`);
        await loadPricingData();
    } catch (error) {
        showError(error.message);
    }
}

window.addEventListener('DOMContentLoaded', () => {
    if (initialStatus) {
        showSuccess(initialStatus);
    }
    if (initialError) {
        showError(initialError);
    }
    loadPricingData();
});
</script>
</body>
</html>
```
