# Tally Stock Viewer - Software Deep Dive

This document describes the current codebase as it exists in this repository.
It is intended for technical maintenance, not office staff operations.

## 1. System purpose

The application is a Windows-hosted Flask system for:
- reading stock and car-group data from Tally
- matching stock items to car models
- mapping product images to stock items
- showing images to admin and customer users
- running unattended on an office PC through a tray launcher

## 2. Runtime architecture

The runtime has five main layers:

### Web layer
- `app.py` defines the Flask app, routes, background tasks, and in-memory caches
- `templates/` contains the admin and customer-facing HTML screens
- `api/search.py` provides Select2-style search endpoints

### Data layer
- `database.py` manages SQLite schema and queries
- `data/mappings.db` stores images, mappings, users, and account logs (plus legacy pricing tables, see section 15)

### Shared normalization layer
- `utils/normalize.py` — whitespace/case normalization (`normalize_text`), the shared lookup key used on both the app and database sides (`normalize_lookup_key`), display-only shelf-code stripping (`strip_shelf_code_for_display`), and car base-name extraction (`extract_car_base_name`)
- `utils/product_normalize.py` — canonical product type/color extraction (`extract_type_and_color`) used by the Bulk Match category buckets

### Tally integration layer
- `tally/sync.py` performs HTTP POST retries to Tally
- `app.py` builds XML requests, parses Tally XML, and writes local stock caches

### Windows hosting layer
- `serve.py` runs the Flask app under `waitress`
- `launcher.pyw` runs in the tray, starts the server, monitors it, and optionally starts `cloudflared.exe`
- `relaunch_helper.py` is a detached helper spawned by the System panel's restart routes so a restart works even if the launcher watchdog is not running
- `first_time_setup.bat`, `update_app.bat`, and `stop_server.py` support deployment and operations
- `scripts/measure_tally.ps1` is a read-only diagnostic that times the Tally stock-export requests

## 3. Startup flow

Normal production startup works like this:

1. Windows launches `launcher.pyw`
2. `launcher.pyw` starts `serve.py` with `pythonw.exe`
3. `serve.py` imports `app` from `app.py`
4. `app.py` loads configuration, configures logging, initializes SQLite, and registers routes
5. `waitress` binds the app to `127.0.0.1:5000`
6. `launcher.pyw` writes `app.pid`, opens the browser, and starts a watchdog thread
7. if `cloudflared.exe` exists in the project root, `launcher.pyw` also tries to start a tunnel process and writes `tunnel.pid`

Important runtime characteristics:
- hosting is local-only by default
- the launcher avoids console windows by using `pythonw.exe`
- the watchdog restarts the app if the process dies or port `5000` stops responding
- the watchdog runs as an independent thread started BEFORE `icon.run()`, deliberately not gated on tray-icon state — it used to loop on `while icon.visible:`, and because a custom pystray setup callback must set `icon.visible = True` itself (ours didn't), the watchdog silently exited on every launch and a killed server was never relaunched. Tray icon failures can no longer disable the safety net
- System panel restarts do not depend on the watchdog at all: the restart routes spawn the detached `relaunch_helper.py`, which waits for port `5000` to be released, launches a fresh `serve.py`, writes `app.pid`, and confirms the port came back — so `Pull Latest Code & Restart` works even if `launcher.pyw` is broken or absent

## 4. Configuration model

`config.py` reads almost everything from environment variables or `.env`.

Important configuration groups:

### Flask and sessions
- `FLASK_SECRET_KEY`
- `FLASK_DEBUG`
- `SESSION_TIMEOUT_HOURS`
- `SESSION_COOKIE_SECURE`

### Tally
- `TALLY_URL`
- `TALLY_TIMEOUT`
- `TALLY_RETRY_ATTEMPTS`
- `TALLY_EXPORT_INTERVAL`

### Files and scanning
- `DB_PATH`
- `LOG_FILE`
- `IMAGE_SCAN_ROOT`
- `MAX_IMAGE_SIZE`
- `MAX_IMAGE_RESPONSE_LIMIT`
- `INITIAL_IMAGE_SCAN`

### Authentication
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`

### Remote System panel
- `SYSTEM_ACCESS_TOKEN` — required to enable `/admin/system`; used once per browser to pair the device (see section 21). If unset, the panel routes return `403`.

### Accounts panel password gate
- `ACCOUNTS_ACCESS_PASSWORD` — required to unlock `/admin/accounts` and its API routes for the current session (see section 16). If unset, those routes return `403` regardless of admin session or `accounts_unlocked` state.

## 5. Data files on disk

The app uses a mixed model: some files are source-of-truth inputs and some are generated caches.

### Source inputs
- `data/car master list.xls`
  - used for the car dropdown model list
- `data/main.xlsx` or `data/main.xls`
  - used as the hierarchy/design source loaded by `load_data()`
- `data/S.S IMAGE/`
  - source image tree used by the image scanner
- `data/mappings.db`
  - SQLite storage for everything not kept in Excel

### Generated runtime caches
- `data/item stock list.auto.xlsx`
- `data/item stock list.auto.json`
- `data/car_master.json` — cached car master (Stock Groups) fetched from Tally
- `data/main_hierarchy.json` — cached parent/children hierarchy fetched from Tally; this is the file the dropdown filter (section 11), the Bulk Match catalog/category endpoints (section 13), and the `PRODUCT_CATEGORY_CACHE` fingerprint all read
- fallback alternates such as `data/item stock list.xlsx`

The JSON caches are used for fast lookup (quantities, hierarchy, categories).
The Excel cache is used as a saved local stock export and fallback artifact.

## 6. SQLite schema and responsibilities

`database.py` creates and maintains these tables:

- `images`
  - one row per scanned image file
- `mappings`
  - links an image to a stock item and stores confidence
  - one image may map to MANY stock items; each stock item maps to at most ONE image (enforced in `_confirm_mapping_core`, not by a constraint)
- `folder_car_mapping`
  - remembers folder-to-car hints learned from confirmed mappings
- `users`
  - admin and customer accounts
- `account_logs`
  - audit-style account actions
- `base_prices` / `customer_prices`
  - LEGACY: created by the schema but unused since the pricing feature was removed (see section 15); `delete_user` still cleans `customer_prices` rows for the deleted user

Notable behavior:
- scanned file paths are validated and normalized
- legacy absolute image paths are migrated to portable relative paths on every startup; if a relative-path row for the same file already exists, the legacy row is merged into it instead of renamed (keeping whichever confirmed mapping has the more recent `created_at`) and the legacy row is deleted
- default seed users are inserted only when the `users` table is empty
- `remove_mappings_for_stock_item(stock_item_name, exclude_image_id=None)` deletes any mapping row(s) for a given stock item, optionally excluding one image; `confirm_mapping` in `app.py` calls this before every save so a stock item only ever has one image mapped to it (see section 13)
- `mappings.image_id` used to carry a column-level UNIQUE constraint, which silently prevented one image from ever linking to more than one stock item (confirming the same image against a second item overwrote the first mapping via `ON CONFLICT(image_id)`). `_migrate_remove_image_id_unique()` fixes this on startup: it checks `PRAGMA index_list('mappings')` for the old autoindex and, only if present, backs up the live DB file (`backup_TIMESTAMP` convention) and rebuilds the table inside a transaction without the constraint. The upserts were retargeted to a compound `UNIQUE(image_id, stock_item_name)` index, so re-confirming the exact same image+item pair stays idempotent while one image can map to many items — which is what makes Bulk Match (section 13) possible
- stock-item name lookups (`get_mappings_for_stock_items`) match on the shared `normalize_lookup_key()` from `utils/normalize.py` (collapse whitespace + strip + lowercase) on the Python side rather than SQL `LOWER()`, because Tally names can contain irregular internal whitespace and SQL can't collapse it — the app-side lookup builder uses the same function, so both sides always produce the same key

Seeded defaults from the current code:
- admin / `idris123`
- star / `111`
- jeewajee / `222`

## 7. Authentication and authorization

Authentication is form-based.

### Login flow
- `GET /login` renders the login page
- `POST /login` checks username and access code through `db.authenticate_user()`
- successful login stores `user_id`, `username`, and `role` in the session

### Route protection
- `@app.before_request` blocks all non-public routes for logged-out users
- `@admin_required` protects admin-only endpoints such as accounts, mapping changes, and stock refresh
- `@accounts_access_required` layers a second, session-based password check on top of `@admin_required` for every Manage Accounts route (see section 16) — independent of the System panel's device pairing, and never a substitute for `@admin_required` itself

### Roles
- `admin`
  - full access
- `customer`
  - read-only browsing

## 8. Main in-memory model

`app.py` keeps several module-level caches:

- `CAR_GROUPS`
  - dropdown car models
- `CAR_DESIGN_MAP`
  - car model to matching design rows
- `TALLY_HIERARCHY`
  - reserved hierarchy structure
- `PARENT_NAME_SET`
  - normalized parent names
- `STOCK_ITEMS_CACHE`
- `MAIN_ROWS_CACHE`
- `STOCK_QTY_CACHE`
- `PRODUCT_CATEGORY_CACHE`
  - Bulk Match category buckets, invalidated when `main_hierarchy.json`'s file fingerprint changes
- `last_refresh_status`

`load_data()` is the core cache-building function.
It clears the current runtime state and rebuilds the design map from local files.

## 9. Tally export and refresh flow

Manual stock refresh uses this path:

1. client calls `POST /update_stock` or `POST /refresh_stock`
2. route calls `_refresh_stock_data()`
3. `_refresh_stock_data()` calls `fetch_item_stock_flat()`
4. `fetch_item_stock_flat()` sends XML export requests to Tally
5. the returned rows are filtered and deduplicated
6. JSON and Excel cache files are updated
7. `load_data()` rebuilds the in-memory design map

If a full refresh is running, `_refresh_stock_data()` returns a `busy` response ("Full refresh is in progress. Please wait.") instead of racing it.

### Full refresh

`run_full_refresh_job()` is the shared implementation behind every "full refresh" (car master + main hierarchy + item stock, in that order, followed by `load_data()`). It updates the module-level `full_refresh_status` dict as it moves through stages (`car_master` -> `main_hierarchy` -> `item_stock` -> `reloading` -> `done`, or `error`), guarded end-to-end by `FULL_REFRESH_LOCK`.

Two callers share this function without duplicating logic:
- `POST /full_refresh` — the manual admin button; acquires the lock (non-blocking, `409` if already running), runs the job in a background thread, returns `202` immediately
- the automatic startup job described in section 10 — runs once, ~45 seconds after the app starts

`GET /full_refresh_status` polls the same `full_refresh_status` dict regardless of which caller started the job, so the frontend progress bar behaves identically whether a human clicked the button or the startup job triggered it.

### What the refresh actually asks from Tally

The item stock export is a SINGLE TDL collection request (`Item Names With Closing`, a collection walk over StockItem masters fetching `NAME` + `CLOSINGBALANCE`).

It used to be three requests per cycle (item master collection, non-detailed Stock Summary used only as a group-name filter, and a detailed+exploded Stock Summary as the data source). The exploded report forced Tally to render its entire stock tree every cycle, which visibly stalled Tally Prime every 3 minutes. Two production measurements via the System panel's Tally Performance Test put the old cycle at ~20.7s and ~27s of total engine work (Tally load varies between runs), against ~5.2s and ~1.5s for the single-collection replacement in the same runs. The single collection returns only items (never group rows), so the filtering requests became unnecessary. Output format is unchanged.

Each cycle logs one permanent line ("Item stock export completed in X.Xs across 1 Tally request (N items)") so future slowdowns are diagnosable from the System panel's Recent Logs.

### Filtering logic

- keep only rows with `qty > 0`
- optionally align rows with names present in the local hierarchy file (skipped if that would empty the result)
- dedupe by item name

### Multiple Tally instances

`fetch_item_stock_flat()` (and the car master / hierarchy fetches) call `_check_multiple_tally_instances()` (psutil process scan for `tally.exe`) before sending anything. If more than one Tally is running, the export raises with a plain message ("Multiple Tally windows are open. Close the extra Tally and keep only one.") — this reaches the main page through the normal `/refresh_status` polling, so the 3-minute export cycle itself is the sensor; there is no separate polling endpoint.

### Failure behavior

If Tally is unreachable or times out:
- the route still returns success-style JSON
- `tally_online` is set to `False`
- the UI is told it is using the last saved upload
- the app keeps serving prior cached data instead of crashing

## 10. Background jobs

`start_background_startup_tasks()` launches a daemon thread that:
- preloads local design data when possible
- runs an image scan on every startup (`_scan_images_on_startup()`), as long as `INITIAL_IMAGE_SCAN` is enabled and `IMAGE_SCAN_ROOT` exists
- schedules the repeating item stock export if auto-export is enabled (`AUTO_EXPORT_ITEM`, on by default), starting ~10 seconds after startup

The startup scan used to be skipped whenever the `images` table already had rows, which meant it only ever ran once, the very first time the database was populated — restarting the app afterward never picked up new files added to `S.S IMAGE`. It now always re-scans on startup; the scan is an upsert (`add_images_batch`), so re-scanning unchanged files is a cheap no-op.

`start_background_startup_tasks()` also starts a second, independent daemon thread that runs one automatic full refresh (car master + main hierarchy + item stock via `run_full_refresh_job()`, see section 9) shortly after startup:
- it sleeps 45 seconds first, to give Tally and the app time to be ready
- it then tries to acquire `FULL_REFRESH_LOCK` (non-blocking); if the lock is already held (e.g. someone clicked the manual button first) it logs and skips instead of waiting or queuing
- if Tally is not reachable yet at the 45-second mark, `run_full_refresh_job()`'s own error handling catches it, logs a warning, and updates `full_refresh_status` to `error` — it does not crash the app or block startup
- this runs exactly once per app start; it is not a repeating timer, and the manual `Full Refresh` button remains the way to trigger it again later

### Timers

The item stock export is the ONLY repeating timer: first run ~10 seconds after startup, then every `TALLY_EXPORT_INTERVAL` seconds (default `180`) via a self-rescheduling `threading.Timer`. A scheduled run skips itself (and reschedules) if a full refresh or a previous export is still in progress (`FULL_REFRESH_LOCK` / `EXPORT_LOCK`).

There is no periodic car master refresh — the car master and main hierarchy are only refreshed by a full refresh (the one-time startup job 45 seconds in, or the manual `Full Refresh` button).

### Car master refresh

`fetch_car_master_from_tally()` requests the Stock Groups collection from Tally using the `List of Stock Groups` export ID. It extracts all `STOCKGROUP` name attributes from the XML response, filters out empty names, and returns a sorted list. `save_car_master_to_file()` writes that list to `data/car master list.xls` via a temp file and then reloads the in-memory caches. If Tally is unreachable the existing file is left unchanged. As noted above, this runs only as part of a full refresh, not on its own schedule.

## 11. Car and design matching model

The app does not load designs by a strict normalized relational model.
Instead, it builds a token-based mapping between car names and design rows.

### Car list source

`data/car master list.xls` is read first.
It becomes the dropdown source shown in the UI.

Two refinements apply on top of the raw list:
- `load_data()` filters `CAR_GROUPS` down to `_car_names_with_real_children()` — parent names that have a non-empty children list in `main_hierarchy.json` (regardless of current stock). This keeps cars deleted from Tally (still in the raw Stock Group fetch but absent from the hierarchy) out of the dropdown. If `main_hierarchy.json` is missing or unreadable, the filter is skipped so a machine that has never run a Full Refresh keeps its full dropdown. `PARENT_NAME_SET` is deliberately built from the FULL unfiltered list because it doubles as a row-boundary marker when scanning `main.xlsx`.
- car names are shown to humans with any trailing Tally shelf-location code stripped (`* M-20`, `****H-4****`, etc.) via `strip_shelf_code_for_display()` in `utils/normalize.py`. This is DISPLAY ONLY — the raw name (shelf code intact) is what the frontend sends back as `?car=` and what all matching runs against. Each template that needs it carries a hand-ported JS copy of the same function, since there is no shared frontend module.

### Design list source

`parse_flat_tally()` reads the current main hierarchy file as a flat list of `{design, raw, qty}` records.

### Matching strategy

For each car:
- the code extracts a base car name with `extract_car_base_name()`
- it tokenizes that base name
- it matches tokens against tokenized design rows
- matching rows are stored in `CAR_DESIGN_MAP[car]`

This is intentionally heuristic, not schema-driven.

Important limit on where `CAR_DESIGN_MAP` may be used: it is a whole-catalog token index, and common tokens like "MAT" make it wildly over-inclusive. It used to serve as a fallback in `_find_children_by_qty()` and `designs()` whenever a car had no hierarchy match at all, which meant a car deleted from Tally could return hundreds of designs really belonging to other cars. Those fallbacks were removed — a car absent from the hierarchy now returns an honest empty result. `CAR_DESIGN_MAP` remains in use only for `_all_stock_items()` (the global stock-item search), where whole-catalog coverage is the point.

## 12. Image scanning and storage

`image_scanner.py` walks the image root and stores only safe relative paths.

Important details:
- allowed file extensions come from `Config.ALLOWED_IMAGE_EXTENSIONS`
- oversized files are skipped
- unreadable files are skipped
- relative paths outside the image root are rejected
- inserts are batched through `database.add_images_batch()`, which upserts on the `filepath` UNIQUE constraint

This is one of the portability improvements in the current codebase.
The database no longer has to rely on machine-specific absolute image paths.

Image list queries used by the training UI (`get_images_by_folder`, `get_unmapped_images`, `get_unmapped_images_by_folder`) order results by `LOWER(filename) ASC, id ASC`, not insertion order. This matters because newly-scanned files get a much higher autoincrement `id` than the rest of their folder; ordering by `id` alone would always push new images to the end of the list regardless of filename. `get_next_unmapped_image()` is the one exception — it still orders by `id` because it uses `id` as a pagination cursor (`WHERE i.id > ?`), so it is not filename-sorted.

### Missing-image detection and removal (two-way sync)

The scan used to be add-only: files could be added to the `images` table but a row was never removed after its file was deleted from `S.S IMAGE`, so deleted photos stayed mapped forever. `find_missing_image_rows(base_path=None)` in `image_scanner.py` closes that gap:

- read-only — it never deletes or modifies anything itself
- pulls every image row via `database.get_all_images_with_link_status()`, which flags each row `mapped` using an `EXISTS` subquery (not a `LEFT JOIN`) against `mappings`, because one image can have more than one mapping row now that the `UNIQUE(image_id)` constraint is gone (Bulk Match links one image to many stock items) — a join would double-count
- resolves each row's stored relative `filepath` against `base_path` (the caller passes `IMAGE_SCAN_ROOT`) and checks `os.path.exists()`; anything that fails is "missing"
- for just that missing subset, looks up which confirmed stock item name(s) it was linked to via `database.get_stock_item_names_for_images()` — scoped to the missing ids so this doesn't cost a catalog-wide `GROUP_CONCAT` on every scan
- computes `over_threshold_warning`: true only when `missing_count >= MISSING_IMAGE_WARNING_MIN_COUNT` (20) **and** `missing_count / total_images > MISSING_IMAGE_WARNING_RATIO` (0.15). Both a floor and a ratio are required so a small catalog's noisy percentage (e.g. 1 missing out of 4 images) doesn't trip it, while a large fraction of a real catalog does. This is meant to catch `IMAGE_SCAN_ROOT` itself being temporarily unreachable (disconnected network drive, renamed folder) — in that case every row resolves as missing at once, which the ratio check reliably flags

`POST /scan_images` runs the unchanged add-side `scan_ss_image_folder()` and then always also calls `find_missing_image_rows()`, adding `missing_count`, `missing_mapped_count`, `over_threshold_warning`, `missing_image_ids`, and a `missing_images` list (`{id, car_folder, filename, filepath, mapped, stock_item_names}` per row) to the same JSON response — so the frontend gets file-level detail for a "View List" display without a second disk walk.

Nothing is deleted by the scan. `POST /admin/remove_missing_images` (admin-only) takes a JSON `image_ids` list, re-runs `find_missing_image_rows()` from scratch, and only deletes the intersection with what was requested — a file that reappeared between the scan and the confirm click (drive reconnected, folder restored) is never deleted even if the client still asks for it. The actual delete is `database.remove_missing_image_rows()`: it deletes the `images` row(s), and their `mappings` rows disappear automatically through the existing `ON DELETE CASCADE` foreign key, reverting that stock item back to "Needs link" — no separate mapping-delete step is needed.

`templates/train.html` shows a Remove/Keep prompt under `Rescan Images` when `missing_count > 0` (or, when over threshold, a more serious warning with Remove/Keep hidden — re-running `Rescan Images` after confirming the folder is reachable is the only way past it). A "View List" toggle renders the `missing_images` payload already in hand — folder/filename per row, with a "Was linked to: ..." badge for mapped ones — without calling the scan again.

## 13. Mapping workflow

The mapping workflow lives mostly in `app.py`, `database.py`, `matcher.py`, and `templates/train.html`.

### Core admin routes
- `GET /train` — accepts optional `?car=<name>&stock_item=<name>` query params; the "Add Image" pill on every admin-view design card on the main page links here with both filled in, so Training Mode lands with the car dropdown and stock item pre-selected (falls back to a "select manually" notice if the exact item isn't found)
- `GET /get_unmapped_images`
- `GET /train_images`
- `POST /confirm_mapping`
- `POST /remove_mapping`
- `POST /scan_images` — add-side rescan plus a read-only `find_missing_image_rows()` pass; response includes both the add-side counts and the missing-image detection fields (see section 12's "Missing-image detection and removal")
- `POST /admin/remove_missing_images` — takes `{"image_ids": [...]}`, re-verifies each is still actually missing before deleting, and removes those `images` rows (mappings cascade); returns `images_removed`/`mappings_removed` plus refreshed `stats`
- `POST /admin/upload_image` — lets an admin upload a photo straight from the `Train Matches` page instead of pre-copying it into `S.S IMAGE\` and rescanning; validates extension (`Config.ALLOWED_IMAGE_EXTENSIONS`) and size (`Config.MAX_IMAGE_SIZE`), saves it under `data/S.S IMAGE/<car_folder>/` (de-duplicating the filename with `_unique_filename_in_dir()` if one already exists), inserts an `images` row via `db.add_image()`, then confirms the mapping to the selected stock item through the same `_confirm_mapping_core()` helper used by `/confirm_mapping`
- `GET /mapping_stats`
- `GET /get_current_mapping_image?stock_item=<name>` — used by the `train.html` "Currently Matched Image" preview; looks up `db.get_mapping_for_stock_item()` and returns `{has_mapping, image_id, image_url, confidence}` as JSON so the admin can visually compare the existing match against the new image before confirming

### Bulk Match

`templates/bulk_match.html` (linked from the `Bulk Match` button on `train.html`) matches ONE image against MANY stock items at once — for products like floor mats and curtains where the same photo applies to hundreds of car variants. The flow is: pick a car folder and image, then find stock items either by free-text search or by product category, tick the ones that apply, and confirm in one shot.

Routes (all admin-only):
- `GET /bulk_match` — renders the page
- `GET /api/search_all_stock_items` — searches the whole catalog (not one car); supports either a free-text `?q=` substring match or a `?type=&color=` category filter, dedupes on (car, item), caps at 500 results (`truncated` flag), and includes each item's current mapping state via the batched `db.get_mappings_for_stock_items()`
- `GET /api/list_product_categories` — groups every stock item by canonical (type, color) via `extract_type_and_color()`, returning counts per bucket; cached until `main_hierarchy.json` changes (`PRODUCT_CATEGORY_CACHE`)
- `POST /admin/bulk_confirm_mapping` — takes `image_id` + a list of stock items and runs each through the same `_confirm_mapping_core()` as single confirms, reporting per-item failures without aborting the batch

### Product type/color categorization

`utils/product_normalize.py` powers the category buckets:
- `TYPE_PATTERNS` is an ordered list of (label, regex) pairs for generic product types (FOOT MAT, 7D MAT, GRASS MAT, CURTAINS, ...). The regexes tolerate spacing/apostrophe variations ("7D", "7'D", "7 D"). Trailing "MAT" is optional for 7D/9X/GRASS/NOODLE (verified against the real catalog — many genuine mats omit it) but deliberately REQUIRED for SPLIT and DICKY, where bare keywords produced real false positives.
- `COLOR_MAP` canonicalizes spelling variants (BAIGE→BEIGE, BLK/BALCK→BLACK, GRAY/D.GREY→GREY, ...). Colors are counted PER OCCURRENCE, not deduped — "BLACK + BLACK" is a distinct product from "BLACK" in this catalog, so the sorted, repeated color list joins into distinct keys like `BLACK-BLACK` vs `BLACK-TAN`. Counting uses one combined regex alternation scanned with `finditer()` so overlapping variants (e.g. "GREY" inside "D.GREY") are never double-counted.

### Mapping save behavior

The save logic is factored into `_confirm_mapping_core(image_id, stock_item_name, car_model, confidence, confirmed_by)` in `app.py`, shared by both `/confirm_mapping` and `/admin/upload_image` so a manually-confirmed match and a freshly-uploaded-and-matched image go through identical save/overwrite behavior.

When an admin confirms a mapping:
- the image row is looked up
- any other image currently mapped to the same `stock_item_name` is deleted first via `db.remove_mappings_for_stock_item(stock_item_name, exclude_image_id=image_id)`, so a stock item is only ever mapped to one image at a time (blank and `__UNMATCHABLE__` values are skipped, matching the existing folder-mapping guard just below it)
- the selected stock item is saved to `mappings`
- the car model hint is resolved
- high-confidence confirmed mappings also update `folder_car_mapping`

The direction of uniqueness matters and is easy to get backwards:
- one STOCK ITEM ↔ at most one image: enforced in app code by the delete-then-insert step above (never by a DB constraint on `stock_item_name`)
- one IMAGE ↔ many stock items: allowed since the `UNIQUE(image_id)` column constraint was removed by the startup migration described in section 6; the compound `UNIQUE(image_id, stock_item_name)` index only makes re-confirming the exact same pair idempotent

This is what lets Bulk Match confirm one photo against hundreds of items while each item still shows exactly one photo.

### Image serving behavior

Images are served through:
- `GET /get_image/<image_id>`
- `GET /get_stock_image`
- `GET /get_current_mapping_image` (JSON metadata only, not the image bytes — the frontend then loads the image itself via `/get_image/<image_id>`)

If no file can be resolved, the app returns an inline SVG placeholder instead of failing.

## 14. Match suggestion heuristics

`matcher.py` is currently heuristic, not AI-driven.

It ranks candidates using:
- exact or partial stock-code extraction
- folder-name similarity
- similarity to previously confirmed mappings

The public route `GET /suggest_match/<image_id>` is currently disabled and returns HTTP `410`.

## 15. Pricing (removed feature, legacy leftovers)

The app used to have a two-level pricing model (global base price plus per-customer override, with a `Contact Us` fallback and a per-customer `force_contact_us` flag). The feature was removed: there are no pricing routes in `app.py`, no `templates/pricing.html`, and nothing reads prices anywhere.

What remains, and should not be mistaken for a live feature:
- `database.py` still creates the `base_prices` and `customer_prices` tables (and their index) on init
- the `users` table still has a `force_contact_us` column
- `delete_user` still deletes the user's `customer_prices` rows as cleanup

If pricing is ever reintroduced, these leftovers are the starting point; until then they are dead schema.

## 16. Account management

Customer account administration is built into the same Flask app.

Important routes:
- `GET /admin/accounts`
- `POST /admin/create_user`
- `GET /admin/get_all_customers` — now also returns `access_code`, `is_active`/`status`, and `last_login` per customer
- `POST /admin/delete_user/<user_id>`
- `POST /admin/toggle_user_status/<user_id>` — flips a single customer's `is_active` flag (admin-only accounts can't be toggled; `db.toggle_customer_active_status()` raises `ValueError` if the target isn't a customer)
- `POST /admin/set_all_customer_status` — bulk-sets `is_active` for every customer account in one call (`db.set_all_customer_active_status()`), used by the `Resume All` / `Pause All` buttons on `templates/accounts.html`

The `users` table has `is_active` (default `1`) and `last_login` columns, added via `_ensure_users_schema()` so existing databases are migrated in place. `login()` rejects a customer login with HTTP `403` if `is_active` is `0`, and records `last_login` on every successful login through `db.update_last_login()`.

Each major account action is logged through `account_logs`, including bulk pause/resume (`bulk_paused` / `bulk_resumed` action labels).

### Secondary password gate

All six routes above (plus the `/admin/accounts` page route itself) also require `@accounts_access_required`, stacked directly after `@admin_required` — a valid admin session alone is no longer enough to reach customer account data:
- gated on `Config.ACCOUNTS_ACCESS_PASSWORD` (read from `.env`, no default — same "unset means disabled" pattern as `SYSTEM_ACCESS_TOKEN`); if unset, every accounts route returns `403` regardless of session state
- unlock is purely session-based (`session["accounts_unlocked"]`), NOT a device cookie like the System panel's pairing — `session.clear()` on both `/login` and `/logout` already wipes it, so it has to be re-entered every new login session
- `POST /admin/accounts/unlock` (itself behind `@admin_required`) checks the submitted password against `Config.ACCOUNTS_ACCESS_PASSWORD`, rate-limited through the same `_check_login_rate_limit()` used by `/login` (keyed separately as `accounts:<username>` so attempts don't share a bucket with regular login attempts), and sets the session flag on success before redirecting to `next` (validated through the existing `_safe_next_url()`)
- `accounts_access_required(is_page=True)` on the `/admin/accounts` route renders `templates/accounts_unlock.html` (a password interstitial styled like `login.html`) instead of the real page when locked; every other accounts route (JSON) just returns `403` with a clear message instead
- `@admin_required` still runs first in the decorator stack (it's listed above `@accounts_access_required` on every route), so a non-admin session is blocked before this gate is ever reached — the two checks are independent, not a replacement for one another

## 17. Search endpoints

`api/search.py` provides paginated JSON endpoints for Select2 widgets.

Endpoints:
- `GET /api/search_cars`
- `GET /api/search_car_folders`
- `GET /api/search_customers`
- `GET /api/get_stock_items_for_car`

Dependencies are injected from `app.py` through `set_search_dependencies(...)`.
This keeps the blueprint isolated from the main app state.

## 18. UI surfaces

The templates map cleanly to the major workflows:

- `templates/login.html`
  - sign-in screen
- `templates/index.html`
  - main stock viewer
  - admin-only update, training, and account links
  - `checkTimestampFreshness()` marks the "Last updated" text with the `.stale-timestamp` class (red, bold) whenever it is more than 180 seconds old (`STALE_WARNING_THRESHOLD_SECONDS` below does NOT touch this); it runs after every timestamp update and on a 10-second `setInterval`, so it turns red live even if no new data arrives (e.g. Tally down for a while)
  - a SEPARATE, much longer threshold, `STALE_WARNING_THRESHOLD_SECONDS` (900s / 15 minutes), gates a clearly visible banner (`#staleDataNotice`, near the top of the page) plus a short matching hint next to the timestamp — both explain WHY the last auto-refresh failed in plain language (multiple Tally windows, Tally closed, Tally slow, or an unexpected issue with a pointer to the System panel). Both read off one shared classifier, `classifyRefreshIssueBucket()`, fed by the existing 30-second `/refresh_status` poll — `error_code` when present, falling back to the same message keywords `_classify_tally_exception` uses — so there is exactly one place that decides which reason it is, never two competing schemes. `updateStaleWarningUI()` additionally requires `hasRealRefreshAttempt()` (a non-null `timestamp` on the payload) before showing anything, so the pre-refresh placeholder (`{"success": false, "message": "Not yet run", "timestamp": null}`, set at `last_refresh_status`'s module-level default in `app.py`) can never be mistaken for a real failure — this is what used to cause a false-positive banner immediately after a fresh restart, since an old cached file's mtime could already read as "stale" before the app had done anything. The separate red `#statusMessage` "Last error: ..." line was removed entirely from the failure path (it used to show unconditionally and read as a second, disconnected fragment next to the muted hint); on failure `#statusMessage` now stays blank and the banner/hint pair is the single consolidated notice. Both banner and hint clear automatically once a refresh cycle succeeds again; no separate timer was added, everything piggybacks on the existing 30-second `/refresh_status` poll and `checkTimestampFreshness()`'s existing 10-second tick
  - every admin-view design card carries an "Add Image" pill (its own `.add-image-link` class, not the plain `.fix-link` shared with Logout) linking to `/train?car=...&stock_item=...` so Training Mode opens pre-selected
  - car names in the heading and info messages are shown shelf-code-stripped (see section 11); the raw name still drives `?car=` requests
- `templates/train.html`
  - image mapping workflow
  - the "Currently Matched Image" preview (`#currentMatchImg`) is 280px on desktop / 200px on narrow screens (`@media (max-width: 640px)`), sized via CSS id rules rather than inline `max-width`/`max-height` so the mobile override can apply
  - admin-only `Upload Image` button opens a modal to pick a car folder and upload a photo straight to `POST /admin/upload_image`, skipping the manual copy-then-rescan flow
  - `Bulk Match` button links to `/bulk_match`
- `templates/bulk_match.html`
  - one-image-to-many-stock-items matching (see section 13): pick a shared image, find items by search or product category, confirm the checked set in one `POST /admin/bulk_confirm_mapping`
- `templates/system.html`
  - remote System panel (see section 21)
- `templates/accounts.html`
  - customer account management
  - accounts table adds Access Code, Status (Active/Paused), and Last Login columns, plus per-row Pause/Resume and bulk `Resume All`/`Pause All` controls
- `templates/accounts_unlock.html`
  - password interstitial rendered in place of `/admin/accounts` when the current session hasn't passed the accounts password gate (see section 16); styled consistently with `templates/login.html`

`templates/train.html`, `templates/accounts.html`, and `templates/bulk_match.html` share the same sticky topbar pattern (`.topbar` > `.topbar-left` / `.topbar-right`, `.link-button` for navigation, `.role-indicator` for the current role label) for visual consistency across admin screens; `templates/index.html` still uses the older `.role-badge` topbar style.

The main page supports both admin and customer roles.
Customer mode is read-only.

## 19. Operational scripts

### `first_time_setup.bat`

New-machine bootstrap script.
It:
- copies the project into `C:\tally_stock`
- creates the venv
- installs requirements
- creates `.env`
- optionally helps with tunnel setup
- optionally connects Git
- creates Desktop shortcuts
- writes Windows auto-start

### `update_app.bat`

Operational update path for Git-connected installs:
- stop current app
- `git pull origin main`
- verify and silently repair the `TallyStockViewer` autostart entry under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (idempotent; recreates it if missing or pointing at the wrong path, so an office PC can't silently lose auto-launch-on-login)
- restart `launcher.pyw`

The autostart check reads the registry value with `FOR /F` plus a plain string-equality comparison — NOT `findstr`. `findstr /C` literal matching was empirically found to fail unpredictably on Windows path patterns containing `\.` (e.g. `\.venv`); avoid `findstr` for path comparisons anywhere in this codebase.

### `stop_server.py`

Manual emergency stop helper for the server and tunnel PID files.

### `relaunch_helper.py`

Detached helper spawned by the System panel restart routes right before `app.py` exits (see section 3). Self-contained on purpose: the previous design trusted `launcher.pyw`'s watchdog to notice the dead process, and a real `pull_and_restart` once left the site down indefinitely because the launcher had a crash-on-start bug and the watchdog never ran.

### `scripts/measure_tally.ps1`

Read-only diagnostic that times the Tally stock-export requests (the old three-request cycle plus the current single-collection replacement) against a live Tally, for before/after numbers from real data. The same measurement is available remotely as the System panel's Tally Performance Test (section 21); the script remains for local PowerShell use.

## 20. Logging and health

### Logs
- Flask and waitress write to `logs/app.log`
- `launcher.pyw` also appends operational events to the same log path

### Health endpoint
- `GET /health`

Current health output includes:
- overall status
- database status
- tally URL
- auto-export flag
- mapping statistics

The health endpoint checks SQLite access, not live Tally reachability.

## 21. Remote System panel

`templates/system.html` plus the `/admin/system/*` routes in `app.py` form a remote ops panel so the office PC can be managed without RDP/PowerShell access.

### Access model

Every panel route requires BOTH the admin session (`@admin_required`) AND a paired device (`@system_device_required`):
- pairing happens once per browser via `GET /admin/system/authorize-device?token=<SYSTEM_ACCESS_TOKEN>`, which validates the token from `.env` and sets a signed, `HttpOnly`, `SameSite=Strict` device cookie before redirecting to the panel
- if `SYSTEM_ACCESS_TOKEN` is not set, the panel is disabled entirely (`403`)

### Routes

- `GET /admin/system` — renders the panel
- `GET /admin/system/status` — local commit, last 10 commits, remote `origin/main` commit (via `git fetch`), and an `up_to_date` flag; degrades to `offline: true` when the fetch fails
- `GET /admin/system/logs` — tails `logs/app.log` (up to 1000 lines)
- `POST /admin/system/pull_and_restart` — `git pull origin main` then restart via `relaunch_helper.py`
- `POST /admin/system/restart_app_only` — restart without pulling
- `GET /admin/system/download_backup` — downloads a timestamped copy of `mappings.db`
- `GET /admin/system/find_duplicate_images`
- `GET /admin/system/tally_status` — Tally reachability plus the multiple-instance count, with a plain-words warning when more than one Tally is open
- `GET /admin/system/autostart_status` — `reg query` check (via subprocess, `CREATE_NO_WINDOW`) that the `TallyStockViewer` autostart Run entry exists and points at the right path; shown as an "Autostart" row ("Configured correctly" / "Missing or incorrect")
- `GET /admin/system/tally_perf_test` — the browser-triggerable port of `scripts/measure_tally.ps1`: sends the old three export requests plus the current single-collection request through `_post_tally_with_retry()`, timing each and returning row counts and sample name/qty pairs; pre-checks reachability and multiple instances first. This is the measurement that justified the single-request export in section 9
- `GET /admin/system/env_summary`, `GET /admin/system/disk_usage`, `GET /admin/system/uptime` — environment/diagnostic read-outs

All git subprocess calls go through `_run_git_command()`, which passes `creationflags=subprocess.CREATE_NO_WINDOW` — the server runs under `pythonw.exe` (no console), so without this every git spawn flashed a visible terminal window on the office PC screen.

## 22. Important code constraints and gotchas

- `first_time_setup.bat` always installs into `C:\tally_stock`
- the app assumes Windows and uses Windows-specific launcher behavior
- the health endpoint does not prove Tally is online
- the current search and design matching logic is heuristic and token-based
- seeded credentials still exist if the database has not been hardened
- public tunnel startup is conditional on `cloudflared.exe` being present in the app root
- the generated Desktop stop shortcut only kills the server PID; the full clean shutdown path is the tray menu item `Stop & Exit`
- `templates/train.html` exposes an admin-only `Rescan Images` button that calls `POST /scan_images` directly; it also runs automatically on every app startup
- the missing-image removal flow trusts nothing from the earlier scan at delete time: `POST /admin/remove_missing_images` always re-runs `find_missing_image_rows()` itself and intersects with the requested ids, so a reconnected drive between scan and confirm-click can't cause a real file to be deleted. Never change this to delete the client-supplied id list directly
- do not lower `MISSING_IMAGE_WARNING_MIN_COUNT`/`MISSING_IMAGE_WARNING_RATIO` (`image_scanner.py`) without re-checking both conditions together — the floor and the ratio each guard a different false-positive: the floor stops a tiny catalog's noisy percentage from tripping the warning, the ratio stops a large catalog from ever reaching the floor on genuine one-by-one deletions
- a full refresh (car master + main hierarchy + item stock) also runs automatically once, ~45 seconds after every app startup, sharing `run_full_refresh_job()` with the manual `Full Refresh` button; it silently skips if a refresh is already running and fails gracefully (logged, `full_refresh_status` set to `error`) if Tally isn't reachable yet — it never blocks startup or crashes the app
- each stock item can only be mapped to one image at a time; confirming a new image against a stock item that's already mapped elsewhere silently deletes that other mapping first (`db.remove_mappings_for_stock_item`). The reverse is NOT true: one image may map to many stock items (Bulk Match depends on this) — do not reintroduce a unique constraint on `mappings.image_id`
- `strip_shelf_code_for_display()` is presentation-only; anything sent to the backend (`?car=` params, matching against `car_master.json` / `main_hierarchy.json`) must use the raw, unstripped name. The templates carry hand-ported JS copies of the function — if the Python version changes, change every JS copy too
- never use `CAR_DESIGN_MAP` as a fallback for a car missing from the hierarchy; its token matching is over-inclusive (common tokens like "MAT" pull in hundreds of unrelated items) and reintroducing it re-creates the deleted-car wrong-designs bug
- do not use `findstr` for path comparisons in batch scripts; its `/C` literal matching fails unpredictably on patterns containing `\.` (see `update_app.bat`'s autostart check for the working `FOR /F` + string-equality pattern)
- every subprocess spawn must pass `creationflags=subprocess.CREATE_NO_WINDOW`; the server runs under `pythonw.exe` and any unsuppressed spawn flashes a console window on the office PC
- the item stock export intentionally sends ONE collection request; do not add Stock Summary report requests back into the cycle — the detailed+exploded report costs ~15-20s of Tally engine work per call and visibly stalls Tally Prime
- `.env` is no longer tracked in git (it holds rotated secrets including `SYSTEM_ACCESS_TOKEN`); a fresh clone gets it from `first_time_setup.bat`, not from the repo
- `/admin/upload_image` sanitizes `car_folder` against path separators and `..` (`_sanitize_upload_car_folder()`) and the filename against directory components (`_sanitize_upload_filename()`) before touching the filesystem — do not bypass these when adding new upload entry points
- in `templates/train.html`, `#stockItemSelect` is a plain `<select>` (not Select2); its `change` event only fires on real user interaction. Any code path that sets `select.value` programmatically (auto-matching by filename/car folder, restoring a preferred stock item, or leaving the browser's default first-option selection in place) must explicitly call `handleStockItemSelection()` afterward, or the "Currently Matched Image" preview silently stays out of sync until the user manually changes the dropdown — this was the root cause of a bug where the preview only appeared from the second selection onward
- `updateTimestamp()` in `templates/index.html` declares `statusEl` once, before both of its `try` blocks. It used to be declared inside the first `try` block only, so the second block referenced it out of scope and threw a `ReferenceError` on every single `/refresh_status` poll — caught and logged to the console, never surfaced, so the auto-refresh hint text silently never updated in any real browser despite looking correct on a read of the code. Reading the code was not enough to catch this; it only showed up by actually executing the script. Keep both status-fetching blocks sharing the one `statusEl` reference
- `accounts_access_required`'s rate limit key (`accounts:<username>`) is deliberately namespaced away from the plain `<username>` key `/login` uses in the same `LOGIN_ATTEMPTS` dict — don't collapse them, or a user's login attempts and their accounts-password attempts would consume the same budget
- the stale-data warning banner/hint (`templates/index.html`) must never fire off `success === false` alone — always also check `hasRealRefreshAttempt()` (a real `timestamp`) and the separate `STALE_WARNING_THRESHOLD_SECONDS` (900s) staleness check. `last_refresh_status`'s pre-refresh placeholder in `app.py` is also `success: false`; treating that as a real failure is exactly what caused the banner to appear immediately after every fresh restart

## 23. File map for maintenance

If you need to change a behavior, start here:

- Tally refresh/export: `app.py`, `tally/sync.py`
- SQLite schema or user logic: `database.py`
- image scan behavior: `image_scanner.py`
- matching heuristics: `matcher.py`
- shared name normalization / shelf-code stripping: `utils/normalize.py`
- product type/color categorization (Bulk Match buckets): `utils/product_normalize.py`
- login/session settings: `config.py`, `app.py`
- tray startup and tunnel behavior: `launcher.pyw`, `serve.py`
- System panel restart mechanism: `relaunch_helper.py`, `app.py` (`/admin/system/*` routes)
- Manage Accounts secondary password gate: `app.py` (`accounts_access_required`, `/admin/accounts/unlock`), `templates/accounts_unlock.html`, `config.py` (`ACCOUNTS_ACCESS_PASSWORD`)
- Tally timing diagnostics: `scripts/measure_tally.ps1`, `/admin/system/tally_perf_test`
- admin and customer UI: `templates/`
- install/update scripts: `first_time_setup.bat`, `update_app.bat`
