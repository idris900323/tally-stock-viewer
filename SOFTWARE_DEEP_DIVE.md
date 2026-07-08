# Tally Stock Viewer - Software Deep Dive

This document describes the current codebase as it exists in this repository.
It is intended for technical maintenance, not office staff operations.

## 1. System purpose

The application is a Windows-hosted Flask system for:
- reading stock and car-group data from Tally
- matching stock items to car models
- mapping product images to stock items
- showing images and prices to admin and customer users
- running unattended on an office PC through a tray launcher

## 2. Runtime architecture

The runtime has four main layers:

### Web layer
- `app.py` defines the Flask app, routes, background tasks, and in-memory caches
- `templates/` contains the admin and customer-facing HTML screens
- `api/search.py` provides Select2-style search endpoints

### Data layer
- `database.py` manages SQLite schema and queries
- `data/mappings.db` stores images, mappings, users, prices, and account logs

### Tally integration layer
- `tally/sync.py` performs HTTP POST retries to Tally
- `app.py` builds XML requests, parses Tally XML, and writes local stock caches

### Windows hosting layer
- `serve.py` runs the Flask app under `waitress`
- `launcher.pyw` runs in the tray, starts the server, monitors it, and optionally starts `cloudflared.exe`
- `first_time_setup.bat`, `setup_office_pc.bat`, `update_app.bat`, and `stop_server.py` support deployment and operations

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
- fallback alternates such as `data/item stock list.xlsx`

The JSON cache is used for fast quantity lookup.
The Excel cache is used as a saved local stock export and fallback artifact.

## 6. SQLite schema and responsibilities

`database.py` creates and maintains these tables:

- `images`
  - one row per scanned image file
- `mappings`
  - links an image to a stock item and stores confidence
- `folder_car_mapping`
  - remembers folder-to-car hints learned from confirmed mappings
- `users`
  - admin and customer accounts
- `account_logs`
  - audit-style account actions
- `base_prices`
  - global prices per stock item
- `customer_prices`
  - customer-specific price overrides

Notable behavior:
- scanned file paths are validated and normalized
- legacy absolute image paths are migrated to portable relative paths on every startup; if a relative-path row for the same file already exists, the legacy row is merged into it instead of renamed (keeping whichever confirmed mapping has the more recent `created_at`) and the legacy row is deleted
- default seed users are inserted only when the `users` table is empty
- `remove_mappings_for_stock_item(stock_item_name, exclude_image_id=None)` deletes any mapping row(s) for a given stock item, optionally excluding one image; `confirm_mapping` in `app.py` calls this before every save so a stock item only ever has one image mapped to it (see section 13)

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
- `@admin_required` protects admin-only endpoints such as pricing, accounts, mapping changes, and stock refresh

### Roles
- `admin`
  - full access
- `customer`
  - read-only browsing with customer-specific pricing behavior

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

### Full refresh

`run_full_refresh_job()` is the shared implementation behind every "full refresh" (car master + main hierarchy + item stock, in that order, followed by `load_data()`). It updates the module-level `full_refresh_status` dict as it moves through stages (`car_master` -> `main_hierarchy` -> `item_stock` -> `reloading` -> `done`, or `error`), guarded end-to-end by `FULL_REFRESH_LOCK`.

Two callers share this function without duplicating logic:
- `POST /full_refresh` — the manual admin button; acquires the lock (non-blocking, `409` if already running), runs the job in a background thread, returns `202` immediately
- the automatic startup job described in section 10 — runs once, ~45 seconds after the app starts

`GET /full_refresh_status` polls the same `full_refresh_status` dict regardless of which caller started the job, so the frontend progress bar behaves identically whether a human clicked the button or the startup job triggered it.

### What the refresh actually asks from Tally

The code performs two main export requests:
- stock item master names
- stock summary rows, both regular and detailed

### Filtering logic

The refresh logic intentionally filters Tally data in layers:
- keep names that exist in the stock item master
- subtract obvious group rows seen in summary data
- optionally align rows with names present in the local hierarchy file
- if filtering becomes too strict, fall back to detailed rows

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
- schedules item stock export if auto-export is enabled
- schedules car master refresh

The startup scan used to be skipped whenever the `images` table already had rows, which meant it only ever ran once, the very first time the database was populated — restarting the app afterward never picked up new files added to `S.S IMAGE`. It now always re-scans on startup; the scan is an upsert (`add_images_batch`), so re-scanning unchanged files is a cheap no-op.

`start_background_startup_tasks()` also starts a second, independent daemon thread that runs one automatic full refresh (car master + main hierarchy + item stock via `run_full_refresh_job()`, see section 9) shortly after startup:
- it sleeps 45 seconds first, to give Tally and the app time to be ready
- it then tries to acquire `FULL_REFRESH_LOCK` (non-blocking); if the lock is already held (e.g. someone clicked the manual button first) it logs and skips instead of waiting or queuing
- if Tally is not reachable yet at the 45-second mark, `run_full_refresh_job()`'s own error handling catches it, logs a warning, and updates `full_refresh_status` to `error` — it does not crash the app or block startup
- this runs exactly once per app start; it is not a repeating timer, and the manual `Full Refresh` button remains the way to trigger it again later

### Timers
- item export default interval: from `TALLY_EXPORT_INTERVAL`, default `180` seconds
- car master refresh interval: `86400` seconds
- automatic startup full refresh: one-time, 45 seconds after `start_background_startup_tasks()` runs

### Car master refresh

`fetch_car_master_from_tally()` requests the Stock Groups collection from Tally using the `List of Stock Groups` export ID. It extracts all `STOCKGROUP` name attributes from the XML response, filters out empty names, and returns a sorted list. `save_car_master_to_file()` writes that list to `data/car master list.xls` via a temp file and then reloads the in-memory caches. If Tally is unreachable the existing file is left unchanged.

## 11. Car and design matching model

The app does not load designs by a strict normalized relational model.
Instead, it builds a token-based mapping between car names and design rows.

### Car list source

`data/car master list.xls` is read first.
It becomes the dropdown source shown in the UI.

### Design list source

`parse_flat_tally()` reads the current main hierarchy file as a flat list of `{design, raw, qty}` records.

### Matching strategy

For each car:
- the code extracts a base car name with `extract_car_base_name()`
- it tokenizes that base name
- it matches tokens against tokenized design rows
- matching rows are stored in `CAR_DESIGN_MAP[car]`

This is intentionally heuristic, not schema-driven.

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

## 13. Mapping workflow

The mapping workflow lives mostly in `app.py`, `database.py`, `matcher.py`, and `templates/train.html`.

### Core admin routes
- `GET /train`
- `GET /get_unmapped_images`
- `GET /train_images`
- `POST /confirm_mapping`
- `POST /remove_mapping`
- `POST /scan_images`
- `POST /admin/upload_image` — lets an admin upload a photo straight from the `Train Matches` page instead of pre-copying it into `S.S IMAGE\` and rescanning; validates extension (`Config.ALLOWED_IMAGE_EXTENSIONS`) and size (`Config.MAX_IMAGE_SIZE`), saves it under `data/S.S IMAGE/<car_folder>/` (de-duplicating the filename with `_unique_filename_in_dir()` if one already exists), inserts an `images` row via `db.add_image()`, then confirms the mapping to the selected stock item through the same `_confirm_mapping_core()` helper used by `/confirm_mapping`
- `GET /mapping_stats`
- `GET /get_current_mapping_image?stock_item=<name>` — used by the `train.html` "Currently Matched Image" preview; looks up `db.get_mapping_for_stock_item()` and returns `{has_mapping, image_id, image_url, confidence}` as JSON so the admin can visually compare the existing match against the new image before confirming

### Mapping save behavior

The save logic is factored into `_confirm_mapping_core(image_id, stock_item_name, car_model, confidence, confirmed_by)` in `app.py`, shared by both `/confirm_mapping` and `/admin/upload_image` so a manually-confirmed match and a freshly-uploaded-and-matched image go through identical save/overwrite behavior.

When an admin confirms a mapping:
- the image row is looked up
- any other image currently mapped to the same `stock_item_name` is deleted first via `db.remove_mappings_for_stock_item(stock_item_name, exclude_image_id=image_id)`, so a stock item is only ever mapped to one image at a time (blank and `__UNMATCHABLE__` values are skipped, matching the existing folder-mapping guard just below it)
- the selected stock item is saved to `mappings`
- the car model hint is resolved
- high-confidence confirmed mappings also update `folder_car_mapping`

Before this, `mappings` only enforced a UNIQUE constraint on `image_id`, so two images could independently end up mapped to the same stock item — `get_mapping_for_stock_item()` would just return whichever was most recently confirmed while the older mapping sat unused in the table. The explicit delete-then-insert step closes that gap.

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

## 15. Pricing and customer access

The pricing model has two levels:
- base price for everyone
- customer-specific override price

Price resolution flow:
1. gather all visible stock item names
2. fetch base prices
3. fetch customer-specific overrides if a customer is logged in
4. if no price exists, return `Contact Us`
5. if `force_contact_us` is set for the customer, force all visible items to `Contact Us`

Admin UI routes:
- `GET /admin/pricing`
- `GET /admin/pricing_data`
- `POST /admin/save_price`
- `POST /admin/toggle_contact_us`

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
  - admin-only update, training, pricing, and account links
  - `checkTimestampFreshness()` marks the "Last updated" text with the `.stale-timestamp` class (red, bold) whenever it is more than 180 seconds old; it runs after every timestamp update and on a 10-second `setInterval`, so it turns red live even if no new data arrives (e.g. Tally down for a while)
- `templates/train.html`
  - image mapping workflow
  - the "Currently Matched Image" preview (`#currentMatchImg`) is 280px on desktop / 200px on narrow screens (`@media (max-width: 640px)`), sized via CSS id rules rather than inline `max-width`/`max-height` so the mobile override can apply
  - admin-only `Upload Image` button opens a modal to pick a car folder and upload a photo straight to `POST /admin/upload_image`, skipping the manual copy-then-rescan flow
- `templates/pricing.html`
  - pricing management
- `templates/accounts.html`
  - customer account management
  - accounts table adds Access Code, Status (Active/Paused), and Last Login columns, plus per-row Pause/Resume and bulk `Resume All`/`Pause All` controls

`templates/train.html` and `templates/accounts.html` share the same sticky topbar pattern (`.topbar` > `.topbar-left` / `.topbar-right`, `.link-button` for navigation, `.role-indicator` for the current role label) for visual consistency across admin screens; `templates/index.html` still uses the older `.role-badge` topbar style.

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

### `setup_office_pc.bat`

Older in-place setup helper.
It is simpler than `first_time_setup.bat` and does not replace the new-machine flow.

### `update_app.bat`

Operational update path for Git-connected installs:
- stop current app
- `git pull origin main`
- restart `launcher.pyw`

### `stop_server.py`

Manual emergency stop helper for the server and tunnel PID files.

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

## 21. Important code constraints and gotchas

- `first_time_setup.bat` always installs into `C:\tally_stock`
- the app assumes Windows and uses Windows-specific launcher behavior
- the health endpoint does not prove Tally is online
- the current search and design matching logic is heuristic and token-based
- seeded credentials still exist if the database has not been hardened
- public tunnel startup is conditional on `cloudflared.exe` being present in the app root
- the generated Desktop stop shortcut only kills the server PID; the full clean shutdown path is the tray menu item `Stop & Exit`
- `templates/train.html` exposes an admin-only `Rescan Images` button that calls `POST /scan_images` directly; it also runs automatically on every app startup
- a full refresh (car master + main hierarchy + item stock) also runs automatically once, ~45 seconds after every app startup, sharing `run_full_refresh_job()` with the manual `Full Refresh` button; it silently skips if a refresh is already running and fails gracefully (logged, `full_refresh_status` set to `error`) if Tally isn't reachable yet — it never blocks startup or crashes the app
- each stock item can only be mapped to one image at a time; confirming a new image against a stock item that's already mapped elsewhere silently deletes that other mapping first (`db.remove_mappings_for_stock_item`)
- `/admin/upload_image` sanitizes `car_folder` against path separators and `..` (`_sanitize_upload_car_folder()`) and the filename against directory components (`_sanitize_upload_filename()`) before touching the filesystem — do not bypass these when adding new upload entry points
- in `templates/train.html`, `#stockItemSelect` is a plain `<select>` (not Select2); its `change` event only fires on real user interaction. Any code path that sets `select.value` programmatically (auto-matching by filename/car folder, restoring a preferred stock item, or leaving the browser's default first-option selection in place) must explicitly call `handleStockItemSelection()` afterward, or the "Currently Matched Image" preview silently stays out of sync until the user manually changes the dropdown — this was the root cause of a bug where the preview only appeared from the second selection onward

## 22. File map for maintenance

If you need to change a behavior, start here:

- Tally refresh/export: `app.py`, `tally/sync.py`
- SQLite schema or user/pricing logic: `database.py`
- image scan behavior: `image_scanner.py`
- matching heuristics: `matcher.py`
- login/session settings: `config.py`, `app.py`
- tray startup and tunnel behavior: `launcher.pyw`, `serve.py`
- admin and customer UI: `templates/`
- install/update scripts: `first_time_setup.bat`, `setup_office_pc.bat`, `update_app.bat`
