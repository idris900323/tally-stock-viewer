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
- `design_categories` stores the per-stock-item material-tier category assignments (Pearl, Ruby, Saka, ...) — see section 22. Not to be confused with the Bulk Match product-type/color buckets in section 13, which are derived on the fly from the item name and never stored

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
- `serve.py` also works unmodified as a cloud (Render) entry point: it reads `PORT` from the real OS environment if set and binds waitress to that port and `0.0.0.0` instead of the hardcoded `127.0.0.1:5000` (see section 4's "Server binding"); the office PC path (`PORT` unset) is byte-for-byte unchanged. It also prints a few unconditional `[DIAGNOSTIC]` lines (host/port about to bind, the raw `PORT` env var) straight to stdout, not through `logging` — added after a real Render deploy went silent for two minutes with no visible output, traced to `_configure_logging()` attaching only a file handler (see section 20) so even waitress's own "Serving on http://..." line never reached the platform's log viewer. `_configure_logging()` now also attaches a console `StreamHandler`, so this class of silent-cloud-startup problem shouldn't recur, but the raw prints stay as a belt-and-suspenders trace for the handful of lines between process start and the first log call succeeding
- restart routes (`pull_and_restart`, `restart_app_only`) refuse with `409` while a cloud backup sync is running (see section 25) — `_trigger_self_restart()` ultimately calls `os._exit(0)`, an immediate kill with no graceful shutdown, which could otherwise land inside the narrow window in `cloud_backup._upload_or_update_file()` between a file's Drive upload succeeding and its manifest entry being saved, orphaning a real duplicate on Drive

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

### Cloud backup (Google Drive) — see section 25
- `GDRIVE_OAUTH_CLIENT_SECRETS_PATH`, `GDRIVE_OAUTH_TOKEN_PATH`, `GDRIVE_BACKUP_FOLDER_ID` — all required together; missing any one keeps the feature a clean no-op
- `CLOUD_BACKUP_INTERVAL` — seconds between scheduled syncs (default `259200` / 3 days)

### Cloud deployment (push-based sync to a cloud instance) — see section 26
- `DISABLE_TALLY_SCHEDULING` — set on a cloud instance that can never reach Tally; `"1"`/`"true"`/`"yes"`, default off
- `INTAKE_SYNC_TOKEN` — the secret a cloud instance's `/admin/intake/sync_data` checks against; unset keeps that route `403` for everyone
- `CLOUD_SYNC_URL`, `CLOUD_SYNC_TOKEN` — set on the OFFICE PC to push to a cloud instance's intake endpoint after each local export; either missing keeps the push a clean no-op

### Server binding (`serve.py`)
- `PORT` — read directly via `os.environ`, not through `Config`. If set (Render and most PaaS hosts always set this for web services), `serve.py` binds waitress to that port instead of the hardcoded `5000`. Always binds host `0.0.0.0` regardless — safe in both environments since actual public exposure is controlled by Cloudflare Tunnel (office PC) or the platform's own network layer (Render), never by this bind address directly.

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
- `data/.cloud_backup_manifest.json`, `data/.cloud_backup_status.json`, `data/gdrive_oauth_token.json` — cloud backup's manifest, persisted run/skip history, and saved OAuth token (section 25); none of these three are themselves included in what gets backed up

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

### Security headers and `robots.txt`

`app.py`'s `add_security_headers()` (`@app.after_request`) stamps every response, public or authenticated, with:
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `X-XSS-Protection: 1; mode=block` (pre-existing)
- `Referrer-Policy: same-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`, `X-Robots-Tag: noindex, nofollow`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` — only sent when `Config.SESSION_COOKIE_SECURE` is true, i.e. only once a deployment has already told the app it's served over HTTPS; forcing it unconditionally would break a plain-`http` local/office setup by telling browsers to refuse future non-HTTPS connections to that host

`GET /robots.txt` (added to `PUBLIC_ENDPOINTS` alongside `login`/`logout`/`static`/`full_refresh_status_route`) returns a blanket `User-agent: *\nDisallow: /`. This is a private customer/admin catalog, not a marketing site — the goal is keeping it out of search indexes regardless of what domain fronts it (see `GOING_PUBLIC.md`), not permissions or access control; `X-Robots-Tag` reinforces the same intent at the HTTP-header level for any crawler that ignores `robots.txt`.

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
- `QUEUE_STATS_CACHE`
  - per-car completion stats behind the Needs Category / Needs Image Matching work queues (section 23), keyed on `(hierarchy file fingerprint, _QUEUE_STATS_VERSION)` — see that section for the invalidation write paths
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
- `GET /train` — accepts optional `?car=<name>&stock_item=<name>` query params; the "Add Image" pill on every admin-view design card on the main page links here with both filled in (plus `from_add_image=1`, see "Return to home car" below), so Training Mode lands with the car dropdown and stock item pre-selected (falls back to a "select manually" notice if the exact item isn't found)
- `GET /get_unmapped_images`
- `GET /train_images`
- `POST /confirm_mapping` — accepts an optional `category` field in the JSON body (Training Mode's category picker, see below); omitted entirely, the mapping save behaves exactly as before
- `POST /remove_mapping`
- `POST /scan_images` — add-side rescan plus a read-only `find_missing_image_rows()` pass; response includes both the add-side counts and the missing-image detection fields (see section 12's "Missing-image detection and removal")
- `POST /admin/remove_missing_images` — takes `{"image_ids": [...]}`, re-verifies each is still actually missing before deleting, and removes those `images` rows (mappings cascade); returns `images_removed`/`mappings_removed` plus refreshed `stats`
- `POST /admin/upload_image` — lets an admin upload a photo straight from the `Train Matches` page instead of pre-copying it into `S.S IMAGE\` and rescanning; validates extension (`Config.ALLOWED_IMAGE_EXTENSIONS`) and size (`Config.MAX_IMAGE_SIZE`), saves it under `data/S.S IMAGE/<car_folder>/` (de-duplicating the filename with `_unique_filename_in_dir()` if one already exists), inserts an `images` row via `db.add_image()`, then confirms the mapping to the selected stock item through the same `_confirm_mapping_core()` helper used by `/confirm_mapping`; also accepts an optional `category` multipart form field, same picker as `/confirm_mapping`
- `GET /mapping_stats`
- `GET /get_current_mapping_image?stock_item=<name>` — used by the `train.html` "Currently Matched Image" preview; looks up `db.get_mapping_for_stock_item()` and returns `{has_mapping, image_id, image_url, confidence, category}` as JSON so the admin can visually compare the existing match against the new image before confirming. `category` is looked up and returned regardless of `has_mapping` — the category picker (below) needs an item's category even before it has an image

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

### Category picker in Training Mode

Confirm Match and the Upload Image modal in `templates/train.html` each carry an optional Category `<select>` (`#matchCategorySelect` / `#uploadCategorySelect`) alongside the existing image-matching fields — an additional entry point alongside the standalone Assign Category / "Manage Categories for [Car]" flow on the main page (section 22), not a replacement for it. Both pickers are populated live from `GET /api/categories` and default to whatever category the selected stock item already has (via `get_current_mapping_image`'s `category` field above), so leaving the picker untouched and confirming never changes or clears an existing category.

Backend: `_apply_confirm_category_update(stock_item_name, category_raw)` in `app.py` is the shared decision function behind both `/confirm_mapping` and `/admin/upload_image`:
- returns `None` (no-op) if `category` was omitted from the request entirely — this is what keeps `bulk_confirm_mapping` and any other existing caller byte-for-byte unchanged, since they never send the field
- also returns `None` if the submitted value already matches the item's current category, so a no-op confirm doesn't needlessly bump `assigned_at`/`assigned_by` via `upsert_design_category()`
- otherwise calls `db.upsert_design_category()` (non-blank value) or the new `db.remove_design_category()` (blank value — the picker's explicit "(No category)" clear option), then calls `_invalidate_queue_stats_cache()` (section 23) and `_regenerate_badges_for_stock_items()` (section 22) — the exact same triggers the standalone Assign Category flow already uses, so a category set from Training Mode is indistinguishable afterward from one set the other way
- `db.remove_design_category(stock_item_name)` (`database.py`) is a new, single-item counterpart to the existing category-wide `delete_category()` — it clears one stock item's `design_categories` row and is what makes an explicit "clear category" possible for the first time (previously `upsert_design_category()` was the only write path, so a category could only ever be reassigned, never cleared, outside of deleting the whole category)

`/confirm_mapping`'s response includes `category_update: {changed, category, error}` when a category was involved, and `category_stats` (the same shape `_compute_category_completion_stats()` returns, see section 23) when it actually changed — `templates/train.html` uses this to refresh the category progress card in place without a reload, the same way `updateProgress()` already refreshes the image-mapping progress card. If the mapping save itself succeeds but the category update fails, the response's `status` becomes `"saved_category_failed"` (`"success_partial": "category_failed"` for `/admin/upload_image`) rather than silently dropping the category error.

### Return to home car after an Add Image confirm

Design cards' "Add Image" link (section 18) now appends `from_add_image=1` to its `/train?car=...&stock_item=...` URL — an explicit marker, not inferred from the presence of `car`+`stock_item` alone, since other deep links (the Needs Image Matching work queue, section 23) reuse those same two params without wanting this behavior. `train.html`'s `maybeStoreReturnToHomeCarFromUrl()` reads it on page load, strips it from the URL (same one-shot pattern as `?open_queue=`), and — if present — stashes the target car in `sessionStorage` under `returnToHomeCar`.

The next successful `Confirm Match` during that same visit (`submitMapping()`) checks `sessionStorage` first: if the flag is set, it's cleared immediately (one-shot within the visit) and the browser is redirected to `/?car=<car>`, reusing `index.html`'s existing `?car=` pre-selection code path (`restoreCarFromUrl()`) rather than a separate mechanism — landing the admin back on the original car's design list instead of Training Mode's own next-image flow. A normal, direct visit to Training Mode (top nav, Bulk Match, a work-queue link) never sets the flag, so this never fires for those.

Because the flag lives in `sessionStorage` and survives until either a confirm or an explicit clear, `Home`, `Bulk Match`, and `Logout` in `train.html`'s topbar all call `clearPendingReturnToHomeCar()` on click — leaving Training Mode without confirming drops the pending redirect, so it can't unexpectedly fire on some unrelated later visit to the page in the same tab.

### Image serving behavior

Images are served through:
- `GET /get_image/<image_id>`
- `GET /get_stock_image`
- `GET /get_current_mapping_image` (JSON metadata only, not the image bytes — the frontend then loads the image itself via `/get_image/<image_id>`)

If no file can be resolved, the app returns an inline SVG placeholder instead of failing.

### Share image flow

The main page also exposes a share workflow for selected images:
- `GET /get_share_image/<image_id>` builds and serves a cached share-optimized JPEG under `data/share_cache/`
- `GET /get_share_image_badged/<image_id>` adds the item's category badge when available and falls back to the plain share image when it is not; the response always carries `Cache-Control: no-cache, must-revalidate` (see section 22's "Badge cache versioning") so a Cloudflare edge cache or browser can't keep replaying a stale badge purely because the URL (fixed by `image_id` alone) never changes when the category or badge-drawing logic does — `no-cache` still permits caching, it just forces a conditional revalidation against the existing `ETag`/`Last-Modified` (from `conditional=True`) before reuse
- `templates/index.html` provides the `Share Images` button that drives this flow

The "category" here is the material-tier tag from section 22 (Pearl/Ruby/Saka/...), not the Bulk Match product-type bucket above — see section 22 for how it's assigned and section 23 for the dashboards that surface which cars still need it assigned.

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
  - admin-only update, training, share-images, and account links
  - `checkTimestampFreshness()` marks the "Last updated" text with the `.stale-timestamp` class (red, bold) whenever it is more than 180 seconds old (`STALE_WARNING_THRESHOLD_SECONDS` below does NOT touch this); it runs after every timestamp update and on a 10-second `setInterval`, so it turns red live even if no new data arrives (e.g. Tally down for a while)
  - a SEPARATE, much longer threshold, `STALE_WARNING_THRESHOLD_SECONDS` (900s / 15 minutes), gates a clearly visible banner (`#staleDataNotice`, near the top of the page) plus a short matching hint next to the timestamp — both explain WHY the last auto-refresh failed in plain language (multiple Tally windows, Tally closed, Tally slow, or an unexpected issue with a pointer to the System panel). Both read off one shared classifier, `classifyRefreshIssueBucket()`, fed by the existing 30-second `/refresh_status` poll — `error_code` when present, falling back to the same message keywords `_classify_tally_exception` uses — so there is exactly one place that decides which reason it is, never two competing schemes. `updateStaleWarningUI()` additionally requires `hasRealRefreshAttempt()` (a non-null `timestamp` on the payload) before showing anything, so the pre-refresh placeholder (`{"success": false, "message": "Not yet run", "timestamp": null}`, set at `last_refresh_status`'s module-level default in `app.py`) can never be mistaken for a real failure — this is what used to cause a false-positive banner immediately after a fresh restart, since an old cached file's mtime could already read as "stale" before the app had done anything. The separate red `#statusMessage` "Last error: ..." line was removed entirely from the failure path (it used to show unconditionally and read as a second, disconnected fragment next to the muted hint); on failure `#statusMessage` now stays blank and the banner/hint pair is the single consolidated notice. Both banner and hint clear automatically once a refresh cycle succeeds again; no separate timer was added, everything piggybacks on the existing 30-second `/refresh_status` poll and `checkTimestampFreshness()`'s existing 10-second tick
  - every admin-view design card carries an "Add Image" pill (its own `.add-image-link` class, not the plain `.fix-link` shared with Logout) linking to `/train?car=...&stock_item=...&from_add_image=1` so Training Mode opens pre-selected; the `from_add_image=1` marker is what makes the first confirm during that visit return to this car's design list instead of Training Mode's own next-image flow (section 13, "Return to home car after an Add Image confirm")
  - car names in the heading and info messages are shown shelf-code-stripped (see section 11); the raw name still drives `?car=` requests
  - the `More` popover (`#moreMenu`, `toggleMoreMenu()`/`openMoreMenu()`/`closeMoreMenu()`) holds five items behind one trigger: `Full Refresh`, `Manage Accounts`, `System` (a visual divider), `Category Settings` (section 22's admin-editable category list — a visual divider), then a single `Work Queue` entry (section 23; the earlier separate `Needs Category` and `Needs Image Matching` entries were merged into this one item, `?open_queue=work`, once `train.html` gained an in-page tab toggle for the same two lists) — closes on item click, outside click, or Escape; every item runs through `handleMoreMenuAction()` so the menu can never linger open after a selection
  - the car-scoped `Assign Category` button opens the continuous Manage Categories session described in section 22
- `templates/train.html`
  - image mapping workflow
  - the "Currently Matched Image" preview (`#currentMatchImg`) is 280px on desktop / 200px on narrow screens (`@media (max-width: 640px)`), sized via CSS id rules rather than inline `max-width`/`max-height` so the mobile override can apply
  - admin-only Category `<select>` fields (`#matchCategorySelect` next to Confirm Match, `#uploadCategorySelect` in the Upload Image modal) let an admin set/change/clear the selected stock item's category in the same action as confirming or uploading its image (section 13)
  - admin-only `Upload Image` button opens a modal to pick a car folder and upload a photo straight to `POST /admin/upload_image`, skipping the manual copy-then-rescan flow
  - `Bulk Match` button links to `/bulk_match`
  - a category-completion progress card (`#categoryStatsCard` — Categorized/Total Items/Remaining/Complete) sits alongside the existing image-mapping progress card, both refreshed in place after a relevant save without a page reload
  - the merged Needs Category / Needs Image Matching work-queue panel (section 23) is hidden by default; it only renders when arriving via `?open_queue=work|category|image_matching` from the More menu's single `Work Queue` entry above (or the page's own close-then-reopen isn't possible — there is no in-page trigger, only the URL param), and has its own close control
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
- Flask and waitress write to `logs/app.log` via a `RotatingFileHandler` attached to the root logger in `_configure_logging()`
- the same function also attaches a console `StreamHandler` (added after a real Render debugging session — see section 3's "diagnostic" note) so every logging-module message, including waitress's own startup line and any `logger.exception()` from a background thread, reaches stdout too, not just the file. Both handlers stay attached permanently, not just for that one diagnosis
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

`"health"` is in `PUBLIC_ENDPOINTS` (unlike every other route) — a PaaS host's automated HTTP health check is an unauthenticated prober, and before this it got a `401` login page like every other gated route instead of the `200`/`503` JSON it's built to return. Render's edge treats a failing health check as "instance unhealthy" and refuses to route any real traffic to it at all, producing a `502` at the edge with zero request logs (the edge never proxies through) — this is exactly the failure that was reproduced and fixed. On a PaaS deployment, point the platform's own Health Check Path setting at `/health`.

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
- `POST /admin/system/pull_and_restart` — `git pull origin main` then restart via `relaunch_helper.py`; refuses with `409` if a cloud backup sync is currently running (section 25)
- `POST /admin/system/restart_app_only` — restart without pulling; same `409` guard while a cloud backup sync is running
- `GET /admin/system/download_backup` — downloads a timestamped copy of `mappings.db`
- `GET /admin/system/find_duplicate_images`
- `GET /admin/system/tally_status` — Tally reachability plus the multiple-instance count, with a plain-words warning when more than one Tally is open
- `GET /admin/system/autostart_status` — `reg query` check (via subprocess, `CREATE_NO_WINDOW`) that the `TallyStockViewer` autostart Run entry exists and points at the right path; shown as an "Autostart" row ("Configured correctly" / "Missing or incorrect")
- `GET /admin/system/tally_perf_test` — the browser-triggerable port of `scripts/measure_tally.ps1`: sends the old three export requests plus the current single-collection request through `_post_tally_with_retry()`, timing each and returning row counts and sample name/qty pairs; pre-checks reachability and multiple instances first. This is the measurement that justified the single-request export in section 9
- `GET /admin/system/share_cache_files` — read-only listing of every file actually present in `data/share_cache/` (filename, `is_badge`, size, real mtime), newest first; lets an admin confirm whether a cached badge predates a given fix by comparing mtimes, without shell/RDP access
- `POST /admin/system/clear_badge_cache` — deletes every `*_badge_*.jpg` file from `data/share_cache/` (plain, non-badged share cache is untouched); the manual, on-demand counterpart to `BADGE_FORMAT_VERSION`'s automatic invalidation (section 22) for whenever a full sweep is wanted without a developer involved
- `GET /admin/system/env_summary`, `GET /admin/system/disk_usage`, `GET /admin/system/uptime` — environment/diagnostic read-outs
- `GET /admin/system/cloud_backup/status` — running state, last run (with `verification`/`phase_timing`), live progress, skipped-run history (section 25)
- `POST /admin/system/cloud_backup/authorize` — the one-time OAuth consent flow; `GET .../auth_status` polls it
- `POST /admin/system/cloud_backup/sync_now` — manual trigger; `400` if not yet authorized
- `POST /admin/system/cloud_backup/stop` — cooperative cancel of an in-progress sync

All git subprocess calls go through `_run_git_command()`, which passes `creationflags=subprocess.CREATE_NO_WINDOW` — the server runs under `pythonw.exe` (no console), so without this every git spawn flashed a visible terminal window on the office PC screen.

## 22. Material-tier category assignment

Independent of the Bulk Match product-type/color categorization in section 13 (`utils/product_normalize.py`, derived on the fly from the item name and never stored), stock items can also be tagged with a material-tier category — Pearl, Pearl Designer, Pearl Deluxe, Saka, Ruby, Napa Deluxe, Napa Designer out of the box — for merchandising/display purposes. The category LIST itself is admin-editable (add/rename/merge/delete, reorder, abbreviation override) — see "Admin-editable category list" below.

### Storage

`database.py`'s `design_categories` table (section 6) stores one row per stock item, keyed on `normalize_lookup_key(stock_item_name)` — the same shared key `get_mappings_for_stock_items()` uses. `category` is a plain string (not a foreign key) validated in Python against the live `categories` table (`db.category_exists()`) rather than a SQL `CHECK` constraint — the fixed 7-value CHECK a pre-editable-category version of this table had is removed on startup by the guarded, backup-then-rebuild `_migrate_remove_design_categories_check_constraint()` migration (same pattern as `_migrate_remove_image_id_unique()`). Assignment is last-write-wins (`upsert_design_category()`, `ON CONFLICT(stock_item_key) DO UPDATE`): there is no assignment history, only overwrite-in-place. Clearing a single item's category is a separate, single-purpose write, `db.remove_design_category(stock_item_name)` (a plain `DELETE ... WHERE stock_item_key = ?`) — added alongside the Training Mode category picker's explicit "(No category)" option (section 13); it is distinct from `delete_category()` below, which clears an entire category name's worth of items at once by removing the category itself.

`database.py`'s `categories` table holds the editable list itself: `id`, `name` (unique, case-insensitively via a `LOWER(name)` unique index), `sort_order`, and `abbreviation` (always populated — auto-generated via `generate_category_abbreviation()`, or admin-overridden, never null). Seeded once, on first startup after this feature shipped, from the previous fixed 7-category tuple and its hand-curated abbreviations (`DESIGN_CATEGORY_CHOICES` / `DESIGN_CATEGORY_ABBREVIATIONS_SEED`), so upgrading an existing install is a transparent, zero-visible-change event — same names, same order, same abbreviations as before.

### Admin-editable category list

- **Simple flow — "Category Settings"** (`templates/index.html`, reached from the More menu, distinct from the per-item "Assign Category"/"Manage Categories for [Car]" flow below): a lightweight modal listing every category with inline Rename/Delete and an Add box. `POST /admin/categories/add` (auto-generates the abbreviation, appends to the end), `POST /admin/categories/rename` (cascades the new name onto every `design_categories` row referencing the old one — or, if the new name collides case-insensitively with a DIFFERENT existing category, returns `{conflict: true, existing_name, affected_count}` instead of renaming, so the frontend can offer `POST /admin/categories/merge` — moves every tagged item from source to target, then deletes the source category), and `POST /admin/categories/delete` (removes the category row AND every `design_categories` row that had it, reverting those items to uncategorized — `GET /admin/categories/<name>/usage` previews the affected-item count first for the delete warning). All four are admin-only and re-bump `_QUEUE_STATS_VERSION` (section 23) since they can change a car's completion stats.
- **Advanced flow — System panel** (`templates/system.html`, same admin + device-pairing gate as every other System panel feature): reorder (`POST /admin/system/categories/move`, a simple adjacent `sort_order` swap — no drag-and-drop) and abbreviation override (`POST /admin/system/categories/abbreviation`).
- `GET /api/categories` — read-only, login-required but not admin-gated (customers see the ribbon/badge too) — the live `{name, abbreviation, sort_order}` list; `templates/index.html` re-fetches this after any Category Settings change to keep the "Assign Category" picker in sync without a page reload.

### Backend

- `POST /admin/assign_category` (`app.py`) — admin-only; takes `{category, stock_item_names: [...]}`, validates `category` against the live table (`db.category_exists()`), calls `db.upsert_design_category()` for each name, and returns `{assigned_count, category, failed}`. Also starts a background thread to pre-generate the badged share-image variant (section 13's share flow) for every just-tagged item that already has a mapped image, so the badge is warm before anyone shares it.
- `_build_design_payload()` (`app.py`) attaches each item's category via a batched `db.get_categories_for_stock_items()` lookup (same shape as the mapping lookup) and sorts the whole payload by category rank, built fresh per call from `db.get_all_categories()`'s live `sort_order` (small table, no caching needed), uncategorized items sorted last. The sort is stable, so within one category group items keep whatever order the qty-scan/hierarchy handed in. Both `/designs` (customer/admin browsing) and `/api/get_all_items_for_car` (section 23) go through this one function, so the grouping is identical everywhere a design list renders — including share order, since the share flow reads cards in this same DOM/render order.
- Rename/merge/abbreviation-override reuse one extracted trigger, `_regenerate_badges_for_stock_items()` (Part 4 of the category-settings upgrade) — fire-and-forget on a background thread, same as the existing per-item pre-generation, so the admin's request never waits on however many images need re-badging. Category deletion instead calls `_remove_badge_cache_for_image()` synchronously (plain filesystem deletes, no rebuild needed) since a deleted category leaves nothing to badge.
- **No staleness window**: `/get_share_image_badged/<id>` never trusts a cached file's mere existence — `_get_category_info_for_image()` looks up the item's category live from `design_categories`/`categories` on every request, and `_badged_share_cache_path()` derives the cache filename from that live name, so a request arriving mid-regeneration either finds an already-fresh file or (cache miss under the new name) builds one inline from current state; the background job only affects whether that build is already warm, never what it returns. Confirmed with a real concurrent-request test (zero-sleep requests immediately following a rename/abbreviation-override on a real multi-item batch) rather than just reasoned about — see the `regen_status` endpoint below for the one caveat this does NOT cover.
- **Known gap (pre-existing, not fixed by the above)**: a badge is looked up via `db.get_mapping_by_image_id()`, which returns an arbitrary single mapping row for images shared across many stock items (Bulk Match, section 13 — one photo can back 100+ car/stock-item combinations). If the row it happens to pick isn't the one tagged with a category, `_get_category_info_for_image()` returns `None` and the route falls back to the plain, unbadged share image — safe (never wrong/stale content), but such a shared image can silently never get a badge even though a sibling stock item sharing it IS categorized. Distinct from the staleness question above; not addressed here.
- `GET /admin/categories/regen_status` (admin-only) — polling endpoint backing a purely informational "Updating N of M images..." indicator (near the More menu in `templates/index.html`, and next to the Categories panel in `templates/system.html`) shown only once a rename/merge/abbreviation-override affects more than `_BADGE_REGEN_PROGRESS_THRESHOLD` (3) items — same lightweight global-status-dict-plus-polling shape as `full_refresh_status`/`/full_refresh_status`. Never gates or disables Share Images or anything else, since correctness doesn't depend on it finishing.

### Frontend: the continuous "Manage Categories" session

`templates/index.html` — a car-scoped, admin-only tagging session, opened via the `Assign Category` button in the car toolbar or the `enterCategoryAssignMode()` deep-link entry point the work-queue links use (section 23). Unlike a one-shot picker, the session stays open across as many category picks and batches as needed:

1. `enterCategoryAssignMode()` loads every item for the selected car via `/api/get_all_items_for_car` (qty-agnostic — zero-stock and unmapped items are taggable too, not just what customers currently see), reusing `renderDesignItem()` so cards look identical to the normal browsing view.
2. `handleCategoryActivePick()` sets which category the next `Apply` click will use, independent of the current selection.
3. Ticking cards accumulates `categorySelectedKeys`; `applyCategoryToSelection()` posts the current batch to `/admin/assign_category`, then — on success — updates only those cards' ribbons in place (`updateCardCategoryBadge()`) and clears just that batch's checkmarks, without reloading the list or closing the session.
4. `closeCategorySession()` (the `Done` button) is the only thing that ends the session; every `Apply` along the way already committed on its own, so there is nothing left to save on close.

### Category ribbon on thumbnails

Categorized items render a small label along the BOTTOM edge of their thumbnail (`.category-ribbon`, `templates/index.html`) — not a rotated corner ribbon, and not a top-left pill. An earlier version WAS a top-left rounded pill; it read fine for short names like "RUBY" but cramped longer ones like "P.DSGN" against the pill's own curvature. Switching to a full-width flush bar removed that width ceiling. Deliberately anchored to the bottom, not the top, to never collide with `.select-check` (always top-right).

- `CATEGORY_RIBBON_ABBREVIATIONS` (JS, `templates/index.html`) shortens category names for the ribbon (e.g. "Pearl Designer" -> "P DSGN") — a space, not a period, separates the parts; a period compresses toward invisible at real thumbnail size, a space reads as an unambiguous break. No longer a fixed literal: it's built from `design_categories_json` (the live `categories` table, injected fresh on every render by `inject_session_context()`) and rebuilt in place by `refreshCategoryData()`/`applyCategoryData()` whenever the Category Settings panel or System panel changes a category, with no page reload needed. The full, unabbreviated name is still shown via the ribbon's own `title="..."` tooltip and in the full-screen image modal.
- A category with no entry in the map falls back to its own full name — shouldn't happen since every live category always has an abbreviation (auto-generated or overridden), but fails safe rather than showing blank.
- The BURNED badge on a shared image (`_draw_category_badge()`, section 13) uses the category's FULL NAME, not the abbreviation — this is a deliberate correction (commit `6cb872e`), not the current behavior described by an earlier draft of this document. The category-editable refactor had briefly switched the burned text to `categories.abbreviation`, on the reasoning that the ribbon and badge should read the same field; in practice the width-cap/font-shrink legibility logic in `_draw_category_badge()` was built and tested against full names from the start, and the on-thumbnail `.category-ribbon` is a separate, client-side-only path unaffected by this either way — so the two are NOT the same field: ribbon = abbreviation (client-side, `CATEGORY_RIBBON_ABBREVIATIONS`), burned badge = full name (server-side, `_get_category_info_for_image()`'s `name` field, not its `abbreviation` field). Renaming or merging a category still forces a regeneration of every affected item's cached badge (`_regenerate_badges_for_stock_items()`), since the passive mtime-based staleness check has no way to notice the category's name changed underneath a cached file; an abbreviation-only override no longer needs to (it doesn't affect the badge's text at all), though the System panel's override route still triggers a regeneration anyway since that route is shared Category Settings machinery and a same-text re-burn is harmless.

### Badge cache versioning (`BADGE_FORMAT_VERSION`)

The badge cache filename (`_badged_share_cache_path()`, `app.py`) is `{image_id}_badge_v{BADGE_FORMAT_VERSION}_{category_slug}.jpg`. `BADGE_FORMAT_VERSION` is a plain integer constant (currently `2`) with no relationship to the app's own version — it exists solely so a change to `_draw_category_badge()`'s visual output (the text burned in, font, sizing/shrink behavior, padding, banner color/opacity, or position) can invalidate every previously-cached badge file at once, as a real cache miss rather than a staleness judgment call.

This was added specifically because the `6cb872e` abbreviation→name fix above was correct in the drawing code but changed nothing about the cache *key* — any badge file cached before that fix kept being served forever, since `_ensure_badged_share_cached()`'s staleness check only ever compared the source photo's mtime, never the drawing logic's own "version". Bumping `BADGE_FORMAT_VERSION` sidesteps needing to reason about that again: every existing file's key stops matching, so it's treated as missing and rebuilt fresh on next request, with no manual sweep.

Starts at `2`, not `1`, on purpose: every badge cached before this constant existed has no `_v<N>_` segment in its filename at all, so it can never collide with a real version number — that absence is already implicitly "version 1" in spirit, making `2` the first value that's actually new. `_cleanup_stale_badge_variants()` globs on `{image_id}_badge_*.jpg` (not scoped to category or version), so it also cleans up old-version files left behind by a version bump, not just old-category files left behind by a re-tag.

The System panel's Badge Cache tools (section 21: `GET /admin/system/share_cache_files`, `POST /admin/system/clear_badge_cache`) are the manual, on-demand counterpart for right now — listing real on-disk files (with mtimes, so a stale one can be confirmed against a known fix's deploy time) and a one-click sweep — for whenever an admin wants a full clear without waiting on `BADGE_FORMAT_VERSION`'s automatic, per-file invalidation.

## 23. Prioritized work queues (Needs Category / Needs Image Matching)

Two admin-only queues surface the highest-value catalog work first — cars with real, partial progress ahead of cars barely started. Both live on `templates/train.html` as tabs of one merged panel, reachable from `templates/index.html`'s single "Work Queue" More-menu entry (section 18) rather than rendering inline by default.

### Per-car completion stats

`_compute_car_completion_stats()` (`app.py`) computes, in one pass over the whole catalog (never one query per car):
- `total_items` — qty-agnostic count of every hierarchy child under the car (same scope, same non-deduplicated count, as `/api/get_all_items_for_car`)
- `image_linked_count` / `category_set_count` — how many of those items have a mapped image / an assigned category
- `fully_done_count` — how many have BOTH
- `missing_category_items` / `missing_image_items` — the actual stock item names (in hierarchy scan order) still missing a category / an image, each capped at `QUEUE_ITEM_NAME_DISPLAY_CAP` (5) as they're collected. This is the hybrid-display data the dashboard uses to show specifics without a click-through (see "Dashboard UI" below); the cap bounds the list, never the underlying counts — `fully_done_count`/`category_set_count`/`image_linked_count` above are still exact, uncapped tallies

Built from `_load_training_hierarchy_items()` (already fingerprint-cached against `main_hierarchy.json`) plus one batched `db.get_mappings_for_stock_items()` call and one batched `db.get_categories_for_stock_items()` call, chunked at 500 names per call to stay under SQLite's bound-parameter limit — never one lookup per car.

`_compute_category_completion_stats()` sums the per-car `total_items`/`category_set_count` from the function above into one catalog-wide `{total_items, categorized_items, remaining_items, percent_complete}` — the same Linked/Total/Remaining/Complete shape `db.get_mapping_stats()` already returns for images, but for categories, and driving `train.html`'s `#categoryStatsCard` (section 18). It's a pure sum of numbers the queue tiers already compute, not a separate query, so this stat can never disagree with what the queues show.

### Caching and invalidation

`QUEUE_STATS_CACHE` (section 8) is keyed on `(hierarchy file fingerprint, _QUEUE_STATS_VERSION)`. The fingerprint half follows the same convention as `HIERARCHY_JSON_CACHE`/`PRODUCT_CATEGORY_CACHE`; the version half exists because `image_linked_count`/`category_set_count` come from SQLite tables with no file of their own to fingerprint. `_invalidate_queue_stats_cache()` (a counter bump under `DATA_CACHE_LOCK`) is called from every write path that can change those counts: `assign_category()`, `_confirm_mapping_core()` (covers both `/confirm_mapping` and `/admin/upload_image`), `_apply_confirm_category_update()` (Training Mode's category picker, section 13), `remove_mapping()`, `remove_missing_images()`, and `_invalidate_runtime_caches()` (so a Full Refresh also forces a recompute).

### Tiering

- `GET /api/needs_category_queue` — cars with at least one item still missing a category, in three tiers: Tier 1 "Finish the gaps" (`fully_done_count > 0` and `< total_items` — real progress on both fronts, not yet complete), Tier 2 "Ready to tag" (every item already has an image, zero categorization started), Tier 3 (everything else). Secondary sort within each tier: remaining (uncategorized) item count descending, then car name ascending.
- `GET /api/needs_image_matching_queue` — cars with at least one item still missing an image, in THREE tiers (restored from two): Tier 1 "Finish the gaps" (the same signal, shared with the queue above), Tier 2 "Has category, needs image" (`category_set_count == total_items` but zero images linked at all — every item is tagged, none is matched yet; previously deprioritized as rare and folded into the general bucket, brought back as its own tier), Tier 3 (everything else still missing at least one image). Secondary sort within each tier: remaining (unmatched) item count descending, then car name ascending.

Both routes are `@admin_required` and return `{cars: [...], secondary_sort: "..."}`; each car entry also carries `items` (up to `QUEUE_ITEM_NAME_DISPLAY_CAP` real stock item names still missing the thing this queue tracks, read straight from `missing_category_items`/`missing_image_items` above) and `items_more` (how many beyond the cap — `_queue_item_name_fields()` derives this as `remaining - len(items)`, never a second count of its own, so it can't drift out of sync with `remaining`).

### Dashboard UI and deep-linking

`templates/train.html` — the two queues now share ONE panel (`#workQueuesGrid`, `display:none` by default) with a tab toggle (`switchQueueTab()`), not two separate side-by-side panels. Switching tabs never reloads the page; it toggles which queue's panel is visible and lazy-loads that queue's data on first view via `loadWorkQueue()`, reusing whatever a queue already fetched if you flip back to a tab you've already visited. Each visible car row renders its tier header (grouped, not interleaved), the car name, the remaining-count detail line, and — when present — a `queue-item-names` line built from that entry's `items`/`items_more` (e.g. "ALTO K10, BALENO 2022, +3 more"). `QUEUE_DISPLAY_CAP` (6, a separate constant from the backend's `QUEUE_ITEM_NAME_DISPLAY_CAP`) caps how many CAR rows show before a "Show N more" / "Show less" toggle (`toggleQueueExpanded()`) appears.

`initWorkQueuesFromUrl()` reads `?open_queue=`, strips it via `history.replaceState` (the same one-shot pattern `templates/index.html` uses for `?manage_categories=1`), and resolves it through `OPEN_QUEUE_PARAM_MAP`:
- `?open_queue=work` — the merged More-menu entry (see below); opens the panel without forcing a tab, fetches BOTH queues in parallel, and defaults to whichever has more outstanding cars (`openWorkQueuePanelWithDefaultTab()`) — a tie (including both empty) defaults to Needs Category, since it's the first tab and tends to be the faster job to clear
- `?open_queue=category` / `?open_queue=image_matching` — the older, per-queue param values; still resolve directly to a specific tab (`switchQueueTab()`) for any existing bookmarks/links, unchanged from before the tab merge

Each open panel has its own small close ("×") button (`closeQueuePanel()`); closing hides the whole panel, since this page has no other trigger to reopen one besides the URL param.

Each queue entry is a real link, not a JS click handler:
- Needs Category rows link to `/?car=<car>&manage_categories=1`. `templates/index.html`'s `maybeOpenCategorySessionFromUrl()` (called from `loadCars()`, right after `restoreCarFromUrl()` — which resolves `?car=` synchronously) opens the same continuous Manage Categories session from section 22, pre-selected to that car, and strips the param the same way.
- Needs Image Matching rows link to `/train?car=<car>` — reusing the `target_car`/`target_stock_item` mechanism the "Add Image" link (section 18) already used, just without a specific `stock_item`; `initializePage()` in `train.html` pre-fills `#carModelFilter` and loads that car's stock items whenever `targetCar` is present, whether or not a specific item came with it.

### Entry point: the More menu

`templates/index.html`'s More popover (Full Refresh / Manage Accounts / System / Category Settings, section 18) has a single `Work Queue` item below the last divider, linking to `/train?open_queue=work` — the earlier separate `Needs Category` and `Needs Image Matching` entries were merged into this one once the panel above gained an in-page tab toggle, since two menu items opening what's now one shared panel (that only shows one tab at a time anyway) was redundant. The popover's own open/close mechanics (`toggleMoreMenu()`, outside-click, Escape) are unchanged; the merged item runs through the same `handleMoreMenuAction()` every other item already used.

## 24. Important code constraints and gotchas

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
- `design_categories` assignment is last-write-wins (`upsert_design_category()`); clearing ONE item's category requires the separate `db.remove_design_category()` (added alongside the Training Mode category picker, section 13) — do not assume the only way to "remove" a category is reassigning it to a different value, that was true before `remove_design_category()` existed but isn't any more. Clearing an entire category's worth of items at once is still a different function, `delete_category()` (removes the category itself)
- any write path that changes a stock item's image-mapped or category-assigned status MUST call `_invalidate_queue_stats_cache()` (section 23), or the Needs Category / Needs Image Matching queues silently serve stale counts. Current call sites: `assign_category()`, `_confirm_mapping_core()`, `_apply_confirm_category_update()` (Training Mode's category picker), `remove_mapping()`, `remove_missing_images()` — a new mapping/category write path needs the same call added
- `.category-ribbon` (`templates/index.html`) must stay a bottom-flush full-width bar, not a top-left pill and not rotated — both were tried and produced real, confirmed-via-screenshot legibility problems (see section 22). It must also stay anchored to the bottom, not the top, or it will visually collide with `.select-check` (always top-right) whenever a categorized card is selected in Share Images mode
- the burned share-image badge (`_draw_category_badge()`) uses the category's full NAME; the on-thumbnail `.category-ribbon` uses its abbreviation. These are two different fields read by two different code paths (server-side Pillow drawing vs. client-side JS) — do not "simplify" them to share one field again, that was tried (commit prior to `6cb872e`) and produced a real, shipped regression where the badge silently started burning abbreviations. If `_draw_category_badge()`'s visual output changes in ANY way, bump `BADGE_FORMAT_VERSION` (section 22) or previously-cached badges will keep serving the old look indefinitely
- `?open_queue=` (`templates/train.html`) and `?manage_categories=1` (`templates/index.html`) are both one-shot: read once on page load, then stripped from the URL via `history.replaceState` so a later refresh or bookmark of that URL doesn't keep forcing the same panel/session open. Any new deep-link query param added to either page should follow the same read-then-strip pattern rather than leaving itself in the URL indefinitely — this includes `train.html`'s `from_add_image=1` (section 13), which is read once, stripped, and converted into a `sessionStorage` flag rather than staying in the URL
- `from_add_image=1` (the "Add Image" deep link, section 13) is deliberately a separate, explicit URL param from `car`/`stock_item`, not inferred from their presence — other deep links (the Needs Image Matching work queue) reuse those same two params without wanting the "return to home car on confirm" behavior. Don't collapse this into "car+stock_item present" as a shortcut
- every response from `/get_share_image_badged/<id>` carries `Cache-Control: no-cache, must-revalidate` — do not remove this. It's what stops a Cloudflare edge (or a browser) from independently replaying a stale badge after `BADGE_FORMAT_VERSION` was bumped or a category renamed; without it, `conditional=True`'s `ETag`/`Last-Modified` alone isn't enough because an edge cache can serve a full hit without ever asking the origin to revalidate
- `googleapiclient`'s `service` object (and its underlying `httplib2` transport) is NOT thread-safe — sharing one across worker threads corrupted the SSL connection for real (`ssl.SSLError: DECRYPTION_FAILED_OR_BAD_RECORD_MAC`), not just serialized calls. Any concurrent Drive API code must build its own service per thread (see `cloud_backup._get_thread_local_drive_service()`, section 25) from the same already-loaded credentials, never pass one `service` object into worker threads
- `car_master.json`, `main_hierarchy.json`, and `item stock list.auto.json` are written through `_atomic_json_write()` (`app.py`), not a plain `open(path, "w")` — they're read by `cloud_backup.py`'s Drive upload in the background and `item stock list.auto.json` specifically is rewritten every `TALLY_EXPORT_INTERVAL` (3 min default), so an in-place write could be read mid-truncate. On Windows specifically, a bare `os.replace()` onto a destination another thread has open for reading fails with `PermissionError` a large majority of the time (measured ~87% under a real concurrent reader) — `_atomic_json_write()` retries briefly (up to 10 attempts, short sleep between) rather than either corrupting the file or silently dropping the update; never revert this to a bare `open(path, "w")` or a non-retrying `os.replace()`
- Jinja parses `{% ... %}` and `{{ ... }}` wherever they appear in a template file, including inside an HTML `<!-- -->` comment or a JS `//`/`/* */` comment — it has no idea it's "inside a comment". Writing English prose like "(see the matching `{% if not is_admin %}` above)" inside a template comment creates a REAL, unbalanced Jinja tag and breaks the whole template with a confusing "unexpected end of template" error, often pointing at an unrelated later line. This happened for real in this codebase (fixed by rewording two comments) and was only caught by actually rendering the template, not by reading the diff — always describe a Jinja conditional in prose without the literal `{%`/`%}`/`{{`/`}}` delimiters

## 25. Cloud backup (Google Drive)

`cloud_backup.py` is a self-contained module: incremental, verified backup of `mappings.db`, the whole `S.S IMAGE` tree, and three small JSON caches (`car_master.json`, `main_hierarchy.json`, `item stock list.auto.json`) to a Google Drive folder. Entirely off (never schedules, never raises) unless all of `GDRIVE_OAUTH_CLIENT_SECRETS_PATH`/`GDRIVE_OAUTH_TOKEN_PATH`/`GDRIVE_BACKUP_FOLDER_ID` are set — same "missing = disabled" discipline as `SYSTEM_ACCESS_TOKEN`.

### Authentication

OAuth installed-app flow (`google_auth_oauthlib`), authenticating as a real, dedicated Google account — deliberately NOT a Service Account, which has zero personal Drive storage quota of its own (confirmed for real: it can create folders but every file upload fails with a 403). One-time setup: create a Desktop-app OAuth Client ID, download its JSON to `GDRIVE_OAUTH_CLIENT_SECRETS_PATH`, then click "Authorize Google Drive" on the System panel once — a browser opens for consent and the resulting token (with a refresh token) is saved to `GDRIVE_OAUTH_TOKEN_PATH`. Every run after that refreshes silently.

`authorize()` is a dedicated, standalone action (its own `AUTH_LOCK`, its own status dict, never called from `run_sync()`/`build_drive_service()`) — the one place in this module that opens a browser, hard-timed-out at 180s. This split exists because the OAuth consent flow used to live inline inside the sync path: closing the browser tab mid-consent left the background sync thread frozen forever inside an unbounded wait for the callback, with `SYNC_LOCK` held and Stop Sync unreachable (the thread never reached its cancellation checkpoints). `build_drive_service()` now only ever loads/refreshes a saved token; if there is none, it raises `NotAuthorizedError` immediately (a fast, clean failure, not a hang).

### Manifest and change detection

`data/.cloud_backup_manifest.json` is the local source of truth for what Drive is believed to hold: `relative_path -> {drive_file_id, size, mtime_ns, hash (optional, lazily filled)}`, saved atomically (temp file + `os.replace`) after every single file operation succeeds — an interrupted run never has to redo finished work.

`classify_changes()` uses a cheap `(size, mtime_ns)` stat comparison per file, falling back to a real SHA-256 content hash only for the ambiguous case (same size, different mtime — e.g. a touch/copy that preserved size); the hash result is cached back into the manifest so the same touched-but-identical file is never re-hashed on a later run.

### Self-healing against manifest/Drive drift

`_create_folder_self_healing()` / `_create_file_self_healing()` catch a `404 HttpError` from Drive on a `create` call whose `parents=[id]` references a manifest-cached folder id that was since deleted directly on Drive (by hand, or by some other process) — they drop the stale cache entry, resolve the parent fresh (recursing further up if the drift goes deeper than one folder), and retry once. `_upload_or_update_file()` does the equivalent for a stale cached FILE id (a Drive `update` 404 falls through to a fresh `create` instead of failing the item outright).

### Concurrency, cancellation, progress

- `SYNC_LOCK` — acquire-in-caller / release-in-`finally`, same convention as `FULL_REFRESH_LOCK`/`EXPORT_LOCK` in `app.py`. Both the scheduled job and the System panel's "Sync Now" share this, so "is a sync already running" is one atomic acquire, not a check-then-act race.
- `_cancel_event` — cooperative "Stop Sync": checked before starting each new file/folder operation, never mid-write, so a stopped run's manifest reflects only genuinely completed items and the next run resumes the rest normally.
- `_progress` — an in-memory dict (current item, counts, a capped 25-line action log) polled by the System panel every 1.5s while a sync runs, same lightweight shape as `full_refresh_status`. Set to `running: True` at the very start of `run_sync()`, before the (potentially multi-second, thousands-of-files) change-detection scan — not just when the file loops actually start — so a Stop Sync click during that scan is never silently dropped.
- Every per-file failure is caught individually (`logger.exception` for the real traceback, plus a one-line UI-safe rendering via `_short_error_text()` folded into both the live progress log and the run summary's `error` string) and the loop continues — one bad file never aborts the whole run.

### Phase timing

`run_sync()` times its three real phases separately — change detection (local stat/hash work), apply (the actual Drive API calls for whatever changed), verification (see below) — logging each and storing them on the run summary as `phase_timing: {change_detection_seconds, apply_seconds, verification_seconds}`. Added after a real production report of a 3-minute sync for only 3 changed files; measured against a real, disposable 299-folder Drive structure (matching this machine's real local `S.S IMAGE` folder count), the sequential verification listing alone took 115.88s of that — confirming verification, not the handful of actual uploads, is what dominates a mostly-unchanged run.

### Verification

`_verify()` does a fresh recursive `files.list` against the real Drive folder after every non-cancelled run and compares file count + total bytes against the manifest — a genuine "did the backup actually work" check, not just "did the API calls return 200". A mismatch sets the run's status to `"partial"` and is logged as a warning with the real numbers.

The listing itself (`_list_drive_files_recursive()`) is concurrent, bounded to `LIST_CONCURRENCY` (15) folders in flight at once — every folder is still listed exactly once and every file still counted exactly once, only the dispatch is concurrent. Each worker thread builds its OWN `googleapiclient` service via `_get_thread_local_drive_service()` (a `threading.local()` cache) rather than sharing one `service` object across threads: a naive shared-service version was found for real to corrupt the SSL connection (`ssl.SSLError: DECRYPTION_FAILED_OR_BAD_RECORD_MAC`) under concurrent use — `googleapiclient`'s httplib2 transport is not thread-safe. `MIN_CALL_INTERVAL_SECONDS` (the existing 0.15s pacing floor, unchanged) still applies globally across all worker threads, so concurrency only overlaps each call's network wait time, it never loosens the dispatch-rate safety margin. Measured real speedup against the same 299-folder structure: 115.88s sequential -> 47.12s concurrent (2.46x), file count/byte total identical both times — close to the theoretical floor for 300 calls at that pacing rate (~45s), so this is near the practical ceiling without loosening the rate limit itself.

### Status persistence

`_status["last_run"]` (the last run's full summary, including verification and phase timing) and `_skipped_runs` (missed/errored scheduled runs — see below) are both persisted to `data/.cloud_backup_status.json` (same temp-file + `os.replace` pattern as the manifest), loaded once at module import. Before this, both were in-memory only: restarting the app (a System panel restart, a crash, a reboot) wiped the last-run summary and the skip history from the System panel entirely, even though the real backup was completely untouched — which looked like "the cloud backup data disappeared." `_progress` (the live, in-flight sync state) is deliberately still not persisted; "not running" is exactly the right default after a restart.

### Scheduling

`schedule()` is a self-rescheduling `threading.Timer`, same pattern as `app.py`'s `schedule_item_export()`. First run 120s after startup (`app.py`'s `cloud_backup.schedule(initial_delay=120)`), then every `CLOUD_BACKUP_INTERVAL` seconds (default 3 days; the real deployment currently runs this at `86400` / daily) measured from when each run FINISHES, not a fixed clock time — so the actual time-of-day drifts a little each cycle and resets on every restart. A scheduled run that finds `SYNC_LOCK` already held, or finds the account not yet authorized, skips itself and calls `_record_skipped_run(reason, message)` rather than blocking or queuing — surfaced on the System panel (`_status_lock`-independent from `_status["last_run"]`, so a skip never overwrites the previous real run's result with silence).

### System panel routes

`/admin/system/cloud_backup/status` (poll), `/authorize` + `/auth_status` (the dedicated one-time OAuth action above), `/sync_now` (400 if not yet authorized), `/stop`. All under the same admin + device-pairing gate as the rest of `/admin/system/*`.

## 26. Cloud deployment (push-based sync to a Render instance)

A cloud-hosted instance can never reach Tally directly. Rather than have it attempt and fail doomed direct calls, it receives car master / hierarchy / item stock data PUSHED to it from the office PC after each local export succeeds. Every piece of this defaults to fully off — an install that never sets these env vars behaves exactly as it did before this feature existed.

### On the cloud instance

`Config.DISABLE_TALLY_SCHEDULING` (env `DISABLE_TALLY_SCHEDULING=1`) makes `start_background_startup_tasks()` skip the startup full refresh and the item-export scheduling entirely (logs a clear skip message instead) — `cloud_backup.schedule()` itself is NOT gated by this flag, since Google Drive backup is orthogonal to whether this instance talks to Tally. `_refresh_stock_data()` and `POST /full_refresh` both short-circuit with a clear `{"ok": false, "cloud_mode": true, "message": "..."}` (`full_refresh` returns `403`) instead of attempting a Tally call that can only ever fail on this instance; `templates/index.html`'s `refreshStock()` checks `data.cloud_mode` and shows that message directly rather than a generic error.

`POST /admin/intake/sync_data` is the receiving end — deliberately in `PUBLIC_ENDPOINTS` (not behind the session-based login gate) since it's called by a background job on another machine, not a paired browser, with its own independent check: `X-Intake-Token` header compared via `hmac.compare_digest()` against `Config.INTAKE_SYNC_TOKEN` (empty token = route stays `403` for everyone, same "missing secret = disabled" pattern as everywhere else). On a valid token, it writes the pushed `car_master`/`main_hierarchy`/`item_stock` payload through the exact same save functions a local Tally export already uses (`save_car_master_to_file`, `save_main_hierarchy_to_file`, `_save_item_stock_data`) and calls `load_data(refresh_first=False)` to reload in-memory state — so a cloud instance's data is indistinguishable, once pushed, from data it fetched itself.

### On the office PC

`_push_data_to_cloud()` no-ops unless both `Config.CLOUD_SYNC_URL` and `Config.CLOUD_SYNC_TOKEN` are set. When configured, it reads the same local files a full refresh / item export just wrote (never re-derives anything) and `POST`s them to `CLOUD_SYNC_URL` with the token in `X-Intake-Token`, on a background thread so a slow/failed push never delays the local job that triggered it. Called from both `run_full_refresh_job()` and `schedule_item_export()`'s export job, after each succeeds. Wrapped in a broad try/except that only logs — a push failure is completely invisible to the local operator-facing flow.

## 27. Installable PWA (customer sessions only)

`static/manifest.json`, `static/sw.js`, and three icon PNGs (`static/icons/`, generated from `static/logo.jpg`) make the customer-facing view installable. Every piece of this is scoped to customer sessions — an admin session renders none of it.

### Scoping

`templates/index.html`'s `<head>` wraps the manifest `<link>`, the Apple meta tags (`apple-mobile-web-app-capable`, `apple-touch-icon`, etc.), and theme-color in `{% if not is_admin %}` — the same context-processor-injected flag (`inject_session_context()`, section 7) used throughout this file for role-based content. The service worker registration script (a separate, small `<script>` block right before `</body>`) is wrapped the same way. Verified with a real rendered-output check (Flask test client, both roles): zero PWA tags in the admin render, all present in the customer render.

Caveat: a service worker's scope is per-origin, not per Flask session — `fetch` events don't expose the session cookie, so the SW itself has no reliable way to tell which role is browsing on a later request. Registration only ever happens for a customer, but a browser that later logs in as admin on the same device would still have an already-registered SW active. Mitigated by design, not by role-detection: `sw.js` is network-first for everything except six genuinely static files, so it's safe regardless of who it ends up serving.

### Icons

Generated from `static/logo.jpg` (447x447, already square) at 192px and 512px (manifest) plus 180px (`apple-touch-icon.png`). The logo's real content bounding box was measured before choosing padding (not eyeballed): it already sits at ~67% of canvas width, right at the safe-zone ratio Android's adaptive icon mask wants, so no extra padding was added. Kept the logo's own light cream background (solid, not transparent) rather than switching to brand blue — the logo isn't designed for a blue background and nothing else in the app pairs red-on-blue.

### Service worker (`static/sw.js`)

Network-first for everything except `STATIC_ASSETS` (`shared.css`, `shared.js`, `manifest.json`, the three icons) — those alone are cache-first, since none of them carry live business data. Every other GET (HTML navigations, `/designs`, `/cars`, `/get_stock_image`, `/update_stock`, etc.) always tries the network first; only a genuine network failure falls back, and only to a clearly-labeled "You're offline" page for a navigation (a small JSON `{"ok": false, "offline": true}` for anything else) — never to a silently-stale cached response standing in for current stock data. Deliberately no whole-app precaching or offline catalog browsing.

### Home screen shortcuts

`manifest.json`'s `shortcuts` array has one entry, deep-linking to `/?section=contact-us`. `templates/index.html`'s `maybeScrollToSectionFromUrl()` reads it once on load, strips it (`history.replaceState`, same one-shot pattern as `?manage_categories=1` and `?open_queue=`, section 18/23), and scrolls to `#contactUsSection` if the session is a customer.

## 28. Persistent customer login

Customer sessions get a 90-day cookie instead of the standard `SESSION_TIMEOUT_HOURS` (8h); admin sessions are completely unaffected.

Implemented as a custom `flask.sessions.SecureCookieSessionInterface` subclass (`_RoleAwareSessionInterface`, `app.py`, installed via `app.session_interface = _RoleAwareSessionInterface()`) whose `get_expiration_time()` checks `session.get("role")`: `"customer"` gets `now + CUSTOMER_SESSION_LIFETIME` (90 days, if `session.permanent` — which `require_login()`'s `before_request` hook already sets unconditionally on every request), anything else falls through to `super().get_expiration_time()` — Flask's own stock behavior, reading `app.permanent_session_lifetime` exactly as before this existed.

A simpler-looking alternative — temporarily overwriting `app.config["PERMANENT_SESSION_LIFETIME"]` for the duration of a customer's login request — was considered and rejected: waitress serves requests from a thread pool, so that shared, mutable app-config value would race against any OTHER request being handled concurrently on a different thread while the override was in effect. Overriding `get_expiration_time()` instead reads the role off the one session object actually being saved; no shared state, no race, and admin's path is byte-for-byte the same code Flask always ran.

## 29. Customer notices and "Flag this" reports

**Notices.** Two independent slots in `notice_slots` (`text`, `image`), each with an ever-increasing `version` that survives a clear. Admins manage them from More > Manage Notice; only the text slot can be "important". `/api/notice` returns both slots plus a composite version (`t<text>-i<image>`) to customer sessions only (admin gets nothing). The customer page shows one combined popup (text above image); its dismissal is stored in `localStorage` against the composite version, so a change to either slot shows it again. An important text notice also gets a scrolling top banner with no close control: it slides away once the customer scrolls past 80px and returns only when they are back within 10px of the top (visibility only; the sticky toolbar's `top` follows `--notice-banner-h`). Images live in `data/notice_images/`.

**Report button.** Customers get one button below a car's design list (shown only if that car has an incomplete design) that POSTs `/api/report_item` with just the car. The server works out which designs lack an image or category, stores a summary, and a partial unique index on unresolved rows (`customer_reports`) keeps it to one open report per car. Admins see open reports in the Work Queue's "Customer Reports" tab (`/api/customer_reports`) and mark them resolved.

## 30. File map for maintenance

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
- material-tier category assignment: `app.py` (`/admin/assign_category`, `_build_design_payload`, `_apply_confirm_category_update`), `database.py` (`design_categories`, `upsert_design_category`, `remove_design_category`, `get_categories_for_stock_items`), `templates/index.html` (Manage Categories session, `.category-ribbon`), `templates/train.html` (category picker: `#matchCategorySelect`, `#uploadCategorySelect`)
- badge cache versioning/tools: `app.py` (`BADGE_FORMAT_VERSION`, `_badged_share_cache_path`, `_cleanup_stale_badge_variants`, `/admin/system/share_cache_files`, `/admin/system/clear_badge_cache`), `templates/system.html` (Badge Cache panel)
- Needs Category / Needs Image Matching work queue: `app.py` (`_compute_car_completion_stats`, `_compute_category_completion_stats`, `/api/needs_category_queue`, `/api/needs_image_matching_queue`, `_invalidate_queue_stats_cache`), `templates/train.html` (merged queue panel/tabs, `initWorkQueuesFromUrl`, `switchQueueTab`), `templates/index.html` (single "Work Queue" More menu entry, `maybeOpenCategorySessionFromUrl`)
- security headers / robots.txt: `app.py` (`add_security_headers`, `robots_txt`, `PUBLIC_ENDPOINTS`)
- return-to-home-car after Add Image confirm: `templates/index.html` (`addImageUrl`'s `from_add_image=1`), `templates/train.html` (`maybeStoreReturnToHomeCarFromUrl`, `clearPendingReturnToHomeCar`, `submitMapping`)
- cloud backup (Google Drive): `cloud_backup.py` (whole module), `config.py` (`GDRIVE_*`, `CLOUD_BACKUP_INTERVAL`), `app.py` (`/admin/system/cloud_backup/*` routes, `_cloud_backup_sync_running`), `templates/system.html` (Cloud Backup panel)
- cloud deployment (push-based sync): `config.py` (`DISABLE_TALLY_SCHEDULING`, `INTAKE_SYNC_TOKEN`, `CLOUD_SYNC_URL`, `CLOUD_SYNC_TOKEN`), `app.py` (`_push_data_to_cloud`, `/admin/intake/sync_data`, cloud-mode checks in `_refresh_stock_data`/`full_refresh`), `templates/index.html` (`refreshStock()`'s `cloud_mode` handling)
- Render/PaaS hosting: `serve.py` (`PORT`, host binding, diagnostic prints), `app.py` (`_configure_logging`'s console handler, `"health"` in `PUBLIC_ENDPOINTS`)
- installable PWA: `static/manifest.json`, `static/sw.js`, `static/icons/`, `templates/index.html` (customer-only `<head>` tags and SW registration, `maybeScrollToSectionFromUrl`)
- persistent customer login: `app.py` (`_RoleAwareSessionInterface`, `CUSTOMER_SESSION_LIFETIME`)
