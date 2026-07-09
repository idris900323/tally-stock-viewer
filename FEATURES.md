# Tally Stock Viewer - What This Actually Is

A self-hosted stock/catalog system that connects to Tally Prime, matches product
photos to stock items, and shows a searchable, priced catalog to admin and
customer users - built and run by one person on an office PC.

## Features

**Tally integration**
- Pulls car master, hierarchy, and stock quantities directly from Tally Prime over HTTP/XML
- Auto-refreshes ~45 seconds after every app start, plus manual `Update Stock` / `Full Refresh` buttons
- Falls back to the last cached export if Tally is offline, instead of breaking the page
- "Last updated" timestamp turns red live if data goes stale (no refresh in 180s)

**Stock catalog**
- Searchable stock browser with car/model filtering
- Role-aware views: admin sees everything, customers see a read-only, priced view

**Pricing**
- Global base price plus per-customer override pricing
- Per-customer "Contact Us" mode, forceable by admin, with automatic fallback when no price is set

**Image matching / training workflow**
- Heuristic matcher suggests likely stock items for unmapped photos (stock-code extraction, folder-name similarity, history of past confirmations)
- Live "currently matched image" preview while picking a stock item, so admins can visually confirm before saving
- One image per stock item enforced automatically - confirming a new match silently retires the old one
- Direct photo upload from the training screen (validated for type/size, auto-creates the car folder, auto-confirms the match) - no manual file copying + rescanning required
- Auto rescans the image folder on every startup, plus a manual rescan button

**Accounts**
- Session-based login, admin and customer roles, access-code auth
- Create / pause / resume / delete customer accounts, individually or in bulk
- Paused accounts are locked out at login with a clear message
- Last-login tracking and an audit log (`account_logs`) for every account action

**Deployment / ops**
- One-shot Windows setup script (venv, dependencies, `.env`, desktop shortcuts, auto-start)
- Tray launcher that runs the server, monitors it, and can start a Cloudflare tunnel for public access
- Git-based one-command update path
- `/health` endpoint, file-based logging, self-migrating SQLite schema (no manual DB migration steps when columns are added)

## How good is it, honestly

**Solid for what it is:**
- Real integration with an external system (Tally) that degrades gracefully instead of falling over when Tally is closed or slow
- Schema changes migrate themselves (`_ensure_users_schema`) - upgrading doesn't require someone to hand-run SQL on the office PC
- Upload/account/mapping endpoints have real input validation: path-traversal guards on uploaded file names and folders, file type/size checks, typed-confirmation before deleting an account
- Reasonable separation of concerns (`database.py`, `matcher.py`, `image_scanner.py`, `api/search.py`) rather than one giant file doing everything
- UX details that only get added after actually using the tool day to day: the currently-matched-image preview, stale-data warning, sticky nav, bulk pause/resume

**Where it's still workshop-grade, not enterprise:**
- No real automated test coverage (`test_tally.py` is a single 52-line script, not a suite)
- Matching is heuristic/rule-based, not ML - it won't get smarter on its own
- Single admin account model; no granular permissions
- Runs on one office PC with a tray launcher rather than a managed server - fine for the current scale, would need rework to scale past it

Overall: a genuinely useful, correctly-engineered internal tool - not a toy, not over-engineered either. It solves the actual business problem (matching photos to stock and pricing them per customer) without dragging in a framework or infrastructure it doesn't need.

## How much effort this took

From the repo history: 13 commits spanning **2026-05-20 to 2026-07-08** (about 7 weeks), ~8,400 lines of code across the app, plus three separate written guides (`MASTER_SETUP.md`, `GOING_PUBLIC.md`, `SOFTWARE_DEEP_DIVE.md`) documenting setup, public rollout, and architecture.

That includes:
- 34 Flask routes covering auth, stock, pricing, training, accounts, and search
- A custom XML request/response layer for talking to Tally directly (no official SDK)
- A full account-management system with pause/resume/bulk actions and audit logging
- An image-matching pipeline from filesystem scan through heuristic suggestion to confirmed mapping
- End-to-end Windows packaging: setup script, tray app, production server, auto-start, optional public tunnel

Note: 13 commits for this much surface area suggests large chunks of work were committed in batches rather than continuously - the actual hands-on-keyboard time is very likely more than the commit count alone implies. This is not a weekend project; it's closer to a small production system built and hardened iteratively against a real, live use case (an actual office running actual Tally data).
