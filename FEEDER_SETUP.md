# Tally Feeder - Setup Guide

This is the **current, recommended setup for the Tally machine**.

The real site (all data, images, logins, admin) lives on **Render**. This machine only
runs `feeder.py`: it reads stock from Tally every few minutes and sends it to Render.
It has no website, no database, no images, no tunnel and no tray icon. Nobody uses it
directly.

> ## WARNING: never run two feeders at once
> If the old spare PC is ever switched on as a manual full-site backup, **stop the
> feeder on this machine first** (`schtasks /End /TN TallyFeeder`, and also
> `schtasks /Change /TN TallyFeeder /DISABLE` if it will stay off for long). If both
> machines push to Render at the same time, they overwrite each other with
> conflicting data. Turn the feeder back on afterwards
> (`schtasks /Change /TN TallyFeeder /ENABLE`, then `schtasks /Run /TN TallyFeeder`).

## What you need before starting

- A Windows PC that can reach Tally (Tally Prime on the same PC, or on the same network)
- Tally Prime open with the company loaded and its port enabled (default **9000**)
- The Render site's **intake URL** and **secret token** (`INTAKE_SYNC_TOKEN` on Render)
- An internet connection

## 1. Install Python

1. Download Python 3.11 (64-bit) from https://www.python.org/downloads/
2. Run the installer and **tick "Add Python to PATH"**
3. Check in a new terminal: `python --version`

## 2. Install Git

1. Download from https://git-scm.com/download/win and install with the defaults
2. Check in a new terminal: `git --version`

## 3. Get the code

Use a plain folder such as `C:\tally_feeder` (not Desktop or Documents, so the
background task can always read it):

```powershell
git clone https://github.com/idris900323/tally-stock-viewer.git C:\tally_feeder
cd C:\tally_feeder
```

## 4. Install the packages

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

This takes a few minutes. (It installs the whole project's packages, more than the
feeder strictly needs - that is intentional, so the feeder reuses the site's
proven Tally code unchanged.)

## 5. Create the settings file

Create a file called `.env` in `C:\tally_feeder` (Notepad is fine, save as "All files",
name it exactly `.env`) with just these lines:

```text
TALLY_URL=http://localhost:9000
CLOUD_SYNC_URL=https://<your-render-app>.onrender.com/admin/intake/sync_data
CLOUD_SYNC_TOKEN=<the same value as INTAKE_SYNC_TOKEN on Render>
```

Optional (defaults shown, leave out unless you need to change them):

```text
TALLY_EXPORT_INTERVAL=180
FEEDER_FULL_REFRESH_HOURS=6
```

- `TALLY_EXPORT_INTERVAL` - seconds between stock updates
- `FEEDER_FULL_REFRESH_HOURS` - how often the car list and hierarchy are re-read too
  (new cars / new items). Stock quantities always update at the shorter interval.

If `CLOUD_SYNC_URL` or `CLOUD_SYNC_TOKEN` is missing, the feeder logs why and exits.

## 6. Run it once by hand

```powershell
.venv\Scripts\python.exe feeder.py
```

Within a few seconds `logs\feeder.log` (open it in another window) should show:

```text
Feeder started: Tally=...
Running full refresh
Item stock export completed ... (N items)
Full refresh from Tally completed
Cloud sync push succeeded (N cars, N hierarchy rows, N stock rows)
```

Then every few minutes a stock export followed by another "Cloud sync push succeeded".
Open the Render site and confirm the stock looks right. Press **Ctrl+C** to stop.

If it says it cannot reach Tally, check that Tally is open with the company loaded and
that `TALLY_URL` is right. If the push fails, check the URL and token. It keeps retrying
on its own every cycle, so fixing the cause is enough.

## 7. Make it run by itself (Scheduled Task)

Open PowerShell **as administrator** (right-click, Run as administrator):

```powershell
cd C:\tally_feeder
powershell -ExecutionPolicy Bypass -File scripts\register_feeder_task.ps1
```

This creates a Windows Scheduled Task called `TallyFeeder` that:

- **starts when the PC boots** (nobody has to log in),
- **restarts automatically** if the feeder ever crashes or is killed: Windows checks every 5 minutes and starts it again if it is not running (so a crash costs at most about 5 minutes),
- is **not** stopped for running too long, and never runs two copies at once.

Useful commands:

```powershell
schtasks /Query /TN TallyFeeder /V /FO LIST   # status
schtasks /Run /TN TallyFeeder                 # start now
schtasks /End /TN TallyFeeder                 # stop
```

Then restart the PC once and confirm `logs\feeder.log` starts filling in by itself.

## Updating later

Double-click `update_feeder.bat` (it pulls the latest code, updates packages and restarts
the task). `update_app.bat` and `first_time_setup.bat` are for the old full-site PC only.

## Troubleshooting

- **Nothing in `logs\feeder.log`:** run step 6 by hand and read the error on screen.
- **"Cloud sync push failed: HTTP 403":** the token does not match Render's `INTAKE_SYNC_TOKEN`.
- **Push works but Render shows old stock:** check the Tally lines in the log; a
  failing Tally connection is retried every cycle and logged.
- **Task shows "Running" but log is silent:** the PC may be waiting on Tally; check Tally is open.
