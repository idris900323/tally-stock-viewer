@echo off
setlocal

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

REM STEP 3 - Copy project to C:\tally_stock\
echo Copying project to C:\tally_stock\
cd /d "%~dp0"
xcopy /E /I /Y "." "C:\tally_stock\" >nul
if %errorlevel% neq 0 (
    echo Failed to copy project files.
    pause
    exit /b 1
)

REM STEP 4 - Create virtual environment
python -m venv "C:\tally_stock\.venv"
if %errorlevel% neq 0 (
    echo Failed to create virtual environment.
    pause
    exit /b 1
)

REM STEP 5 - Install dependencies
"C:\tally_stock\.venv\Scripts\pip.exe" install --upgrade pip
"C:\tally_stock\.venv\Scripts\pip.exe" install -r "C:\tally_stock\requirements.txt"
"C:\tally_stock\.venv\Scripts\pip.exe" install pystray Pillow psutil

REM STEP 6 - Create logs and data directories
mkdir "C:\tally_stock\logs" 2>nul
mkdir "C:\tally_stock\data\S.S IMAGE" 2>nul

REM STEP 7 - Generate icon.png
"C:\tally_stock\.venv\Scripts\python.exe" "C:\tally_stock\generate_icon.py"

REM STEP 8 - Create Desktop shortcut Start Stock Viewer.bat
set START_BAT=%USERPROFILE%\Desktop\Start Stock Viewer.bat
echo @echo off > "%START_BAT%"
echo start "" "C:\tally_stock\.venv\Scripts\pythonw.exe" "C:\tally_stock\launcher.pyw" >> "%START_BAT%"

REM STEP 9 - Create Desktop shortcut Stop Stock Viewer.bat
set STOP_BAT=%USERPROFILE%\Desktop\Stop Stock Viewer.bat
(
    echo @echo off
    echo "C:\tally_stock\.venv\Scripts\python.exe" -c "import psutil, pathlib; pid_file = pathlib.Path(r'C:\tally_stock\app.pid'); if pid_file.exists():
    pid = int(pid_file.read_text());
    try:
        psutil.Process(pid).terminate();
        pid_file.unlink();
        print('Server stopped.')
    except Exception:
        print('Process not found.')
else:
    print('No running server found.')"
    echo pause
) > "%STOP_BAT%"

REM STEP 10 - Register auto-start in Windows Registry
REG ADD "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "TallyStockViewer" /t REG_SZ /d "\"C:\tally_stock\.venv\Scripts\pythonw.exe\" \"C:\tally_stock\launcher.pyw\"" /f

REM STEP 11 - Verify install
echo Installation complete.
echo Desktop button: Start Stock Viewer.bat
echo Auto-start: registered for Windows login
echo App URL: http://localhost:5000
echo Log file: C:\tally_stock\logs\app.log
pause
endlocal
