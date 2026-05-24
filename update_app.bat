@echo off
setlocal

REM Production update script for Tally Stock Viewer
REM 1) Stop current server
REM 2) Pull latest code
REM 3) Start launcher silently with pythonw.exe

cd /d C:\tally_stock
if errorlevel 1 (
  echo [ERROR] Could not change directory to C:\tally_stock
  exit /b 1
)

echo [INFO] Stopping live server...
python C:\tally_stock\stop_server.py
if errorlevel 1 (
  echo [WARN] stop_server.py returned an error. Continuing with update...
)

echo [INFO] Pulling latest code from origin/main...
git pull origin main
if errorlevel 1 (
  echo [ERROR] git pull failed. Server will not be restarted automatically.
  exit /b 1
)

echo [INFO] Starting server silently...
start "" pythonw.exe C:\tally_stock\launcher.pyw
if errorlevel 1 (
  echo [ERROR] Failed to start launcher with pythonw.exe
  exit /b 1
)

echo [OK] Update complete.
exit /b 0
