# Tally Stock Viewer - FIXED!

## Status: OPERATIONAL

The Tally Stock Viewer Flask application is now fully functional, supports admin/viewer login roles, and keeps working when Tally is offline by falling back to the last saved stock export.

## Current Training Workflow

- SS images are scanned from `data/S.S IMAGE/` and stored in SQLite.
- The training page uses a direct SS-image dropdown instead of next/previous navigation.
- The stock-side dropdown stays in place for manual matching.
- Images are clickable for fullscreen viewing and can be copied from the UI.
- Bulk export is available for the visible image set and the training queue.
- The AI suggestion panel is intentionally removed for now to reduce noise and redundancy.
- Viewer mode is read-only; only admin can confirm mappings and refresh stock.

## Current Access Model

- Username: `admin`
- Password: `idris123`
- Viewer access is a separate read-only option with no write permissions.
- Admin-only actions remain available in the UI, while viewer mode hides them.

## Tally Fallback Behavior

- Refreshes use the live Tally export when the server is available.
- If Tally is down, the app keeps the last saved upload and shows an admin warning.
- When Tally comes back online, the normal refresh flow resumes without manual recovery.

---

## What Was Fixed

### Issue 1: Hierarchical Parser Complexity
**Problem**: Original approach attempted to parse an explicit parent-child hierarchy from Tally's flat Excel export (main.xls). Multiple parsing strategies failed because:
- Tally formatting information (bold text for parents) is lost when reading with pandas
- Content-based heuristics couldn't reliably distinguish parents from children
- The data structure had too many exceptions and edge cases

**Solution**: Switched to a **flat list approach**:
- Simply load all 3,196 valid designs from main.xls as a flat list
- Match each car in the dropdown to designs by name substring matching
- Extract base car model name (removing version markers like `** V-18 **`)
- Return all designs containing that base model name

### Issue 2: Unicode Console Errors
**Problem**: Print statements using Unicode checkmarks (✓, ✗) caused Windows console encoding errors.

**Solution**: Replaced all Unicode characters with ASCII equivalents (`[OK]`, `[ERROR]`).

### Issue 3: Car-to-Design Matching Logic
**Problem**: Initial matching logic was too simplistic and didn't account for version markers in dropdown car names.

**Solution**: Improved matching logic to:
1. Extract base car model from dropdown names (remove trailing parentheses and version markers)
2. Search for all designs containing the base model substring
3. This ensures cars like `ALCAZAR(7)(WA) ** V-18 **` match all ALCAZAR designs

---

## Current Implementation

### Parser: `parse_flat_tally(file_path)`
- Reads all 3,272 rows from main.xls  
- Filters to only rows with valid positive quantities
- Returns list of 3,196 design objects: `{"raw": str, "design": str, "qty": int}`

### Matching: Car-to-Design in `load_data()`
- Loads 598 cars from car master list (filtered between markers)
- For each car:
  - Extracts base model name (`ALCAZAR(7)(WA) ** V-18 **` → `ALCAZAR(7)(WA)`)
  - Searches all designs for substring match
  - Returns all matching designs

### API Endpoints
- **GET `/cars`** - Returns list of 598 car models
- **GET `/designs?car=X`** - Returns designs for specified car
- **GET `/last_update`** - Returns main.xls modification time  
- **POST `/reload`** - Reloads car and design data
- **POST `/export_images`** - Bulk image ZIP export for selected image IDs
- **POST `/refresh_stock`** - Exports fresh data from Tally and reloads
- **GET `/refresh_status`** - Returns last refresh status

### Frontend
- HTML dropdown populated from `/cars` endpoint
- "Load Designs" button fetches from `/designs` endpoint
- Displays all returned designs as list

---

## Test Results

✓ **API Working**:
- ALCAZAR(7)(WA) ** V-18 **: 6 designs found
- BOLERO: 57 designs found  
- ALTO (N): 55 designs found
- Total: 598 cars matched to 3,196+ designs

✓ **Web UI**:
- Dropdown loads with all 598 cars
- Can select car and click "Load Designs"
- Designs display with raw names and quantities

---

## Files Modified

- **app.py**: Replaced hierarchical parser with flat list parser, improved car matching logic
- **test_debug.py**: Updated to test flat parser and car matching
- **test_api.py**: Created to verify API endpoints work

---

## Next Steps (Optional)

1. **UI Enhancement**: Show designs grouped by design code (parent-child grouping)
2. **Export Options**: Add XML/JSON export for alternative formats
3. **Business PC Testing**: Test on Windows 10 with Tally OA installed
4. **Production Deploy**: Set up on business main PC as permanent solution

---

## Planned Feature: Google Photos-Like Picture Management System

### Overview
A visual inventory system that organizes pictures by car model and intelligently matches them against existing stock items, with progressive database learning through user confirmations.

### Key Features

#### 1. **Picture Organization**
- Upload and group images under specific car names
- Auto-organize by car model from the existing car master list
- Visual gallery view for each car

#### 2. **Intelligent Matching System**
- **Exact Matches**: Display stock designs that exactly match the uploaded picture
- **Partial Matches**: Show designs that partially match (e.g., similar parts/components)
- **Best Match Selection**: Present top matching options ranked by relevance (using image recognition or keyword matching)

#### 3. **Interactive Confirmation Workflow**
- User confirms which stock item the picture corresponds to
- System learns from confirmations to build a custom mapping database
- Track confidence levels for future matching

#### 4. **Progressive Database Building**
- Store user confirmations locally (SQL database)
- Build a learned mapping: `picture_id → stock_design_id → car_model`
- Use historical confirmations to improve future matching suggestions
- Learn common picture-to-stock associations

#### 5. **Technical Implementation**
- **Backend**: Flask API to handle image uploads, matching logic, confirmation storage
- **Storage**: Local database to track confirmed matches and build learning dataset
- **Frontend**: Gallery view with drag-and-drop upload, matching suggestions, confirmation buttons
- **Matching Logic**:
  - Extract car name from upload context
  - Query all related stock designs for that car
  - Rank by relevance (full match → partial match → no match)
  - Display ranked options to user for confirmation

#### 6. **Future Enhancements**
- Computer vision for automatic part recognition from images
- Similarity scoring between pictures and designs
- Batch import from folders
- Multiple confirmations to increase confidence threshold
- Analytics dashboard showing learning progress

---

---

## Complete Architecture & Implementation Guide

### 1. System Architecture Overview

The system is built on a **dual-file workflow** utilizing Tally's ERP data:

```
┌─────────────────────────────────────┐
│       Tally ERP (Source)            │
│   (Internal Stock Management)       │
└────────────┬────────────────────────┘
             │ Auto-Export (Every 3 mins)
             ▼
    ┌────────────────────┐
    │ XML over HTTP 9000 │
    └────────┬───────────┘
             │
    ┌────────▼───────────────────────────────┐
    │   app.py (Flask Backend)               │
    │ ┌──────────────────────────────────┐   │
    │ │ Tally Export Parser              │   │
    │ │ (fetch_item_stock_flat)          │   │
    │ ├──────────────────────────────────┤   │
    │ │ Data Loading & Matching          │   │
    │ │ (parse_flat_tally, load_data)    │   │
    │ ├──────────────────────────────────┤   │
    │ │ In-Memory Cache                  │   │
    │ │ CAR_GROUPS, CAR_DESIGN_MAP       │   │
    │ └──────────────────────────────────┘   │
    └────────┬───────────────────────────────┘
             │ HTTP REST API
    ┌────────▼──────────────────┐
    │  Frontend (index.html)     │
    │  ┌──────────────────────┐  │
    │  │ Car Dropdown         │  │
    │  │ Design List Display  │  │
    │  │ Control Buttons      │  │
    │  └──────────────────────┘  │
    └────────────────────────────┘
             │
    ┌────────▼──────────────────────────────┐
    │   Data Files (data/ folder)           │
    │ ┌──────────────────────────────────┐  │
    │ │ car master list.xls              │  │
    │ │ (598 car models dropdown)        │  │
    │ │                                  │  │
    │ │ main.xlsx                        │  │
    │ │ (3,196 designs with qty)         │  │
    │ │                                  │  │
    │ │ item stock list.auto.xlsx        │  │
    │ │ (Live quantities from Tally)     │  │
    │ └──────────────────────────────────┘  │
    └───────────────────────────────────────┘
```

---

### 2. File Structure & Purpose

#### **Root Files**

| File | Purpose | Notes |
|------|---------|-------|
| `app.py` | Main Flask backend application | ~700 lines: handles all business logic |
| `test_tally.py` | Initial Tally connection test | Standalone utility to verify XML export works |
| `PROJECT_STATUS.md` | Project progress tracking | Historical notes (now archived) |
| `SOLUTION.md` | This documentation | Complete technical specification |
| `STOCK_EXPORT.TDL` | Tally Definition Language file | Tally export template for custom XML output |

#### **data/ Folder** (Excel Data Files)

| File | Used By | Updated | Purpose |
|------|---------|---------|---------|
| `car master list.xls` | Frontend dropdown | Never | 598 car models filtered between markers |
| `main.xlsx` | Hierarchy parsing | Manual | 3,196 designs with parent-child structure & original qty |
| `item stock list.auto.xlsx` | Live qty lookup | Every 3 mins | Current stock quantities from Tally |

#### **templates/ Folder**

| File | Purpose |
|------|---------|
| `index.html` | Frontend UI: dropdown, design list, control buttons |

---

### 3. Current Features (Production Ready)

#### **Core Functionality**

✅ **Dynamic Car Selection**
- 598 pre-curated car models in dropdown (filtered between "ACCESSORIES" and "ZS-EV" markers)
- Loaded from `car master list.xls` Column A

✅ **Design Lookup by Car**
- Select car → click "Load Designs" → displays all matching designs
- Designs grouped under selected car model with current stock quantities

✅ **Automatic Stock Refresh**
- Every 3 minutes: connects to Tally ERP via HTTP (port 9000)
- Exports latest stock quantities via XML request
- Parses XML response and saves to `item stock list.auto.xlsx`

✅ **Manual Refresh Controls**
- "Load Designs": Fetch designs for selected car
- "Reload Data": Re-parse Excel files and rebuild in-memory cache
- "Refresh Stock (manual)": Force immediate export from Tally

✅ **Tally Integration**
- Two-way XML over HTTP protocol (Tallyetech)
- Exports "Stock Summary" with both simple and detailed rows
- Filters to only stock items with positive quantities (qty > 0)

✅ **Robust UI Feedback**
- Last update timestamp display
- Auto-export status indicator
- Error messages for missing data
- Real-time refresh status

---

### 4. Data Flow & Methodology

#### **A. Initialization (app.py startup)**

1. **Import Dependencies**: Flask, Pandas, Requests, XML parsing, Threading
2. **Configure File Paths**: Set candidates for `main.xlsx`/`main.xls`, car list, stock files
3. **Set Tally Connection**: Default `http://localhost:9000` (configurable)
4. **Auto-Export Setup**: Schedule `schedule_item_export()` every 180 seconds if enabled
5. **Load Data**: Call `load_data()` to populate in-memory caches

#### **B. Tally Data Export Process**

**Trigger**: Either auto-scheduled (3 min interval) or manual button click

**Process** (`fetch_item_stock_flat()`):

1. **Normalize Function**: Helper to uppercase and collapse whitespace for comparison
2. **Fetch Stock Item Master Names** (XML request → Tally):
   - Request: `<TALLYREQUEST>Export Data</TALLYREQUEST>` for "List of Stock Items"
   - Response: XML with all ~2000+ stock item master names
   - Parse: Extract all `STOCKITEM` nodes and collect normalized names into set
   
3. **Fetch Stock Summary - Both Views**:
   - **Simple View**: Broad categories (roll-ups), for detecting parent groups
     - Request with `<ISDETAILED>No</ISDETAILED>`
   - **Detailed View**: All individual stock items with quantities
     - Request with `<ISDETAILED>Yes</ISDETAILED>` + `<EXPLODEFLAG>Yes</EXPLODEFLAG>`
   - Parse: Extract pairs of `DSPACCNAME` (item name) and `DSPSTKINFO` (quantity)

4. **Multi-Level Filtering**:
   - Primary: Keep only rows whose names exist in Stock Item master
   - Secondary: Remove obvious parent group names (present in simple view)
   - Optional: If `main.xlsx` loaded, align remaining rows to hierarchy names only
   - Fallback: If above yields nothing, keep all detailed rows

5. **Deduplication**: Remove duplicate item names, keep last occurrence

6. **File Write** (with retry logic):
   - Convert DataFrame to Excel (.xlsx format)
   - Atomic write: Temp file → Renamed to final file (6 retry attempts)
   - If locked: Fallback to alternate filename (`.xlsx` or timestamped `.auto.YYYYMMDD_HHMMSS.xlsx`)

7. **Update Status**: Store success/warning message with ISO timestamp

#### **C. Data Loading & In-Memory Cache Mapping**

**Called**: On app startup + after each Tally export + when user clicks "Reload Data"

**Process** (`load_data()`):

1. **Load Cars from Dropdown**:
   - Read `car master list.xls` Column A
   - Filter to range between markers: "ACCESSORIES (NECK REST)" and "ZS - EV FOOT MAT"
   - Store in global `CAR_GROUPS` list (598 models)

2. **Load Designs from Main File**:
   - Find `main.xlsx` using candidate list
   - Call `parse_flat_tally()` to extract all rows with qty > 0
   - For each car, extract "base car name" (strip version markers like `** V-18 **`)
   - Substring-match base car name against all designs
   - Build `CAR_DESIGN_MAP`: {car_name: [matching_designs]}

3. **Preserve Tally Hierarchy** (for advanced child-finding):
   - Store in `TALLY_HIERARCHY` dict for fallback parent-child queries

4. **Log Summary**: Print count of loaded cars and matched designs

#### **D. API Request Handling**

**GET `/`** → Serve **index.html**
- Context: `auto_export_enabled` flag, refresh interval in seconds
- Frontend uses these to show/hide auto-export indicator

**GET `/cars`** → Return **List of 598 Car Models** (JSON array)
- Calls `ensure_data_loaded()` to lazy-load if cache empty
- Returns `CAR_GROUPS` list
- HTTP 500 if loading fails

**GET `/designs?car=X`** → Return **Designs for Car X** (JSON array of objects)
- Advanced strategy: `_find_children_by_qty(car_name)`
  - Finds car in main file, gets parent quantity
  - Scans rows below until child quantities sum to parent
  - Looks up live quantities from `item stock list` file
  - Stops when reaching equal quantity or next parent group
- Fallback strategy: Simple substring lookup in `CAR_DESIGN_MAP`
- Design object: `{raw: "<original_name>", design: "<name>", qty: <int>}`

**POST `/reload`** → Rebuild Cache
- Calls `load_data(refresh_first=False)`
- Re-reads Excel files from disk into memory
- Useful after manual updates to car master or main file

**POST `/refresh_stock`** → Force Tally Export
- Calls `fetch_item_stock_flat()` directly
- Then reloads in-memory cache
- Useful for immediate stock sync without waiting 3 minutes

**GET `/last_update`** → Get Last Refresh Time
- Returns modification time of latest stock file
- Shows user when data was last updated

**GET `/refresh_status`** → Get Export Status
- Returns `last_refresh_status` dict: `{success: bool, message: str, timestamp: iso}`
- Tells frontend if last auto-export succeeded and why (if not)

#### **E. Frontend Interaction (index.html)**

1. **On Page Load**:
   - JavaScript calls `/cars` endpoint
   - Populates `<select id="carSelect">` dropdown with 598 options

2. **User Selects Car & Clicks "Load Designs"**:
   - Calls `/designs?car=<selected_car>`
   - Receives JSON array of designs with quantities
   - Renders each as a list item with border styling

3. **"Reload Data" Button**:
   - Calls `/reload` endpoint
   - Shows success/error message
   - Optionally re-populates dropdown

4. **"Refresh Stock (manual)" Button**:
   - Calls `/refresh_stock` endpoint
   - Shows status message
   - Updates last-update timestamp display

5. **Auto-Export Indicator**:
   - If `auto_export_enabled=true`, shows "🔁 Auto-export enabled (every 180 seconds)"
   - Otherwise shows "🔁 Auto-export disabled"

---

### 5. Key Design Patterns & Techniques

#### **Flat List Approach (No Hierarchy Parsing)**
- Originally attempted explicit parent-child parsing ❌ Failed due to Tally formatting loss
- Switched to: Load all rows as flat list, match by substring ✓ Reliable & simple
- Parent discovery now only by quantity-sum scanning (optional advanced mode)

#### **Dual-File Workflow**
- **Hierarchy Info**: `main.xlsx` (manual Tally export, preserved for structure)
- **Live Quantities**: `item stock list.auto.xlsx` (auto-updated every 3 min)
- **Separation of Concerns**: Get structure once, refresh quantities constantly

#### **In-Memory Caching**
- Global dictionaries: `CAR_GROUPS`, `CAR_DESIGN_MAP`, `TALLY_HIERARCHY`
- No database needed
- Fast API responses
- Cache rebuilt on each Tally export

#### **Retry Logic & Atomic File Operations**
- XML connections: 2 retry attempts on error
- File writes: 6 retry attempts with 500ms delays (handles locked files)
- Temp file → Atomic rename (prevents partial writes)
- Fallback naming if rename fails

#### **Normalized String Comparison**
- `_norm()`: Uppercase + collapse whitespace
- Used for matching item names across different Tally exports
- Handles formatting inconsistencies

#### **Environment-Based Configuration**
- `AUTO_EXPORT_ITEM` env var: Set to "0" or "false" to disable auto-export
- Allows testing without constant Tally exports

---

### 6. How to Recreate This System

#### **Prerequisites**
- Windows machine with Python 3.9+
- Tally ERP installed with Stock module
- Tally API enabled (Tally > Gateway of Tally > Preferences > Enable Remote Connection)
- Network connectivity to Tally machine (default: `localhost:9000`)

#### **Step 1: Project Setup**
1. Create folder: `c:\Users\idirs\Desktop\tally_test`
2. Create virtual environment: `python -m venv .venv`
3. Activate: `.\.venv\Scripts\activate.ps1`
4. Install packages: `pip install flask pandas requests openpyxl`

#### **Step 2: Data Files (Create the initial Excel files)**

**File 1: `data/car master list.xls`**
- Column A: 598 car model names
- Range: From "ACCESSORIES (NECK REST)" to "ZS - EV FOOT MAT"
- Source: Can be extracted from Tally or provided as static list
- Used: Populates frontend dropdown

**File 2: `data/main.xlsx`**
- Column A: All ~3,200 stock design names (parent-child hierarchy)
- Column B: Original quantities at time of export
- Source: Tally Stock Summary export (manual export from Tally)
- Used: Provides design list and parent-child structure lookup

**File 3: `data/item stock list.auto.xlsx`**
- Column A: Stock item names
- Column B: Current quantities
- Auto-created: First time app runs (via Tally export)
- Updated: Every 3 minutes automatically
- Used: Live stock quantities lookup

#### **Step 3: Backend Code (app.py - ~700 lines)**

**Sections to implement**:

1. **Imports & Configuration**
   - Flask, Pandas, Requests, XML ElementTree, threading, datetime, etc.
   - File path constants for car list, main file, stock files
   - Tally URL, export interval (180 seconds)

2. **Tally Export Functions**
   - `fetch_item_stock_from_tally()`: Wrapper
   - `fetch_item_stock_flat()`: Main export logic
     - `_norm()`: String normalization helper
     - `_fetch_stock_item_master_names()`: Get all master stock item names via XML
     - `_fetch_rows()`: Fetch stock summary (simple and detailed)
     - Multi-level filtering, deduplication, atomic file write
   - `schedule_item_export()`: Background thread timer

3. **Parse & Load Functions**
   - `parse_flat_tally(file_path)`: Read main.xlsx, return flat list of designs
   - `load_data(refresh_first)`: Populate globa caches from Excel files
   - `ensure_data_loaded()`: Lazy load if needed
   - Supporting function for finding children by quantity sum

4. **HTTP Routes (5 main endpoints)**
   - `GET /`: Serve index.html with context
   - `GET /cars`: Return list of 598 car models
   - `GET /designs?car=X`: Return designs for selected car
   - `POST /reload`: Rebuild cache from disk
   - `POST /refresh_stock`: Force Tally export
   - `GET /last_update`: Return file modification time
   - `GET /refresh_status`: Return last refresh status

5. **Main Block**
   - Initialize Flask app
   - Start auto-export timer if enabled
   - Call `load_data()` once at startup
   - Run Flask dev server on `http://localhost:5000`

#### **Step 4: Frontend Code (templates/index.html - ~200 lines)**

**Elements**:
1. Car dropdown (`<select id="carSelect">`) - populated by `/cars` API
2. Control buttons: "Load Designs", "Reload Data", "Refresh Stock (manual)"
3. Info display: Last update time, auto-export status indicator
4. Designs list (`<ul id="designsList">`) - populated dynamically
5. Error/success message boxes

**JavaScript Logic**:
1. On page load: Fetch `/cars`, populate dropdown
2. On "Load Designs" click: Fetch `/designs?car=X`, render designs list
3. On "Reload Data" click: POST `/reload`, show result
4. On "Refresh Stock" click: POST `/refresh_stock`, update status display
5. Update last-update timestamp periodically (or on demand)

**Styling**:
- Clean, responsive layout
- White cards with shadows
- Blue buttons with hover effects
- Color-coded messages (red=error, green=success)
- Mobile-friendly (max-width 800px)

#### **Step 5: Optional: Tally Export Template**
- Tally Definition Language (.TDL) file for custom export format
- Pre-defines the XML structure for Stock Summary export
- File: `STOCK_EXPORT.TDL` (not required, Tally works without it)
- Useful: For enforcing consistent export format across Tally versions

---

## Running the App

```bash
cd c:\Users\idirs\Desktop\tally_test
.\.venv\Scripts\python.exe app.py
```

Then open: **http://localhost:5000**

Select a car from dropdown and click "Load Designs" to see all in-stock designs for that car model.

---

### Expected Console Output (on startup)

```
[OK] Loaded 598 car models from dropdown
[OK] Reading 3196 rows from data/main.xlsx
[OK] Loaded 3196 valid designs
Matched 598 cars to 3196+ designs
requesting flat item stock list from Tally...
exported item stock list to data/item stock list.auto.xlsx (2847 rows)
 * Running on http://127.0.0.1:5000
 * WARNING: This is a development server. Do not use it in production.
```

### Web UI Usage

1. **Initial Page Load**: 
   - Dropdown auto-populates with 598 car models
   - "Last Update" shows when stock quantities were last synced
   - Auto-export indicator shows if background refresh is active

2. **Select & Load**:
   - Choose car → click "Load Designs"
   - Displays all matching designs with stock quantities
   - Designs shown as cards with borders

3. **Refresh Options**:
   - **Load Designs**: Refresh current selection
   - **Reload Data**: Re-read Excel files from disk (useful after manual updates)
   - **Refresh Stock**: Force immediate Tally export (don't wait 3 minutes)

---

### Configuration & Advanced Options

#### **Auto-Export Control**
Disable auto-export by setting environment variable before running app:
```powershell
$env:AUTO_EXPORT_ITEM = "0"
.\.venv\Scripts\python.exe app.py
```

Re-enable (default): `$env:AUTO_EXPORT_ITEM = "1"`

#### **Change Tally Connection URL**
Edit line in `app.py`:
```python
TALLY_URL = "http://192.168.1.100:9000"  # Change to your Tally server IP
```

#### **Change Auto-Export Interval**
Edit line in `app.py`:
```python
ITEM_EXPORT_INTERVAL = 60 * 5  # Change from 180 seconds (3 min) to 300 seconds (5 min)
```

#### **Change Car Filter Range**
Edit lines in `app.py` `load_data()` function:
```python
start_marker = "ACCESSORIES (NECK REST)"  # Change start
end_marker = "ZS - EV FOOT MAT"           # Change end
```

---

### Troubleshooting

#### **Problem: "No car models loaded from car master list"**
- **Cause**: `data/car master list.xls` missing or wrong format
- **Solution**: Ensure file exists in `data/` folder with car names in Column A

#### **Problem: Car dropdown shows but "Load Designs" returns empty**
- **Cause**: `main.xlsx` file not found or no matching designs
- **Solution**:
  - Check `main.xlsx` exists in `data/` or current directory
  - Verify car name in file matches selected dropdown option
  - Try "Reload Data" button to refresh cache

#### **Problem: "Tally returned error: Error in XML"**
- **Cause**: Tally server not running or remote connection disabled
- **Solution**:
  1. Verify Tally is running: `Tally > Gateway of Tally > Status`
  2. Enable remote XML: `Tally > Gateway > Preferences > Enable Remote Connection`
  3. Verify port 9000 is accessible: `netstat -an | findstr 9000`

#### **Problem: Auto-export never happens**
- **Cause**: `AUTO_EXPORT_ITEM` set to "0" or Tally unreachable
- **Solution**:
  - Enable with `$env:AUTO_EXPORT_ITEM = "1"`
  - Check Tally connection (see above)
  - Try manual refresh with "Refresh Stock (manual)" button

#### **Problem: "File locked" error when exporting**
- **Cause**: Excel or another app has `item stock list.auto.xlsx` open
- **Solution**: Close the file in Excel. App will retry and eventually write to fallback filename

#### **Problem: Designs show very old quantities**
- **Cause**: Auto-export disabled or Tally not syncing
- **Solution**:
  - Click "Refresh Stock (manual)" to force immediate export
  - Check refresh status in web UI ("Last Update" line)

---

### Testing Without Tally (Local Development)

If Tally isn't available, manually create test data:

**Create `data/main.xlsx`:**
```
Car Model,Quantity
ALCAZAR,50
ALCAZAR SEAT BELT,2
ALCAZAR HEADREST,4
BOLERO,100
BOLERO MIRROR,1
BOLERO DOOR HANDLE,3
```

**Create `data/item stock list.auto.xlsx`:**
```
Item Name,Quantity
ALCAZAR SEAT BELT,2
ALCAZAR HEADREST,4
BOLERO MIRROR,1
BOLERO DOOR HANDLE,3
```

**Create `data/car master list.xls`:**
```
ACCESSORIES (NECK REST)
...
ALCAZAR
BOLERO
...
ZS - EV FOOT MAT
```

Run app with auto-export disabled:
```powershell
$env:AUTO_EXPORT_ITEM = "0"
.\.venv\Scripts\python.exe app.py
```

---

### Performance Notes

- **Startup**: ~2-5 seconds (reads Excel files into memory)
- **API Response Time**: <100ms (all data in memory)
- **Tally Export**: ~30-60 seconds (depends on Tally server responsiveness)
- **Memory Usage**: ~50-150 MB (depends on size of design list)

---

### Future Enhancement: Picture Management System

Once the core stock viewer is stable, the planned picture management feature will add:

- **Image Gallery**: Upload and organize pictures by car model
- **Auto-Matching**: Suggest stock designs that match uploaded pictures
- **User Confirmations**: Build learning database from user validations
- **Confidence Scoring**: Track which matches are most reliable
- **Batch Processing**: Import folders of images at once
- **Analytics**: Dashboard showing matching accuracy trends

See "Planned Feature: Google Photos-Like Picture Management System" section above for full details.
