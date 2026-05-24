# Tally Stock Viewer — Setup Guide (Fresh Windows PC)

This guide explains how to install and configure the project when you have only the project folder (e.g., on a USB stick or shared drive). The steps assume you will copy the folder to `C:\tally_stock` and run a single administrator batch to set up everything.

Prerequisites
-------------
- Windows 10 or later
- Internet access for package installation
- Local user with Administrator privileges to run `setup_office_pc.bat`

Files included in this repository
--------------------------------
- `app.py` — Flask application entry point
- `database.py` — SQLite helpers
- `launcher.pyw` — Silent system-tray launcher (created)
- `setup_office_pc.bat` — One-click installer for office PC (created)
- `generate_icon.py` — Creates `icon.png` used by the tray icon (created)
- `requirements_launcher.txt` — Launcher requirements: `pystray`, `Pillow`, `psutil`
- `deploy_notes.md` — Deploy notes and production options
- `remove_autostart.bat` — Removes registry autostart entry

Quick overview
--------------
1. Copy the project folder to `C:\tally_stock`.
2. Run `setup_office_pc.bat` as Administrator (it will create a venv, install dependencies, create Desktop shortcuts, and register autostart).
3. Use the Desktop shortcut "Start Stock Viewer.bat" to launch silently (system tray icon).

Detailed steps (what the script does)
------------------------------------
1) Verify Administrator and Python 3.8+ exists.
2) Copy project files to `C:\tally_stock` (the script uses `xcopy`).
3) Create a Python virtual environment at `C:\tally_stock\.venv`.
4) Use `pip` inside the venv to install app requirements and launcher dependencies.
   - If you maintain a `requirements.txt`, the script will attempt to install that file.
   - Launcher dependencies (`pystray`, `Pillow`, `psutil`) are installed separately.
5) Create `C:\tally_stock\logs` and `C:\tally_stock\data\S.S IMAGE` folders.
6) Generate `icon.png` used by `launcher.pyw` by running `generate_icon.py`.
7) Create two Desktop shortcuts:
   - `Start Stock Viewer.bat` — runs launcher silently with `pythonw.exe`.
   - `Stop Stock Viewer.bat` — uses `psutil` to terminate the running PID recorded in `app.pid`.
8) Register registry autostart entry to run `launcher.pyw` on user login.

Commands the user can run manually
---------------------------------
Open an elevated PowerShell and run:

```powershell
cd "C:\path\to\copied\project\root"
.\setup_office_pc.bat
```

Starting and stopping manually
------------------------------
- Start silently from Desktop: double-click "Start Stock Viewer.bat".
- Stop via Desktop: double-click "Stop Stock Viewer.bat".
- To run the Flask app visibly (useful for debugging):

```powershell
C:\tally_stock\.venv\Scripts\python.exe C:\tally_stock\app.py
```

Troubleshooting
---------------
- If the tray icon doesn't appear, check `C:\tally_stock\logs\app.log` for errors.
- If Python packages fail to install, ensure the machine has internet access and that the system Python is accessible.
- If `cloudflared` or `ngrok` is used, follow `deploy_notes.md` for public exposure setup.

Security & Production notes
---------------------------
- Do not put `data/mappings.db` in a public folder.
- Use HTTPS and secure session cookies in production.
- Consider adding a reverse proxy (nginx/IIS) and running the Flask app behind a WSGI server for production.

Post-install checklist
----------------------
- [ ] Confirm `C:\tally_stock\logs\app.log` exists and is writable.
- [ ] Confirm Desktop shortcuts created.
- [ ] Confirm registry entry exists under HKCU Run.
- [ ] Start the app and verify access at http://localhost:5000

Additional first-run steps (required):
- [ ] Copy `data/main.xlsx` and `data/car master list.xls` from the old machine to `C:\tally_stock\data\` (the app will start but show no data without these files).
- [ ] Ensure Tally Prime is open and running on the same PC and exposing port `9000` before triggering the stock sync.
- [ ] Log in as admin at http://localhost:5000 (default: `admin / idris123`).
- [ ] Go to Admin → "Scan Images" to run the initial image index of `data\S.S IMAGE` into the database.
- [ ] Change the admin password and customer access codes before going public.

Contact
-------
For help, provide `C:\tally_stock\logs\app.log` and a description of the problem.
