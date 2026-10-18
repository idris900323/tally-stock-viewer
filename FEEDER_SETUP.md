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
MACHINE_ROLE=feeder
TALLY_URL=http://localhost:9000
CLOUD_SYNC_URL=https://<your-render-app>.onrender.com/admin/intake/sync_data
CLOUD_SYNC_TOKEN=<the same value as INTAKE_SYNC_TOKEN on Render>
```

Optional (defaults shown, leave out unless you need to change them):

```text
TALLY_EXPORT_INTERVAL=180
FEEDER_FULL_REFRESH_HOURS=6
```

- `FEEDER_TASK_NAME` - only if your Scheduled Task is not called `TallyFeeder`; `update_feeder.bat`
  uses it to restart the right task (letters, digits, space, `_` `.` `-` only)
- `TALLY_EXPORT_INTERVAL` - seconds between stock updates
- `FEEDER_FULL_REFRESH_HOURS` - how often the car list and hierarchy are re-read too
  (new cars / new items). Stock quantities always update at the shorter interval.

`MACHINE_ROLE=feeder` marks this PC as feeder-only: `serve.py`, `launcher.pyw`,
`relaunch_helper.py`, `python app.py` and `update_app.bat` / `first_time_setup.bat` all refuse to
run (exit code 3, one clear line, nothing created, no port, no `mappings.db`), even if a
`git pull` brings the web-app files back. `feeder.py` is unaffected and logs the role at startup.
To run the web app on a machine anyway, set `MACHINE_ROLE=full` (or remove the line).
Render never sets it and behaves as before.

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

This creates a Windows Scheduled Task called `TallyFeeder` (change it with `-TaskName`) that:

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

Double-click `update_feeder.bat`. It refuses unless `.env` has `MACHINE_ROLE=feeder`, aborts
(touching nothing) if any tracked file has local changes, then fetches and moves to
`origin/main` with `git checkout -B main origin/main` - not `git pull`, so it works even when
this clone's history diverged from GitHub's rewritten history. `.env`, `data\` and `logs\` are
never touched. It then updates packages, checks `feeder.py` still imports, restarts the task
and prints the old and new commit. `update_app.bat` and `first_time_setup.bat` are for the old
full-site PC only and refuse to run on a feeder machine.

Do not run `scripts\make_dormant.ps1` in a feeder checkout: it renames tracked files, so the
update script (correctly) refuses to run there. The role guard makes that unnecessary.

## Switching an existing feeder PC to the guarded version (one-off)

Use this the first time, because the PC still has the OLD `update_feeder.bat` (which runs
`git pull`). After this, `update_feeder.bat` does future updates. Folder and task names
below are placeholders: find the real ones in step 1. Run everything in a Command Prompt.

> **Prerequisite:** the guard must be on `origin/main`. If it is still only on the branch
> `machine-role-guard`, use `origin/machine-role-guard` in step 8 and do **not** run
> `update_feeder.bat` afterwards (it moves to `origin/main`, which would remove the guard).
>
> **Never print `.env`** (`type .env`, `notepad` screenshots, etc.): it holds the sync token.
> Check single lines with `findstr /B /I MACHINE_ROLE .env`.
>
> **Stop and ask for help** whenever a step does not show what it says it should.

1. **Find the real folder and task name** (read-only):
   ```
   schtasks /Query /FO LIST | findstr /I "TaskName"
   schtasks /Query /TN "<task name>" /V /FO LIST | findstr /I "Task To Run, Start In, Status"
   ```
   "Task To Run" / "Start In" show the folder that holds `feeder.py`. Then:
   ```
   cd /d <that folder>
   dir feeder.py .env
   ```
2. **Back up `.env` and `data` outside the repo:**
   ```
   mkdir C:\feeder_backup
   copy .env C:\feeder_backup\.env
   xcopy data C:\feeder_backup\data /E /I /Y
   dir C:\feeder_backup
   ```
   (If `data` contains a large `S.S IMAGE` folder, copy only `data\*.json`, `*.xls*` and `*.db`.)
3. **Write down where you are** (for undo): `git log -1 --oneline` and
   `git status --short --untracked-files=no`
   - If step 3's status lists files, **look at them first** (`git diff --stat`, then
     `git diff <file>`). Do not discard anything you have not looked at. To set changes
     aside safely (recoverable later with `git stash pop`): `git stash push`.
4. **Stop the feeder:** `schtasks /End /TN "<task name>"`
5. **Set the role** (no output of `.env` contents):
   ```
   findstr /B /I MACHINE_ROLE .env
   echo.>> .env
   echo MACHINE_ROLE=feeder>> .env
   findstr /B /I MACHINE_ROLE .env
   ```
   The first check should print nothing (or an old line to fix by hand in Notepad); the last must
   print **exactly one** line, `MACHINE_ROLE=feeder`.
   If the task name is not `TallyFeeder`, also add a line `FEEDER_TASK_NAME=<task name>` the same
   way, so `update_feeder.bat` can restart it later.
6. **Fetch:** `git fetch origin`. If it asks for a username/password or fails, **stop**.
7. **Check the fetch worked:** `git log -1 --oneline origin/main` shows the expected newest commit.
8. **Move to the new code** (works even though history was rewritten):
   ```
   git checkout -B main origin/main
   git log -1 --oneline
   dir machine_role.py
   ```
   `machine_role.py` must exist and `git log -1` must show the new commit. **If either is wrong,
   stop - do not run `serve.py`: an old `serve.py` would start the web app.**
9. **Packages:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`
10. **Prove the guard:** `.venv\Scripts\python.exe serve.py` should print
    `serve.py: This machine is set to feeder-only; ...` and return; then `echo %errorlevel%` must
    print `3`. Then `.venv\Scripts\python.exe -c "import feeder"` must print nothing.
11. **Start the feeder and confirm:**
    ```
    schtasks /Run /TN "<task name>"
    ```
    After 1-2 minutes: `findstr /C:"machine role" /C:"push succeeded" logs\feeder.log` should show
    `machine role=feeder` and `Cloud sync push succeeded`, and
    `schtasks /Query /TN "<task name>" /V /FO LIST | findstr /I "Status Last"` should be sane.

**Undo**
- *Let the web app run again on this PC:* in Notepad, delete the `MACHINE_ROLE=feeder` line from
  `.env` (or change it to `MACHINE_ROLE=full`).
- *Go back to the old code:* `schtasks /End /TN "<task name>"`, then
  `git checkout -B main <hash from step 3>`, then `schtasks /Run /TN "<task name>"`. (The old
  commits stay in `git reflog` even after a history rewrite.) If you stashed, `git stash pop`.
- *Restore settings/data:* `copy /Y C:\feeder_backup\.env .env` and
  `xcopy C:\feeder_backup\data data /E /I /Y`.

## Troubleshooting

- **Nothing in `logs\feeder.log`:** run step 6 by hand and read the error on screen.
- **"Cloud sync push failed: HTTP 403":** the token does not match Render's `INTAKE_SYNC_TOKEN`.
- **Push works but Render shows old stock:** check the Tally lines in the log; a
  failing Tally connection is retried every cycle and logged.
- **Task shows "Running" but log is silent:** the PC may be waiting on Tally; check Tally is open.
