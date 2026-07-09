@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "TARGET_DIR=C:\tally_stock"
set "VENV_DIR=%TARGET_DIR%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "PYTHONW_EXE=%VENV_DIR%\Scripts\pythonw.exe"
set "PIP_EXE=%VENV_DIR%\Scripts\pip.exe"
set "ENV_FILE=%TARGET_DIR%\.env"
set "LOG_DIR=%TARGET_DIR%\logs"
set "DATA_DIR=%TARGET_DIR%\data"
set "IMAGE_DIR=%TARGET_DIR%\data\S.S IMAGE"
set "CLOUDFLARED_DIR=%USERPROFILE%\.cloudflared"
set "CLOUDFLARED_EXE=%TARGET_DIR%\cloudflared.exe"
set "DESKTOP_DIR=%USERPROFILE%\Desktop"
set "PUBLIC_URL="
set "TUNNEL_ID="

call :section1
call :section2
call :section3
call :section4
call :section5
call :section6
call :section7
exit /b 0

:section1
echo.
echo =====================================================
echo   SECTION 1 - Prerequisites check
echo =====================================================
net session >nul 2>&1
if errorlevel 1 call :fail Administrator privileges are required. Right-click and run this script as Administrator.

python --version >nul 2>&1
if errorlevel 1 call :fail Python is not installed or not on PATH.
for /f "delims=" %%V in ('python --version 2^>^&1') do set "PYTHON_VERSION=%%V"
echo Found %PYTHON_VERSION%

git --version >nul 2>&1
if errorlevel 1 call :fail Git is not installed or not on PATH.
for /f "delims=" %%V in ('git --version 2^>^&1') do set "GIT_VERSION=%%V"
echo Found %GIT_VERSION%
exit /b 0

:section2
echo.
echo =====================================================
echo   SECTION 2 - Copy project and create venv
echo =====================================================
xcopy "%SCRIPT_DIR%\*" "%TARGET_DIR%\" /E /I /H /Y /K /C >nul
if errorlevel 1 call :fail Failed to copy project files to %TARGET_DIR%.

python -m venv "%VENV_DIR%"
if errorlevel 1 call :fail Failed to create virtual environment in %VENV_DIR%.

"%PIP_EXE%" install -r "%TARGET_DIR%\requirements.txt"
if errorlevel 1 call :fail Failed to install requirements.txt dependencies.
"%PIP_EXE%" install pystray Pillow psutil
if errorlevel 1 call :fail Failed to install pystray, Pillow, or psutil.

mkdir "%LOG_DIR%" 2>nul
mkdir "%DATA_DIR%" 2>nul
mkdir "%IMAGE_DIR%" 2>nul
exit /b 0

:section3
echo.
echo =====================================================
echo   SECTION 3 - Interactive .env creation
echo =====================================================
set "TALLY_PORT="
set /p "TALLY_PORT=Enter Tally port (default 9000, press Enter to keep):"
if not defined TALLY_PORT set "TALLY_PORT=9000"

set "FLASK_SECRET_KEY="
set /p "FLASK_SECRET_KEY=Enter a secret key (press Enter to auto-generate):"
if not defined FLASK_SECRET_KEY (
    for /f "delims=" %%S in ('python -c "import secrets; print(secrets.token_hex(32))"') do set "FLASK_SECRET_KEY=%%S"
)
if not defined FLASK_SECRET_KEY call :fail Failed to create a Flask secret key.

(
    echo TALLY_URL=http://localhost:%TALLY_PORT%
    echo FLASK_SECRET_KEY=%FLASK_SECRET_KEY%
    echo FLASK_DEBUG=0
    echo SESSION_COOKIE_SECURE=0
    echo DB_PATH=C:\tally_stock\data\mappings.db
    echo LOG_FILE=C:\tally_stock\logs\app.log
    echo IMAGE_SCAN_ROOT=C:\tally_stock\data\S.S IMAGE
) > "%ENV_FILE%"
if errorlevel 1 call :fail Failed to write %ENV_FILE%.

echo .env created at C:\tally_stock\.env
exit /b 0

:section4
echo.
echo =====================================================
echo   SECTION 4 - Cloudflare tunnel setup (optional)
echo =====================================================
echo --- Cloudflare Tunnel Setup (optional, press Enter to skip each step) ---
set "SETUP_TUNNEL="
set /p "SETUP_TUNNEL=Do you want to set up superseatings.carxone.com tunnel now? (Y/N):"
if /I not "%SETUP_TUNNEL%"=="Y" exit /b 0

echo.
echo Step A - Download cloudflared
mkdir "%CLOUDFLARED_DIR%" 2>nul
echo Download cloudflared-windows-amd64.exe and rename it to cloudflared.exe.
echo Place it in C:\tally_stock\
set /p "CLOUDFLARED_READY=Press Enter when cloudflared.exe is in C:\tally_stock\:"
if not exist "%CLOUDFLARED_EXE%" call :fail cloudflared.exe was not found at %CLOUDFLARED_EXE%.

echo.
echo Step B - Login
"%CLOUDFLARED_EXE%" tunnel login
if errorlevel 1 call :fail cloudflared tunnel login failed.
set /p "CLOUDFLARED_LOGIN_OK=Press Enter after you have authorized in the browser:"

echo.
echo Step C - Create tunnel
"%CLOUDFLARED_EXE%" tunnel create tally-stock
if errorlevel 1 call :fail cloudflared tunnel create tally-stock failed.
set /p "TUNNEL_ID=Copy the tunnel ID shown above and paste it here:"
if not defined TUNNEL_ID call :fail No tunnel ID was provided.

echo.
echo Step D - Write config.yml
if not exist "%CLOUDFLARED_DIR%" mkdir "%CLOUDFLARED_DIR%" 2>nul
(
    echo tunnel: %TUNNEL_ID%
    echo credentials-file: "%CLOUDFLARED_DIR%\%TUNNEL_ID%.json"
    echo ingress:
    echo   - hostname: superseatings.carxone.com
    echo     service: http://localhost:5000
    echo   - service: http_status:404
) > "%CLOUDFLARED_DIR%\config.yml"
if errorlevel 1 call :fail Failed to write %CLOUDFLARED_DIR%\config.yml.

echo.
echo Step E - Add DNS CNAME
echo Now go to Cloudflare dashboard -^> carxone.com -^> DNS -^> Add record:
echo   Type: CNAME
echo   Name: superseatings
echo   Target: %TUNNEL_ID%.cfargotunnel.com
echo   Proxy: Proxied (orange cloud)
set /p "DNS_DONE=Press Enter when DNS record is saved:"

set "PUBLIC_URL=https://superseatings.carxone.com"
echo.
echo Step F - Test tunnel
start "" "%PYTHONW_EXE%" "%TARGET_DIR%\serve.py"
timeout /t 8 /nobreak >nul
"%CLOUDFLARED_EXE%" tunnel run tally-stock
if errorlevel 1 call :fail cloudflared tunnel run tally-stock failed.
echo Tunnel running. Test https://superseatings.carxone.com in your browser.
echo Press Ctrl+C here when done testing.
exit /b 0

:section5
echo.
echo =====================================================
echo   SECTION 5 - Git remote setup (optional)
echo =====================================================
set "SETUP_GIT="
set /p "SETUP_GIT=Do you want to connect to a GitHub repo for updates? (Y/N):"
if /I not "%SETUP_GIT%"=="Y" exit /b 0

set "REPO_URL="
set /p "REPO_URL=Paste your GitHub repo URL (e.g. https://github.com/you/repo.git):"
if not defined REPO_URL call :fail No repository URL was provided.

cd /d "%TARGET_DIR%"
if errorlevel 1 call :fail Failed to change directory to %TARGET_DIR%.

git init
if errorlevel 1 call :fail git init failed.
git remote add origin "%REPO_URL%" 2>nul
git remote set-url origin "%REPO_URL%" 2>nul

git fetch origin
if errorlevel 1 call :fail git fetch origin failed.
git checkout main
if errorlevel 1 (
    git checkout -B main origin/main
    if errorlevel 1 call :fail Failed to check out main from origin/main.
)
echo Git connected. Use update_app.bat to pull future updates.
exit /b 0

:section6
echo.
echo =====================================================
echo   SECTION 6 - Desktop shortcuts and autostart
echo =====================================================
set "START_BAT=%DESKTOP_DIR%\Start Stock Viewer.bat"
(
    echo @echo off
    echo start "" "%PYTHONW_EXE%" "%TARGET_DIR%\launcher.pyw"
) > "%START_BAT%"
if errorlevel 1 call :fail Failed to create %START_BAT%.

set "STOP_BAT=%DESKTOP_DIR%\Stop Stock Viewer.bat"
(
    echo @echo off
    echo if exist "%TARGET_DIR%\app.pid" ^(
    echo     for /f "usebackq delims=" %%%%P in ^("%TARGET_DIR%\app.pid"^) do taskkill /PID %%%%P /T /F ^>nul 2^>^&1
    echo     del "%TARGET_DIR%\app.pid" ^>nul 2^>^&1
    echo     echo Server stopped.
    echo ^) else ^(
    echo     echo No running server found.
    echo ^)
    echo pause
) > "%STOP_BAT%"
if errorlevel 1 call :fail Failed to create %STOP_BAT%.

REG ADD "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "TallyStockViewer" /t REG_SZ /d "\"%PYTHONW_EXE%\" \"%TARGET_DIR%\launcher.pyw\"" /f
if errorlevel 1 call :fail Failed to register TallyStockViewer autostart.

"%PYTHON_EXE%" "%TARGET_DIR%\generate_icon.py"
if errorlevel 1 call :fail Failed to generate icon.png.
exit /b 0

:section7
echo.
echo =====================================================
echo   SETUP COMPLETE
echo =====================================================
echo   App URL:     http://localhost:5000
if defined PUBLIC_URL echo   Public URL:  %PUBLIC_URL%
echo   Logs:        C:\tally_stock\logs\app.log
echo   Start:       Double-click "Start Stock Viewer" on Desktop
echo   Update code:  Double-click update_app.bat
echo =====================================================
echo Press any key to launch the app now...
pause >nul
start "" "%PYTHONW_EXE%" "%TARGET_DIR%\launcher.pyw"
exit /b 0

:fail
echo.
echo [ERROR] %*
pause
exit /b 1
