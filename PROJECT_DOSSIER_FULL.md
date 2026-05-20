# Project Dossier: Tally Stock Viewer
Generated: 2026-05-20 13:10:00 +05:30

## 1) Purpose and Scope
This dossier is a comprehensive operational record for the current tally_test workspace, including:
- full project structure references
- current codebase state
- implemented features and behavior
- timeline of user requests and applied changes
- mistakes/regressions encountered and how they were corrected
- verification notes and current risks

This file is paired with machine-generated appendices to satisfy full-detail coverage:
- PROJECT_FILE_MANIFEST.txt (all files with size + modified time)
- PROJECT_FOLDER_MANIFEST.txt (all directories)
- PROJECT_CODE_SNAPSHOT.md (full source snapshot for key app/template files)

## 2) Workspace Snapshot
- Root: C:\Users\idirs\Desktop\tally_test
- Total files: 14,023
- Total directories: 1,236
- Total size: 1,490,713,469 bytes (~1.39 GB)

Top-level items:
- .venv/
- .vscode/
- archived_unneeded/
- data/
- logs/
- templates/
- __pycache__/
- .dockerignore
- .gitignore
- app.py
- config.py
- database.py
- image_scanner.py
- matcher.py
- PROJECT_DOSSIER_FULL.md
- PROJECT_STATUS.md
- SOLUTION.md
- Tally_Stock_Viewer_Solution_v2.md
- test_tally.py
- PROJECT_FILE_MANIFEST.txt
- PROJECT_FOLDER_MANIFEST.txt
- PROJECT_CODE_SNAPSHOT.md

## 3) Code Footprint (Current)
- app.py: 1,568 lines, 53,621 bytes
- database.py: 839 lines, 27,056 bytes
- config.py: 46 lines, 1,957 bytes
- image_scanner.py: 79 lines, 2,599 bytes
- matcher.py: 149 lines, 4,855 bytes
- templates/index.html: 845 lines, 25,933 bytes
- templates/train.html: 951 lines, 30,159 bytes
- templates/login.html: 168 lines, 4,138 bytes
- templates/pricing.html: 456 lines, 14,623 bytes

## 4) Full Inventory and Code Appendices
1. Folder inventory (recursive): PROJECT_FOLDER_MANIFEST.txt
2. File inventory (recursive, with bytes + timestamp): PROJECT_FILE_MANIFEST.txt
3. Code snapshot (full contents): PROJECT_CODE_SNAPSHOT.md

## 5) Architecture Overview
### Backend
- Framework: Flask
- Persistence: SQLite (data/mappings.db)
- Data ingestion: Tally XML polling + local Excel files
- Image indexing: filesystem scan into images table
- Mapping workflow: admin labeling of image to stock item

### Frontend templates
- index.html: viewer/customer/admin browse page
- train.html: mapping/training interface
- login.html: username + access code login
- pricing.html: admin pricing/customer management dashboard

### Data flow (high-level)
1. Car models and stock rows loaded from Excel exports.
2. Images scanned/indexed into SQLite.
3. /designs returns stock items, mapping status, thumbnail URL, and computed price.
4. Admin can map images and manage pricing/customer accounts.

## 6) API Endpoints (Current)
Defined routes in app.py:
- GET|POST /login
- GET /logout
- GET /
- GET /health
- GET /cars
- GET /designs
- GET /admin/pricing
- GET /admin/pricing_data
- POST /admin/create_user
- GET /admin/get_all_customers
- POST /admin/delete_user/<int:user_id>
- POST /admin/toggle_user_status/<int:user_id>
- POST /admin/set_all_customer_status
- POST /admin/save_price
- POST /admin/toggle_contact_us
- GET /train
- GET /get_unmapped_images
- GET /train_images
- POST /export_images
- POST /confirm_mapping
- GET /get_image/<id>
- GET /get_stock_image
- GET /suggest_match/<id>
- POST /scan_images
- GET /mapping_stats
- POST /reload
- POST /refresh_item_stock
- GET /last_update
- GET /refresh_status
- POST /refresh_stock

Defined routes in api/search.py:
- GET /api/search_cars
- GET /api/search_car_folders
- GET /api/search_customers
- GET /api/get_stock_items_for_car

## 7) Database Schema (Current)
### Core mapping tables
- images(id, car_folder, filename, filepath UNIQUE, scan_date)
- mappings(id, image_id UNIQUE, stock_item_name, car_model, confidence, confirmed_by, created_at)
- folder_car_mapping(id, folder_name UNIQUE, car_model_name, created_at)

### Auth and pricing tables
- users(id, username UNIQUE, access_code, role in [admin, customer], force_contact_us, created_at)
- base_prices(stock_item_name UNIQUE/PK, price TEXT)
- customer_prices(id, user_id, stock_item_name, price, UNIQUE(user_id, stock_item_name))
- account_logs(id, user_id, action, performed_by, timestamp)

### Seeded accounts
- admin / idris123 / admin
- star / 111 / customer
- jeewajee / 222 / customer

## 8) Key Behavior Rules (Current)
### Authentication
- Login via DB-backed credentials (/login)
- Session stores user_id, username, role
- Roles: admin, customer

### Pricing cascade for /designs
For each stock item and logged-in user:
1. If force_contact_us is true for user -> Contact Us
2. Else if user custom price exists -> custom price
3. Else if base price exists -> base price
4. Else -> Contact Us

### Mapping stats semantics
- mapped_images: only non-empty, non-__UNMATCHABLE__ mappings
- processed_images: all rows in mappings table
- unmapped_images: total_images - processed_images
- percent_complete: based on processed_images / total_images

## 9) Chronological Change Log (Recent)
### A) Performance, RAM, and complexity hardening
- Added caching and lookup optimizations for designs and stock resolution paths.
- Added response limit guards for image endpoints.
- Added SQLite tuning/index improvements.
- Added deployment ignores (.dockerignore, expanded .gitignore).

### B) Duplicate image dropdown issue fix
- Root cause: same physical file indexed with path-case variations (for example C:\... vs c:\...).
- Fixes:
  - filepath canonicalization before insert
  - SQL dedupe by canonicalized/LOWER(filepath) at query level
- Result: duplicate entries removed from train queue dropdown.

### C) Auth + pricing engine rollout
- Added users, base_prices, customer_prices schema.
- Replaced role-dropdown login with username/access-code login.
- Added customer pricing cascade in /designs payload.
- Added admin pricing dashboard with save/toggle endpoints.

### D) UI cleanup and sharing updates
- train.html copy controls removed per request.
- index.html changed from ZIP export to Web Share-based image sharing with per-image download fallback.
- Sharing restricted to admin-side UI.

### E) Admin pricing dashboard enhancement
- Added Create New Customer Account section and backend route.
- Added Global (Base Prices) mode in customer selector.
- Added /admin/pricing_data API to return both base and customer prices.
- In customer mode, UI now shows read-only default/base price next to custom price input.

### F) Dropdown/search/timestamp refinements and Tally update UX
- Migrated major dropdowns to Select2 AJAX behavior with `minimumInputLength: 0` for open-and-browse plus type-to-search.
- Added immediate search focus on dropdown open for one-click typing.
- Added SS Car Queue search on training page to avoid long manual scroll.
- Added explicit Select2 arrow styling and open/closed arrow state in training/viewer UI.
- Replaced relative "Just now" time wording with fixed `DD/MM/YYYY HH:MM:SS` display.
- Extended stock update response payload with ISO + formatted timestamps.

### G) Exact car-variant stock filtering improvements
- Enhanced `get_stock_items_for_training` to prefer exact car model matches first.
- Added stricter variant-aware filtering using year and variant identifiers before broad fallback.
- Kept safe fallback behavior when exact metadata is unavailable to avoid empty-result dead ends.

### H) Account management and loop fix (2026-05-20)
- Root cause of pricing-page loop: form POST to `/admin/create_user` followed by redirect and selector-driven page navigation interactions.
- Fixed by changing `/admin/create_user` to JSON-only success/error responses (no redirect on create).
- Refactored `pricing.html` account creation to explicit AJAX submit with button disable while in flight.
- Added guard to suppress programmatic customer Select2 value changes from triggering redundant redirects.
- Added account management table with visible status (Active/Paused), created time, and actions.
- Added endpoints and UI actions for:
  - Delete account
  - Pause/Resume account access
  - Pause all / Resume all accounts
- Added account activity persistence via `account_logs` and logging on create/delete/pause/resume actions.

### I) Runtime startup regression and fix
- Regression encountered after schema expansion: `sqlite3.OperationalError: no such column: created_at`.
- Root cause: index on `users(created_at)` created before schema-migration helper ensured column existence.
- Fix: run `_ensure_users_schema(conn)` before `idx_users_created_at` creation in `init_database()`.
- Validation: application startup succeeds after fix; data loading and export scheduler initialization proceed normally.

## 10) Conversation and Request History Summary
Major user intents handled across recent sessions:
1. Optimize runtime, memory, and space for future public deployment.
2. Verify compile/runtime and dropdown integrity.
3. Fix duplicate image rows in train dropdown.
4. Add DB-backed auth and pricing engine.
5. Add admin pricing management dashboard.
6. Clean train UI copy controls and switch visible-image sharing flow.
7. Fix mapped/processed statistics semantics.
8. Enhance pricing dashboard with account creation, global mode, and default price visibility.
9. Restrict visible-image sharing to admin side only.
10. Refresh dossier with complete project detail appendices.

## 11) Mistakes, Friction Points, and Corrections
Observed during implementation and corrected:
- Environment Python launcher was not usable (python, .venv\Scripts\python.exe, py issues).
  - Workaround used: Blender-bundled Python + project site-packages for validation.
- Path-case duplicate image indexing caused doubled dropdown rows.
  - Corrected via canonicalization + query-level dedupe.
- Some command/regex quoting attempts failed during shell operations.
  - Re-ran with safer command forms.
- Minor text encoding artifacts surfaced in template text.
  - Normalized display strings where touched.
- Dossier generation initially rendered escaped control characters.
  - Regenerated dossier with safe literal formatting.

## 12) Validation Notes
Completed checks (recent):
- Python compile checks for modified backend files succeeded.
- Runtime checks via Flask test client succeeded for:
  - login flow (admin/customer)
  - /designs payload price behavior
  - pricing endpoints (/admin/pricing_data, /admin/save_price, /admin/toggle_contact_us)
  - dropdown/list consistency checks
- Runtime startup verified after migration-order fix (`created_at` column/index issue resolved).
- Edited files currently report no diagnostics errors (`app.py`, `database.py`, `api/search.py`, `templates/pricing.html`).

## 16) Latest Operational State (2026-05-20)
- App status: running after applied fixes.
- Training UX: searchable SS queue and car filter, exact-leaning stock match loading, arrow-visible Select2 controls.
- Viewer UX: search-first car dropdown and concrete timestamp display.
- Admin pricing UX: stable account creation (no loop), account lifecycle controls, and bulk access controls.
- DB status: migration-safe users schema handling with persistent account action audit logs.

## 13) Current Constraints and Risks
- Large local dataset (~1.39 GB, many images) remains primary deployment footprint.
- Plaintext access codes currently used by request for testing; production should migrate to hashed credentials.
- Share API behavior depends on browser/device support; fallback downloads remain necessary.

## 14) Operational Notes
- Dossier appendices are generated snapshots, not live views.
- Re-run generation after major changes to keep manifests accurate.

## 15) Regeneration Commands (used)
- PROJECT_FILE_MANIFEST.txt: recursive file listing with size and timestamp.
- PROJECT_FOLDER_MANIFEST.txt: recursive directory listing.
- PROJECT_CODE_SNAPSHOT.md: full source snapshot for primary backend/template files.

---
If needed, next pass can add a separate appendix with per-endpoint request/response examples and a SQL schema dump.
