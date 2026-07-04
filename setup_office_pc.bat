@echo off
setlocal

set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

REM STEP 1 - Check Administrator
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Administrator privileges are required.
    pause
    exit /b 1
)

REM STEP 2 - Check Python 3.8+
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python is not installed or not on PATH.
    echo Download Python 3.8+ from https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=2 delims=[.]" %%A in ('python --version 2^>^&1') do set PYVER=%%A
for /f "tokens=3 delims=. " %%A in ('python --version 2^>^&1') do set PYVERPATCH=%%A
if %PYVER% lss 8 (
    echo Python 3.8 or newer is required.
    echo Download Python 3.8+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM STEP 3 - Create virtual environment in the project folder
python -m venv "%INSTALL_DIR%\.venv"
if %errorlevel% neq 0 (
    echo Failed to create virtual environment.
    pause
    exit /b 1
)

REM STEP 4 - Install dependencies
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install --upgrade pip
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install -r "%INSTALL_DIR%\requirements.txt"
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install pystray Pillow psutil waitress

REM STEP 5 - Create logs and data directories
mkdir "%INSTALL_DIR%\logs" 2>nul
mkdir "%INSTALL_DIR%\data\S.S IMAGE" 2>nul

REM STEP 6 - Generate icon.png
"%INSTALL_DIR%\.venv\Scripts\python.exe" "%INSTALL_DIR%\generate_icon.py"

REM STEP 7 - Create Desktop shortcut Start Stock Viewer.bat
set START_BAT=%USERPROFILE%\Desktop\Start Stock Viewer.bat
echo @echo off > "%START_BAT%"
echo start "" "%INSTALL_DIR%\.venv\Scripts\pythonw.exe" "%INSTALL_DIR%\launcher.pyw" >> "%START_BAT%"

REM STEP 8 - Create Desktop shortcut Stop Stock Viewer.bat
set STOP_BAT=%USERPROFILE%\Desktop\Stop Stock Viewer.bat
(
    echo @echo off
    echo if exist "%INSTALL_DIR%\app.pid" ^(
    echo     for /f "usebackq delims=" %%%%P in ("%INSTALL_DIR%\app.pid") do taskkill /PID %%%%P /T /F ^>nul 2^>^&1
    echo     del "%INSTALL_DIR%\app.pid" ^>nul 2^>^&1
    echo     echo Server stopped.
    echo ^) else ^(
    echo     echo No running server found.
    echo ^)
    echo pause
) > "%STOP_BAT%"

REM STEP 9 - Register auto-start in Windows Registry
REG ADD "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "TallyStockViewer" /t REG_SZ /d "\"%INSTALL_DIR%\.venv\Scripts\pythonw.exe\" \"%INSTALL_DIR%\launcher.pyw\"" /f

REM STEP 10 - Verify install
echo Installation complete.
echo Desktop button: Start Stock Viewer.bat
echo Auto-start: registered for Windows login
echo App URL: http://localhost:5000
echo Log file: %INSTALL_DIR%\logs\app.log
echo Project folder: %INSTALL_DIR%
pause
endlocal
