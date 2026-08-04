# Tally Stock Viewer - What This Actually Is

A self-hosted stock/catalog system that connects to Tally Prime, matches product
photos to stock items, and shows a searchable catalog to admin and
customer users - built and run by one person on an office PC.

## Features

**Tally integration**
- Pulls car master, hierarchy, and stock quantities directly from Tally Prime over HTTP/XML
- Stock export is a single TDL collection request per cycle (measured production runs put the old three-request cycle at ~20-27s of Tally engine work and the replacement at ~1.5-5s depending on load — the old detailed Stock Summary visibly stalled Tally Prime every refresh)
- Stock quantities auto-refresh every 3 minutes while the app runs (`TALLY_EXPORT_INTERVAL`, default 180s); a one-time full refresh (car master + hierarchy + stock) also runs ~45 seconds after every app start, plus manual `Update Stock` / `Full Refresh` buttons
- Falls back to the last cached export if Tally is offline, instead of breaking the page
- "Last updated" timestamp turns red live if data goes stale (no refresh in 180s); if it's been stale for a full 15 minutes with a real refresh failure behind it, a clear banner (plus a short matching hint) explains the likely reason in plain language (Tally closed, Tally slow, multiple Tally windows open) - both clear themselves automatically once a refresh succeeds again, and neither one shows falsely right after a restart or during a brief retry-able blip
- Detects multiple running Tally instances before each export and surfaces a clear on-screen message instead of a cryptic failure
- Cars deleted from Tally disappear from the dropdown instead of showing hundreds of wrong cross-car designs

**Stock catalog**
- Searchable stock browser with car/model filtering
- Role-aware views: admin sees everything, customers see a read-only view
- Tally shelf-location codes (`* M-20`, `****H-4****`, ...) are stripped from car names everywhere a human reads them, while the raw names keep driving search and matching

(An earlier pricing feature — global base prices plus per-customer overrides — was removed; leftover `base_prices`/`customer_prices` tables still exist in the database but nothing uses them.)

**Image matching / training workflow**
- Heuristic matcher suggests likely stock items for unmapped photos (stock-code extraction, folder-name similarity, history of past confirmations)
- Live "currently matched image" preview while picking a stock item, so admins can visually confirm before saving
- One image per stock item enforced automatically - confirming a new match silently retires the old one (but one image can serve many stock items)
- Bulk Match screen: confirm one photo against hundreds of stock items at once - find them by text search or by auto-derived product category (type + color, e.g. "7D MAT / BLACK-TAN"), tick, confirm. Built for floor mats and curtains where the same photo applies across car variants
- Direct photo upload from the training screen (validated for type/size, auto-creates the car folder, auto-confirms the match) - no manual file copying + rescanning required
- "Add Image" button on every design card jumps to Training Mode with the car and stock item pre-selected
- Share Images mode on the main page sends selected images through share-ready endpoints backed by cached derivatives in `data/share_cache/`
- Auto rescans the image folder on every startup, plus a manual rescan button - rescanning is a true two-way sync: it also detects database rows whose file was deleted from disk and offers to remove them (with an expandable list of the exact files and any linked stock item, and a safety threshold that blocks removal if a suspiciously large share of the catalog looks missing at once - e.g. a disconnected image drive)
- The image scanner accepts `.jfif` files alongside the other supported image formats

**Material-tier categorization**
- Every design can be tagged with a material-tier category (Pearl, Pearl Designer, Pearl Deluxe, Saka, Ruby, Napa Deluxe, Napa Designer) through a continuous "Manage Categories" session on the main page - pick a category, tick items, apply; the batch saves immediately and the session stays open for the next batch until you click Done
- Categorized designs show a compact, color-coded label along the bottom of their thumbnail (full name still available via tooltip and the full-screen view) and are grouped by category on both the admin and customer views, uncategorized items last

**Prioritized work queues**
- Two admin-only dashboards - "Needs Category" and "Needs Image Matching" - rank every car by how much of its catalog is actually missing, so cars close to fully done surface ahead of cars barely started, instead of an alphabetical or arbitrary list
- Reachable from the main page's More menu; clicking a car jumps straight into the right workflow (the Manage Categories session, or Training Mode) with that car already selected - no manual re-searching
- Kept out of the way by default on the Train Matches page, each with its own close control once opened

**Accounts**
- Session-based login, admin and customer roles, access-code auth
- Create / pause / resume / delete customer accounts, individually or in bulk
- Paused accounts are locked out at login with a clear message
- Last-login tracking and an audit log (`account_logs`) for every account action
- Manage Accounts sits behind its own second password on top of the admin login (session-based, re-entered every new login) - a compromised admin session alone can't reach customer account data

**Deployment / ops**
- One-shot Windows setup script (venv, dependencies, `.env`, desktop shortcuts, auto-start)
- Tray launcher that runs the server, monitors it, and can start a Cloudflare tunnel for public access
- Remote System panel (token-paired devices only): git status, pull-and-restart, restart-app-only, duplicate-image report, log tail, DB backup download, disk/uptime/env read-outs, autostart check, Tally status, and a live Tally performance test - the office PC can be managed without remote desktop
- Restarts are self-sufficient: a detached relaunch helper brings the server back even if the tray launcher's watchdog is broken or absent
- Git-based one-command update path that also verifies and repairs the Windows autostart entry on every update
- `/health` endpoint, file-based logging, self-migrating SQLite schema (no manual DB migration steps when columns are added - even a table-level constraint removal runs as an automatic, backed-up rebuild)

## How good is it, honestly

**Solid for what it is:**
- Real integration with an external system (Tally) that degrades gracefully instead of falling over when Tally is closed or slow
- Schema changes migrate themselves (`_ensure_users_schema`) - upgrading doesn't require someone to hand-run SQL on the office PC
- Upload/account/mapping endpoints have real input validation: path-traversal guards on uploaded file names and folders, file type/size checks, typed-confirmation before deleting an account
- Reasonable separation of concerns (`database.py`, `matcher.py`, `image_scanner.py`, `api/search.py`, `utils/`) rather than one giant file doing everything
- Performance work done by measurement, not guesswork: the stock export was rebuilt around timings taken against the live production Tally (a diagnostic script first, then a browser-triggerable perf test, then the fix)
- UX details that only get added after actually using the tool day to day: the currently-matched-image preview, stale-data warning with plain-words failure reasons, sticky nav, bulk pause/resume, pre-selected training links from design cards

**Where it's still workshop-grade, not enterprise:**
- No real automated test coverage (`test_tally.py` is a single 52-line script, not a suite)
- Matching is heuristic/rule-based, not ML - it won't get smarter on its own
- Single admin account model; no granular permissions
- Runs on one office PC with a tray launcher rather than a managed server - fine for the current scale, would need rework to scale past it

Overall: a genuinely useful, correctly-engineered internal tool - not a toy, not over-engineered either. It solves the actual business problem (matching photos to stock items and showing customers a live catalog) without dragging in a framework or infrastructure it doesn't need.

## How much effort this took

From the repo history: 72 commits spanning **2026-05-20 to 2026-08-04** (about 11 weeks), ~15,800 lines of code across the app, plus three separate written guides (`MASTER_SETUP.md`, `GOING_PUBLIC.md`, `SOFTWARE_DEEP_DIVE.md`) documenting setup, public rollout, and architecture.

That includes:
- 60 Flask routes covering auth, stock, training, bulk matching, accounts, search, remote system management, material-tier categorization, and prioritized work queues
- A custom XML request/response layer for talking to Tally directly (no official SDK), including TDL collection requests tuned against real production timing measurements
- A full account-management system with pause/resume/bulk actions and audit logging, now with its own secondary password gate independent of the admin login
- An image-matching pipeline from filesystem scan through heuristic suggestion to confirmed mapping, now a true two-way sync (add and remove, with a mass-deletion safety threshold), plus a one-to-many bulk matching workflow with automatic product categorization
- A material-tier tagging system (continuous multi-batch session, not a one-shot picker) and two prioritized dashboards that rank every car by how much work is actually left, tucked behind a consolidated More menu instead of cluttering the page
- End-to-end Windows packaging: setup script, tray app, production server, auto-start (self-repairing on every update), optional public tunnel, and a remote ops panel

The early history was committed in large batches, so the hands-on-keyboard time is more than the commit count alone implies; the later history shows the opposite pattern - small, heavily-verified fixes hardened against a real, live use case (an actual office running actual Tally data), several of them diagnosed with measurements taken on the production machine. This is not a weekend project; it's a small production system built and maintained iteratively.
