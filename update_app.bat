@echo off
setlocal enabledelayedexpansion

cd /d C:\tally_stock
if errorlevel 1 (
    echo [ERROR] Could not find C:\tally_stock
    pause
    exit /b 1
)

echo [INFO] Stopping app if running...
"C:\tally_stock\.venv\Scripts\python.exe" "C:\tally_stock\stop_server.py"
timeout /t 3 /nobreak >nul

echo [INFO] Killing any remaining python processes on port 5000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5000 ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo [INFO] Pulling latest code from GitHub...
git pull origin main
if errorlevel 1 (
    echo [ERROR] git pull failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo [INFO] Checking System Panel access token...
findstr /B "SYSTEM_ACCESS_TOKEN=" "C:\tally_stock\.env" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ============================================================
    echo   ONE-TIME SETUP: System Panel Access Token
    echo ============================================================
    echo   This machine needs a System Panel access token so you can
    echo   remotely check deployment status and pull updates from a
    echo   browser. This only needs to be set once.
    echo.
    set "SYS_TOKEN="
    set /p "SYS_TOKEN=Paste a token now, or press Enter to auto-generate one: "
    if not defined SYS_TOKEN (
        for /f "delims=" %%T in ('"C:\tally_stock\.venv\Scripts\python.exe" -c "import secrets; print(secrets.token_hex(32))"') do set "SYS_TOKEN=%%T"
    )
    if not defined SYS_TOKEN (
        echo [ERROR] Failed to create a System Panel access token.
        pause
        exit /b 1
    )

    echo SYSTEM_ACCESS_TOKEN=!SYS_TOKEN!>> "C:\tally_stock\.env"

    echo.
    echo   Token set. To finish pairing THIS browser/device, visit this
    echo   URL once while logged in as admin:
    echo.
    echo   https://superseatings.carxone.com/admin/system/authorize-device?token=!SYS_TOKEN!
    echo.
    echo   (Copy the URL above - you can paste it on your phone or laptop.)
    echo ============================================================
    echo.
    pause
)

echo [INFO] Starting app...
start "" "C:\tally_stock\.venv\Scripts\pythonw.exe" "C:\tally_stock\launcher.pyw"
timeout /t 5 /nobreak >nul

echo [OK] Update complete. App is restarting.
echo Check http://localhost:5000 in a few seconds.
pause
endlocal
