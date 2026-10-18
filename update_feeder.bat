@echo off
rem Updates the Tally feeder machine: move to the latest origin/main, refresh
rem packages, check the feeder still imports, and restart the scheduled task.
rem Run from anywhere; it works on the folder this file lives in.
rem (update_app.bat is for the legacy full-site PC only and refuses to run here.)
rem
rem Safe by design:
rem   * refuses unless .env says MACHINE_ROLE=feeder (so the web app can never be revived here)
rem   * aborts if any TRACKED file has local changes (nothing is touched)
rem   * moves to origin/main with "git checkout -B main origin/main", NOT "git pull", so it
rem     works even when this clone's history diverged from the rewritten history on GitHub.
rem     .env, data\ and logs\ are untracked/ignored and are never touched.
setlocal EnableExtensions
cd /d "%~dp0"

rem ---- 1. role check (plain batch: must work even on the OLD checkout) ----
if not exist ".env" (
    echo [ERROR] No .env here. This script only runs on a feeder machine ^(MACHINE_ROLE=feeder^).
    pause
    exit /b 1
)
findstr /B /I /R /C:"MACHINE_ROLE[ ]*=[ ]*feeder[ ]*$" ".env" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] .env does not contain the line  MACHINE_ROLE=feeder
    echo         Add it ^(see FEEDER_SETUP.md^) and run this again. Nothing was changed.
    pause
    exit /b 1
)
findstr /B /I /C:"MACHINE_ROLE" ".env" | findstr /V /I /R /C:"MACHINE_ROLE[ ]*=[ ]*feeder[ ]*$" >nul 2>&1
if not errorlevel 1 (
    echo [ERROR] .env has another MACHINE_ROLE line that is not "feeder". Remove it. Nothing was changed.
    pause
    exit /b 1
)

rem ---- 2. refuse if tracked files were modified locally (staged, unstaged or deleted) ----
git update-index -q --refresh
git diff-index --quiet HEAD --
if errorlevel 1 (
    echo [ERROR] Tracked files have local changes - aborting, nothing was changed:
    git status --short --untracked-files=no
    echo         Undo them with  git checkout -- .  ^(or ask for help^) and run this again.
    pause
    exit /b 1
)

set "OLDHASH="
for /f %%H in ('git rev-parse --short HEAD') do set "OLDHASH=%%H"
echo [INFO] Currently at %OLDHASH%.

rem ---- 3. fetch and move to origin/main (works with diverged history) ----
echo [INFO] Fetching latest code...
git fetch origin
if errorlevel 1 (
    echo [ERROR] git fetch failed. Check the internet connection and try again. Nothing was changed.
    pause
    exit /b 1
)
git checkout -B main origin/main
if errorlevel 1 (
    echo [ERROR] Could not move to origin/main. Nothing else was changed; the previous commit was %OLDHASH%.
    pause
    exit /b 1
)

echo [INFO] Updating packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

rem ---- 4. confirm the feeder still imports (uses its own log file; touches no database) ----
echo [INFO] Checking feeder.py still loads...
set "LOG_FILE=logs\import_check.log"
".venv\Scripts\python.exe" -c "import feeder" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] feeder.py no longer imports after the update. The task was NOT restarted.
    echo         To go back:  git checkout -B main %OLDHASH%
    pause
    exit /b 1
)
set "LOG_FILE="

echo [INFO] Restarting the feeder task...
schtasks /End /TN TallyFeeder >nul 2>&1
timeout /t 3 /nobreak >nul
schtasks /Run /TN TallyFeeder
if errorlevel 1 (
    echo [ERROR] Could not start the TallyFeeder task. Was it registered? See FEEDER_SETUP.md, step 7.
    pause
    exit /b 1
)

set "NEWHASH="
for /f %%H in ('git rev-parse --short HEAD') do set "NEWHASH=%%H"
echo [OK] Feeder updated: %OLDHASH% -^> %NEWHASH% ^(now on origin/main^). Check logs\feeder.log.
pause
endlocal
