@echo off
setlocal

REM Production update script for Tally Stock Viewer
REM 1) Stop current server
REM 2) Pull latest code
REM 3) Start launcher silently with pythonw.exe

set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

cd /d "%INSTALL_DIR%"
if errorlevel 1 (
  echo [ERROR] Could not change directory to %INSTALL_DIR%
  exit /b 1
)

echo [INFO] Stopping live server...
if exist "%INSTALL_DIR%\app.pid" (
  for /f "usebackq delims=" %%P in ("%INSTALL_DIR%\app.pid") do taskkill /PID %%P /T /F >nul 2>&1
  del "%INSTALL_DIR%\app.pid" >nul 2>&1
)

echo [INFO] Pulling latest code from origin/main...
git pull origin main
if errorlevel 1 (
  echo [ERROR] git pull failed. Server will not be restarted automatically.
  exit /b 1
)

echo [INFO] Starting server silently...
start "" "%INSTALL_DIR%\.venv\Scripts\pythonw.exe" "%INSTALL_DIR%\launcher.pyw"
if errorlevel 1 (
  echo [ERROR] Failed to start launcher with pythonw.exe
  exit /b 1
)

echo [OK] Update complete.
exit /b 0
