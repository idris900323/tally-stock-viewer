# Tally Sync & Integration Process Documentation

## Overview

This document details how the application integrates with Tally ERP for stock management, processes Excel files, and filters items using a two-dropdown system to show available stock.

---

## 1. Tally Sync & Integration Process

### 1.1 High-Level Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    Tally ERP Server                         │
│                  (Running on localhost)                      │
└────────────────────────┬────────────────────────────────────┘
                         │
                         │ XML Request (POST)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        fetch_from_tally_with_retry()                        │
│  • Sends XML to Tally via HTTP POST                         │
│  • Retry logic with exponential backoff                     │
│  • Timeout handling (120s default)                          │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ XML Response
                       ▼
┌─────────────────────────────────────────────────────────────┐
│    Parse Stock Items & Generate Excel Files                 │
│  • fetch_item_stock_flat()                                  │
│  • Parse response into stock items list                     │
│  • Generate item stock list.auto.xlsx                       │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│    Cache & Runtime Storage                                  │
│  • item stock list.auto.json (JSON cache)                   │
│  • In-memory STOCK_QTY_CACHE                                │
│  • Update every X seconds (configurable)                    │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 Tally Connection with Retry Logic

**File:** `tally/sync.py`

```python
def fetch_from_tally_with_retry(session, url, xml_request, timeout=120, max_retries=3, logger=None):
    """Post XML to Tally with retries and exponential backoff."""
    last_error = None
    for attempt in range(max_retries):
        try:
            if logger:
                logger.info("[Tally] Attempt %s/%s", attempt + 1, max_retries)
            response = session.post(url, data=xml_request, timeout=timeout)
            response.raise_for_status()
            if logger:
                logger.info("[Tally] Success on attempt %s", attempt + 1)
            return response
        except requests.Timeout as exc:
            last_error = exc
            wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
            if logger:
                logger.warning("[Tally] Timeout on attempt %s; retrying in %ss", attempt + 1, wait_time)
            time.sleep(wait_time)
        except requests.RequestException as exc:
            last_error = exc
            wait_time = 2 ** attempt
            if logger:
                logger.warning("[Tally] Request error on attempt %s; retrying in %ss: %s", attempt + 1, wait_time, exc)
            time.sleep(wait_time)

    if last_error is not None:
        raise last_error
    raise requests.RequestException("Tally request failed")
```

**Features:**
- **Exponential Backoff:** Retries at 1s, 2s, 4s intervals to avoid overwhelming Tally
- **Timeout Handling:** 120 seconds default timeout for long-running exports
- **Graceful Degradation:** Falls back to last cached data if Tally is unavailable

### 1.3 Item Stock Export from Tally

**File:** `app.py` - `fetch_item_stock_flat()` function

```python
def fetch_item_stock_flat():
    """Export stock items and quantities from Tally.
    
    Process:
    1. Fetch Stock Item master names (all items in Tally)
    2. Fetch Stock Summary (grouped/parent quantities)
    3. Fetch detailed rows with quantities
    4. Filter: keep only items in Stock Item master
    5. Filter: remove obvious group names
    6. Optional: align with main hierarchy if available
    7. Export to Excel and JSON cache
    """
    
    def _norm(text: str) -> str:
        """Normalize text: uppercase, no special chars"""
        return re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).strip()
    
    def _fetch_stock_item_master_names():
        """Get all Stock Items defined in Tally."""
        # Sends XML request to Tally for complete item list
        # Returns set of normalized item names
        
    def _fetch_rows(detailed=False):
        """Fetch stock quantities from Tally.
        
        Args:
            detailed: If True, fetch all item quantities. 
                     If False, fetch only group summaries.
        Returns:
            list: [{"item_name": str, "qty": int, "upper_name": str}, ...]
        """
    
    # 1. Get master names from Tally
    master_name_set = _fetch_stock_item_master_names()
    main_name_set = _load_main_name_set()
    summary_rows = _fetch_rows(detailed=False)  # Group totals
    detailed_rows = _fetch_rows(detailed=True)   # All items
    
    # 2. Primary filter: keep only Stock Item master items
    rows = [
        {"item_name": r["item_name"], "qty": r["qty"]}
        for r in detailed_rows
        if r["upper_name"] in master_name_set
    ]
    
    # 3. Secondary filter: remove group names
    group_name_set = {r["upper_name"] for r in summary_rows}
    rows = [r for r in rows if _norm(r["item_name"]) not in group_name_set]
    
    # 4. Optional strict alignment with main.xlsx hierarchy
    if main_name_set:
        aligned_rows = [r for r in rows if _norm(r["item_name"]) in main_name_set]
        if aligned_rows:
            rows = aligned_rows
    
    # 5. Create DataFrame and export
    stock_df = pd.DataFrame(rows)
    stock_df = stock_df.drop_duplicates(subset=["item_name"], keep="last")
    
    # 6. Write to Excel
    stock_df.to_excel(ITEM_STOCK_FILE_AUTO, index=False)
    
    # 7. Cache as JSON for faster lookups
    with open(ITEM_STOCK_CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(stock_df.to_dict(orient="records"), f)
    
    return {"rows": len(stock_df), "file": ITEM_STOCK_FILE_AUTO}
```

### 1.4 Automatic Polling Schedule

```python
def schedule_item_export():
    """Automatically export from Tally every X seconds.
    
    Configured via: TALLY_EXPORT_INTERVAL (default: 300s = 5 minutes)
    """
    global item_export_timer, last_refresh_status
    
    if not EXPORT_LOCK.acquire(blocking=False):
        # Skip if previous export still running
        logger.warning("Skipping scheduled export; previous export still running")
        item_export_timer = threading.Timer(ITEM_EXPORT_INTERVAL, schedule_item_export)
        item_export_timer.daemon = True
        item_export_timer.start()
        return
    
    try:
        export_result = fetch_item_stock_flat()
        load_data()  # Reload in-memory caches
        last_refresh_status = {
            "success": True,
            "message": "Stock updated successfully",
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
    
    # Schedule next export
    item_export_timer = threading.Timer(ITEM_EXPORT_INTERVAL, schedule_item_export)
    item_export_timer.daemon = True
    item_export_timer.start()
```

---

## 2. Excel Files in Data Folder

### 2.1 File Overview

| File | Purpose | Source | Frequency | Usage |
|------|---------|--------|-----------|-------|
| `car master list.xls` | Dropdown list of car models | Manual upload | Static | Populate first dropdown (car selection) |
| `main.xlsx` / `main.xls` | Parent-child hierarchy | Manual export from Tally | Static | Define item relationships, used for hierarchy parsing |
| `item stock list.xls` | Stock quantities (manual) | Manual export from Tally | Manual | Backup stock data (rarely used now) |
| `item stock list.auto.xlsx` | Auto-exported stock items | Automatic Tally export | Every 5 min | Latest quantities from Tally |
| `item stock list.auto.json` | JSON cache of stock | Auto-generated | Every 5 min | Fast in-memory lookups |

### 2.2 Car Master List (`car master list.xls`)

**Purpose:** Provides dropdown options for car model selection.

**Format:** Single column, one car model per row.

---

## Implementation Notes — Tallyv2 `smart_sync.py`

- The `tallyv2/smart_sync.py` script implements the documented two-step export pattern to avoid broken collection XML and to reliably filter group/category rows.

- Export pattern used:
    1. `List of Stock Items` — parsed to build `master_name_set` (all STOCKITEM master names)
    2. `Stock Summary` — parsed to build `group_name_set` and extract item rows with quantities

- Parsing rules:
    - Normalize names by uppercasing and removing non-alphanumerics (see `normalize_name()`)
    - Keep only rows whose normalized name exists in `master_name_set`
    - Exclude rows whose normalized name exists in `group_name_set` to avoid categories
    - Deduplicate by normalized item name

- Configuration & runtime:
    - `config.yaml` controls `tally_url`, `cloud_webhook`, `webhook_secret`, and `sync_interval`.
    - Run one cycle for testing: `python tallyv2/smart_sync.py --test`
    - Run as silent daemon on Windows: `pyw.exe C:\tally_sync\smart_sync.py --daemon` or use `Start_Tally_Sync.bat` which calls `pyw.exe`.

- Webhook format and signing:
    - JSON payload contains `timestamp`, `items` (list of `{item_name, qty}`), and `count`.
    - Header `X-Webhook-Signature` = HMAC-SHA256(payload_json, webhook_secret).

- Notes on scheduler and reliability:
    - The script uses `threading.Timer` and `EXPORT_LOCK` to avoid overlapping runs.
    - `sync_interval` from `config.yaml` is used to schedule the next run.
    - `fetch_from_tally_with_retry()` implements exponential backoff and proper exception propagation.

If you want, I can produce a small test harness that mocks Tally HTTP responses and validates the parser against sample XML files.

**Example:**
```
ACCESSORIES (NECK REST)
A-STAR (1 PCS)
A-STAR (2 PCS)
ALTO K-10 (2014)&ALTO (800)LXI
ALTIS
BALENO (2015)(2PCS)
BOLERO (7) 2012
BOLERO NEO ARM PLUS
...
ZS - EV FOOT MAT
```

**Loading in app.py:**
```python
def load_data(refresh_first: bool = False):
    """Load car models from car master list."""
    global CAR_GROUPS, CAR_DESIGN_MAP
    
    try:
        car_df = pd.read_excel(CAR_FILE)  # CAR_FILE = "data/car master list.xls"
        car_groups = (
            car_df.iloc[:, 0]  # First column only
            .dropna()
            .astype(str)
            .str.strip()
            .tolist()
        )
        
        # Filter to relevant range (optional)
        start_marker = "ACCESSORIES (NECK REST)"
        end_marker = "ZS - EV FOOT MAT"
        if start_marker in car_groups and end_marker in car_groups:
            start_idx = car_groups.index(start_marker)
            end_idx = car_groups.index(end_marker)
            car_groups = car_groups[start_idx : end_idx + 1]
        
        CAR_GROUPS = car_groups
        print(f"[OK] Loaded {len(CAR_GROUPS)} car models from dropdown")
    
    except Exception as exc:
        print(f"[ERROR] Error loading car master list: {exc}")
```

### 2.3 Main Hierarchy File (`main.xlsx` or `main.xls`)

**Purpose:** Defines parent-child relationships for stock items.

**Format:** Two columns
- Column A: Item name (parent or child)
- Column B: Quantity (total for parent, individual for children)

**Example:**
```
Item Name                           | Qty
------------------------------------|-----
ALTO (800)(2012) PLAIN              | 45
    RUBBER KITS (FRONT DOOR)        | 12
    RUBBER KITS (REAR DOOR)         | 15
    RUBBER STRIP (WEATHERING)       | 18
ALTO K-10 (2014)                    | 30
    DOOR HANDLE CHROME INNER        | 30
    DOOR LATCH ASSY                 | 25
...
```

**Why it matters:**
- The app searches this file when a car is selected
- It uses **quantity matching** to find which items belong to that car
- The sum of child quantities should equal the parent's quantity

### 2.4 Item Stock Files

#### Auto-Exported Excel (`item stock list.auto.xlsx`)

**Purpose:** Latest stock quantities from Tally, auto-updated every 5 minutes.

**Format:**
```
item_name           | qty
--------------------|-----
RUBBER KITS (...)   | 45
DOOR HANDLE (...)   | 30
WINDOW REGULATOR    | 12
...
```

**Generated by:** `fetch_item_stock_flat()` function

#### JSON Cache (`item stock list.auto.json`)

**Purpose:** Fast in-memory lookups without parsing Excel repeatedly.

**Format:**
```json
[
  {"item_name": "RUBBER KITS (FRONT DOOR)", "qty": 45},
  {"item_name": "DOOR HANDLE CHROME INNER", "qty": 30},
  {"item_name": "WINDOW REGULATOR", "qty": 12}
]
```

**Used by:** `_find_children_by_qty()` and stock quantity lookups

---

## 3. Two-Dropdown System & Stock Filtering

### 3.1 Architecture Overview

```
┌────────────────────────────────────────┐
│      USER SELECTS FROM DROPDOWN 1      │
│           (Car Model)                  │
│  e.g., "ALTO K-10 (2014)"             │
└────────────────┬───────────────────────┘
                 │
                 ▼
    ┌──────────────────────────────┐
    │  search_cars() API           │
    │  (api/search.py)             │
    │  Returns fuzzy-matched cars  │
    └──────────────────────────────┘
                 │
                 ▼
    ┌──────────────────────────────┐
    │ _find_children_by_qty()      │
    │ (Quantity Matching Logic)    │
    │ Searches main.xlsx:          │
    │ - Find parent row            │
    │ - Get parent's total qty     │
    │ - Collect child items        │
    │ - Sum until qty matches      │
    └──────────────────────────────┘
                 │
                 ▼
┌────────────────────────────────────────┐
│   DROPDOWN 2 POPULATED                 │
│   (Available Stock Items for Car)      │
│                                        │
│  ✓ RUBBER KITS (FRONT DOOR) - Qty: 12 │
│  ✓ DOOR HANDLE CHROME INNER - Qty: 30 │
│  ✓ WINDOW REGULATOR - Qty: 18         │
└────────────────────────────────────────┘
```

### 3.2 Dropdown 1: Car Selection

**Frontend:** Two-way binding with Select2 library

**Backend API:** `/api/search_cars`

```python
@search_bp.route("/api/search_cars")
def search_cars():
    """Search/autocomplete for car models.
    
    Query params:
        q: search query string
        page: pagination (1-indexed)
        per_page: results per page (max 50)
    """
    page = request.args.get("page", 1, type=int)
    per_page = min(max(request.args.get("per_page", 30, type=int), 1), 50)
    query = request.args.get("q", "")
    
    # Get all car models from CAR_GROUPS
    car_models = _get_dependency("get_car_models")()
    
    # Filter by query using normalized text matching
    normalized_query = normalize_text(query)
    if not normalized_query:
        matches = list(car_models)
    else:
        matches = [
            value for value in car_models 
            if normalized_query in normalize_text(value)
        ]
    
    # Paginate
    start = (page - 1) * per_page
    end = start + per_page
    page_values = matches[start:end]
    has_more = end < len(matches)
    
    return jsonify({
        "results": [{"id": value, "text": value} for value in page_values],
        "pagination": {"more": has_more},
    })
```

### 3.3 Dropdown 2: Stock Items for Selected Car

**Key Function:** `_find_children_by_qty()` - Intelligent Quantity Matching

```python
def _find_children_by_qty(car_name: str):
    """
    Find stock items for a car using quantity-based matching.
    
    Algorithm:
    1. Normalize car name
    2. Load main.xlsx into memory (cached)
    3. Find exact or fuzzy match for car_name in main.xlsx
    4. Get the car's total quantity
    5. Scan rows BELOW the car row
    6. Collect items until running quantity sum matches car's qty
    7. Stop at next parent row or when qty matches
    8. Return matching items with quantities from item stock list
    """
    
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).upper()
    
    def _to_int(value):
        """Extract integer from cell."""
        if pd.isna(value):
            return 0
        match = re.search(r"-?\d+", str(value))
        return int(match.group()) if match else 0
    
    # 1. Load and cache main.xlsx rows
    def _load_main_rows_cached():
        main_file = get_main_file_path()
        fingerprint = _file_fingerprint(main_file)
        
        # Check if already cached
        with DATA_CACHE_LOCK:
            if MAIN_ROWS_CACHE["fingerprint"] == fingerprint:
                return MAIN_ROWS_CACHE["rows"], MAIN_ROWS_CACHE["exact_index"]
        
        # Parse Excel
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
        
        # Cache it
        with DATA_CACHE_LOCK:
            MAIN_ROWS_CACHE["fingerprint"] = fingerprint
            MAIN_ROWS_CACHE["rows"] = rows
            MAIN_ROWS_CACHE["exact_index"] = exact_index
        
        return rows, exact_index
    
    # 2. Load stock quantities from item stock list
    def _load_stock_qty_map_cached():
        """Load quantities from item stock list.auto.json or .xlsx"""
        json_file = ITEM_STOCK_CACHE_JSON
        
        # Try JSON cache first (fastest)
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
                    return qty_map
            except Exception:
                pass
        
        # Fallback to Excel file
        stock_file = get_latest_stock_file_path()
        fingerprint = _file_fingerprint(stock_file)
        if fingerprint is None:
            return {}
        
        # Check cache
        with DATA_CACHE_LOCK:
            if STOCK_QTY_CACHE["fingerprint"] == fingerprint:
                return STOCK_QTY_CACHE["qty_map"]
        
        # Parse Excel
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
                if qty > 0:
                    qty_map[_norm(item_name)] = qty
        except Exception:
            qty_map = {}
        
        # Cache it
        with DATA_CACHE_LOCK:
            STOCK_QTY_CACHE["fingerprint"] = fingerprint
            STOCK_QTY_CACHE["qty_map"] = qty_map
        
        return qty_map
    
    # ===== MAIN LOGIC =====
    
    rows, exact_index = _load_main_rows_cached()
    if not rows:
        return []
    
    # Find parent row (exact match first, then fuzzy)
    parent_idx = None
    car_upper = _norm(car_name)
    matched_parent_upper = ""
    parent_qty_total = 0
    
    # Try exact match first
    exact_match_idx = exact_index.get(car_upper)
    if exact_match_idx is not None:
        parent_idx = exact_match_idx
        _, matched_parent_upper, parent_qty_total = rows[parent_idx]
    else:
        # Fuzzy match: find car_name substring in row
        for idx, (_, name_upper, qty_total) in enumerate(rows):
            if car_upper in name_upper:
                parent_idx = idx
                matched_parent_upper = name_upper
                parent_qty_total = qty_total
                break
    
    if parent_idx is None:
        return []  # Car not found
    
    # Load quantities from item stock list
    stock_qty_map = _load_stock_qty_map_cached()
    
    # Collect child items by summing quantities
    children = []
    running_qty = 0
    
    for name, upper_name, _ in rows[parent_idx + 1:]:
        
        # Stop if we've collected enough items to match parent qty
        if parent_qty_total > 0 and running_qty >= parent_qty_total:
            break
        
        # Stop at next parent group (to isolate this car's section)
        if upper_name in PARENT_NAME_SET:
            break
        
        # Skip parent labels themselves
        if upper_name in PARENT_NAME_SET or upper_name == matched_parent_upper:
            continue
        
        # Lookup quantity from item stock list
        qty = stock_qty_map.get(upper_name, 0)
        if qty > 0:
            children.append({
                "raw": name,
                "design": name,
                "qty": qty
            })
            running_qty += qty
            
            # Stop when we've reached parent's total quantity
            if parent_qty_total > 0 and running_qty >= parent_qty_total:
                break
    
    return children
```

### 3.4 Designs Endpoint (Dropdown 2)

**API:** `/designs`

```python
@app.route("/designs")
def designs():
    """Get stock items for a selected car.
    
    Query params:
        car: car model name (from dropdown 1)
    
    Returns:
        JSON array of enriched design items with prices & images
    """
    car = request.args.get("car")
    if not car:
        return jsonify([])
    
    load_error = ensure_data_loaded()
    if load_error:
        return jsonify({"error": load_error}), 500
    
    # PRIMARY strategy: Quantity-sum scanning of main.xlsx export
    children = _find_children_by_qty(car)
    if children:
        return jsonify(_build_design_payload(children))
    
    # FALLBACK strategy: Flat lookup in CAR_DESIGN_MAP
    if car in CAR_DESIGN_MAP and CAR_DESIGN_MAP[car]:
        return jsonify(_build_design_payload(CAR_DESIGN_MAP[car]))
    
    return jsonify([])


def _build_design_payload(designs):
    """Enrich design items with prices, images, and metadata.
    
    For each design item:
    1. Look up price (custom > base > "Contact Us")
    2. Look up mapped image
    3. Add thumbnail URL and image ID
    4. Add training/fix URL
    """
    stock_item_names = []
    seen = set()
    
    for item in designs or []:
        stock_item_name = item.get("design") or item.get("raw") or "Unknown"
        lookup_key = _normalize_lookup_key(stock_item_name)
        if lookup_key and lookup_key not in seen:
            seen.add(lookup_key)
            stock_item_names.append(stock_item_name)
    
    # Get prices (customer override or base prices)
    mapping_lookup = db.get_mappings_for_stock_items(stock_item_names)
    price_lookup, default_price = _resolve_prices_for_stock_items(stock_item_names)
    
    # Build response
    payload = []
    for item in designs or []:
        stock_item_name = item.get("design") or item.get("raw") or "Unknown"
        price = price_lookup.get(_normalize_lookup_key(stock_item_name), default_price)
        mapping = mapping_lookup.get(_normalize_lookup_key(stock_item_name))
        
        enriched_item = dict(item)
        
        # Add image information if mapped
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
```

---

## 4. Example Workflow

### Scenario: User Selects "ALTO K-10 (2014)"

**Step 1: Dropdown 1 Selection**
- User types or selects "ALTO K-10 (2014)" from car models
- API `/api/search_cars?q=alto+k10` returns matching cars

**Step 2: Dropdown 2 Population**
- App calls `/designs?car=ALTO%20K-10%20(2014)`
- `_find_children_by_qty()` runs:
  - Loads main.xlsx into memory (cached)
  - Finds row: `ALTO K-10 (2014) | 30` (total qty = 30)
  - Scans rows BELOW this row
  - Collects items:
    ```
    DOOR HANDLE CHROME INNER (qty: 30 from stock list)
    DOOR LATCH ASSY (qty: 25 from stock list)
    RUBBER STRIP (qty: 18 from stock list)
    ...
    ```
  - Continues until running sum ≥ 30

**Step 3: Response Format**
```json
[
  {
    "raw": "DOOR HANDLE CHROME INNER",
    "design": "DOOR HANDLE CHROME INNER",
    "qty": 30,
    "mapped": true,
    "image_id": 42,
    "thumbnail_url": "/get_image/42",
    "confidence": 0.95,
    "price": "$25.50",
    "fix_url": "/train?stock_item=DOOR%20HANDLE%20CHROME%20INNER"
  },
  {
    "raw": "DOOR LATCH ASSY",
    "design": "DOOR LATCH ASSY",
    "qty": 25,
    "mapped": false,
    "image_id": null,
    "thumbnail_url": "/get_stock_image?stock_item=DOOR%20LATCH%20ASSY",
    "confidence": 0.0,
    "price": "Contact Us",
    "fix_url": "/train?stock_item=DOOR%20LATCH%20ASSY"
  }
]
```

**Step 4: UI Display**
- Dropdown 2 shows all items with quantities
- Items with mapped images show thumbnails
- Items without images show placeholder
- Prices fetched from database

---

## 5. Caching Strategy

### Cache Layers

| Cache | Type | TTL | Invalidation |
|-------|------|-----|---------------|
| CAR_GROUPS | In-memory list | Until app restart | Manual `load_data()` call |
| MAIN_ROWS_CACHE | Dict with fingerprint | File-based | File hash change detected |
| STOCK_QTY_CACHE | Dict with fingerprint | File-based | File hash change detected |
| item stock list.auto.json | JSON file | Until next Tally export | Every 5 min (auto-export) |

### Cache Invalidation Logic

```python
MAIN_ROWS_CACHE = {
    "fingerprint": None,  # Hash of main.xlsx file
    "rows": [],           # Parsed rows
    "exact_index": {}     # For O(1) lookups
}

STOCK_QTY_CACHE = {
    "fingerprint": None,  # Hash of stock file
    "qty_map": {}         # Normalized name -> qty
}

def _file_fingerprint(file_path):
    """Get MD5 hash of file to detect changes."""
    try:
        with open(file_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except:
        return None
```

When a dropdown is clicked:
1. Check if file fingerprint matches cache
2. If matches → use cached data
3. If different → reload Excel and update cache

---

## 6. Configuration

**File:** `config.py`

```python
class Config:
    TALLY_URL = "http://localhost:9000"  # Tally server
    TALLY_TIMEOUT = 120  # Seconds
    TALLY_RETRY_ATTEMPTS = 3
    TALLY_EXPORT_INTERVAL = 300  # 5 minutes in seconds
    AUTO_EXPORT_ITEM = "1"  # Enable auto-export ("0" to disable)
    MAX_IMAGE_RESPONSE_LIMIT = 30
```

---

## 7. Key Files Reference

| File | Purpose |
|------|---------|
| `app.py` | Main app, data loading, API routes |
| `tally/sync.py` | Tally HTTP communication & retry logic |
| `api/search.py` | Search/autocomplete APIs |
| `database.py` | SQLite operations, image/mapping storage |
| `matcher.py` | ML-based image-to-item matching |
| `image_scanner.py` | Scan filesystem for car images |
| `config.py` | Configuration settings |

---

## Summary

- **Tally Sync:** Runs every 5 minutes, exports stock items & quantities to Excel/JSON
- **Excel Files:** Car list + Main hierarchy + Stock quantities = dual-source data
- **Two Dropdowns:** Car selector → Stock items for that car (quantity-matched)
- **Filtering:** Uses quantity sums to find related items, stops at parent boundaries
- **Caching:** Aggressive caching with file fingerprints for performance
