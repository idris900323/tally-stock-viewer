@echo off
rem Updates the Tally feeder machine: pull the latest code, refresh packages,
rem and restart the scheduled task. Run from anywhere; it works on the folder
rem this file lives in. (The older update_app.bat is for the full-site PC only.)
setlocal
cd /d "%~dp0"

echo [INFO] Pulling latest code...
git pull origin main
if errorlevel 1 (
    echo [ERROR] git pull failed. Check the internet connection and try again.
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

echo [INFO] Restarting the feeder task...
schtasks /End /TN TallyFeeder >nul 2>&1
timeout /t 3 /nobreak >nul
schtasks /Run /TN TallyFeeder
if errorlevel 1 (
    echo [ERROR] Could not start the TallyFeeder task. Was it registered? See FEEDER_SETUP.md, step 7.
    pause
    exit /b 1
)

echo [OK] Feeder updated and restarted. Check logs\feeder.log.
pause
endlocal
