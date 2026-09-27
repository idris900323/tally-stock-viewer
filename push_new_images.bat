@echo off
rem Double-click this to push any newly-added images from data\S.S IMAGE to
rem Render and trigger a remote rescan. No manual PowerShell needed. See
rem scripts\push_new_images.py for details.
setlocal
cd /d "%~dp0"

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\push_new_images.py"

echo.
pause
endlocal
