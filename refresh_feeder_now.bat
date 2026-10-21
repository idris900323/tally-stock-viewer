@echo off
rem Asks the feeder for an immediate full refresh (car list + hierarchy + stock) by creating
rem refresh_now.flag next to feeder.py. The feeder notices it within a couple of seconds, runs
rem the refresh and deletes the file. Safe to double-click more than once.
cd /d "%~dp0"
echo Refresh requested at %date% %time%> refresh_now.flag
if errorlevel 1 (
    echo [ERROR] Could not create refresh_now.flag in %~dp0
    pause
    exit /b 1
)
echo Refresh requested; it will appear on the site in a few minutes
timeout /t 5 >nul
