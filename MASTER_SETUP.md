# Tally Stock Viewer — Setup & Deployment Guide (Master)

This master guide consolidates project setup, production deployment, troubleshooting, and operational procedures for Tally Stock Viewer v2.0.

---

## Overview

Tally Stock Viewer is a Flask + SQLite application for viewing and mapping Tally stock with an image training UI. This guide explains how to install prerequisites, prepare a Windows host, create a portable environment, deploy using `waitress`, and (optionally) expose the app via Cloudflare Tunnel (`cloudflared`).

**Key changes in v2.0**
- Portable relative image paths in DB (no absolute machine paths).
- Background service execution using `pythonw.exe` and a tray `launcher.pyw`.
- `serve.py` to run the app under `waitress` in production.
- Automated shortcuts via `setup_office_pc.bat`.
- `requests` added to standard dependencies for Tally API integration.

---

## Prerequisites (Windows)
- Windows 10/11 (64-bit)
- Internet access for package installation
- Admin rights for certain setup steps (creating shortcuts, installing system-wide tools)

### Recommended tools
- Git for Windows (64-bit x86_64 installer)
- Python 3.11 or 3.10 (official Windows installer, 64-bit)
- PowerShell (default on Windows 10+)

---

## 1. Install Git
Download from: https://git-scm.com/download/win — choose the standard 64-bit installer (x86_64). Avoid ARM64 builds on Intel/AMD machines.

---

## 2. Install Python
1. Download the Windows x86-64 installer for Python 3.10/3.11 from python.org.
2. Run the installer and *check* "Add Python to PATH".
3. Prefer installing for "All Users" to reduce file-permissions issues.

Verify:

```powershell
python --version
pip --version
```

---

## 3. Clone or copy the project
Option A — Git (recommended):

```powershell
cd C:\Path\To\Where\You\Want\Tally_Stock
git clone https://your.repo/url tally_stock
cd tally_stock
```

Option B — Manual copy (USB): copy the project folder into `C:\tally_stock` or any chosen path.

Note: The app uses portable relative paths for image references, so the folder can be moved between machines.

---

## 4. Create virtual environment and install dependencies
Run these commands from the project root (where `app.py` lives):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # PowerShell
pip install --upgrade pip
pip install -r requirements.txt
```

If you copied `.venv` from another machine, delete it and recreate it locally to avoid binary incompatibilities.

---

## 5. `requirements.txt` (project)
The project includes a `requirements.txt`. Typical contents:

```
Flask
waitress
requests
Pillow
psutil
pystray
```

Install with `pip install -r requirements.txt`.

---

## 6. Project files of interest
- `app.py` — Flask application and routes.
- `serve.py` — Production entrypoint (uses `waitress`).
- `launcher.pyw` — System tray launcher that starts/stops the app and optionally runs `cloudflared`.
- `database.py` — SQLite helper and path canonicalization.
- `image_scanner.py` — Scans `data/S.S IMAGE/` and inserts relative paths into DB.
- `setup_office_pc.bat` — (optional) script to create Desktop shortcuts and prepare environment.

---

## 7. Running locally (development)
Activate the venv and start Flask (dev server):

```powershell
.\.venv\Scripts\Activate.ps1
$env:FLASK_APP='app.py'
flask run
```

The app will be available at `http://127.0.0.1:5000`.

---

## 8. Running in production (recommended)
Use `serve.py` which launches `waitress` and logs to `C:\tally_stock\logs\app.log` by default.

```powershell
.\.venv\Scripts\Activate.ps1
python serve.py
```

To run the app silently as a background GUI process via the included launcher (recommended for non-server Windows installs):
- Double-click `launcher.pyw` (or create a shortcut that runs it with `pythonw.exe`).
- The launcher manages the server lifecycle and optionally starts `cloudflared` if present.

---

## 9. Cloudflare Tunnel (optional)
1. Download `cloudflared.exe` and place it in the project root (`C:\tally_stock\cloudflared.exe`).
2. Authenticate & create a tunnel following Cloudflare docs (one-time, manual interactive step).
3. The `launcher.pyw` will attempt to start/stop the tunnel automatically if `cloudflared.exe` exists.

Manual start example:

```powershell
.\cloudflared.exe tunnel --url http://localhost:5000 --name tally-stock
```

The launcher stores the tunnel PID at `C:\tally_stock\tunnel.pid` and will kill it on stop.

---

## 10. Setup Shortcuts (setup_office_pc.bat)
The repo contains `setup_office_pc.bat` which performs these actions:
- Builds a fresh `.venv` if missing
- Creates `Start Stock Viewer.bat` and `Stop Stock Viewer.bat` shortcuts on the current user's Desktop

Run it after copying the folder to a new machine.

---

## 11. Troubleshooting (common issues)
- Git installer `CreateProcess failed; code 216`: use the x86_64 Git installer, not ARM64.
- C-extension / numpy crashes: Delete and recreate `.venv` locally.
- PowerShell `Access is denied` when deleting `.venv`: kill `pythonw` processes first: `Stop-Process -Name "pythonw" -Force`.
- Missing `Start Stock Viewer.bat` after copying: run `setup_office_pc.bat` to regenerate shortcuts for the current machine.

---

## 12. Fresh Installation Workflow (Manual Copy)
If deploying via USB copy, follow these commands in an elevated PowerShell session:

```powershell
cd C:\tally_stock
# Remove any copied virtualenv and create a fresh one
Remove-Item -Recurse -Force .venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 13. Security & Exposure Checklist (before going public)
- Ensure only required ports are open to localhost. `waitress` binds to 127.0.0.1 by default.
- Use a reverse-proxy / tunnel (Cloudflare Tunnel) when exposing to internet.
- Limit sensitive credentials in the repo or DB; do not store API credentials in plain text.

---

## 14. Verification & Smoke Tests
After starting `serve.py`:
- Visit `http://127.0.0.1:5000` and confirm the UI loads.
- Use `GET /health` or a simple endpoint to confirm readiness (if present).
- Navigate the Train UI and confirm images are served correctly from `/get_image/<id>`.

---

## 15. Appendix: Useful Commands
Activate venv (PowerShell):

```powershell
.\.venv\Scripts\Activate.ps1
```

Install waitress & deps:

```powershell
pip install waitress requests Pillow psutil pystray
```

Start serve.py manually:

```powershell
python serve.py
```

Start launcher (tray): double-click `launcher.pyw` or run:

```powershell
.\.venv\Scripts\pythonw.exe launcher.pyw
```

---

If you want, I can also generate a trimmed `README.md` from this master guide and commit the changes to the repository. Let me know if you prefer a different filename or additional CI/packaging instructions.
