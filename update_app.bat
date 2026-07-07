@echo off
setlocal

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

echo [INFO] Starting app...
start "" "C:\tally_stock\.venv\Scripts\pythonw.exe" "C:\tally_stock\launcher.pyw"
timeout /t 5 /nobreak >nul

echo [OK] Update complete. App is restarting.
echo Check http://localhost:5000 in a few seconds.
pause
endlocal
