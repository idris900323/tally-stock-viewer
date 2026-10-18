@echo off
rem Push new design images to the live site. Double-click this file.
rem The window always stays open at the end so you can read the result.
rem First time? Read SETUP_GUIDE.md in this folder. Settings live OUTSIDE the git
rem folder (%LOCALAPPDATA%\SuperSeatings\push_images), so updating the code is safe.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [STOPPED] Python was not found on this PC. Install Python 3 from python.org
    echo           ^(tick "Add Python to PATH"^) and double-click this file again.
    goto :end
)

%PY% push_images.py %*
set "CODE=%errorlevel%"
echo.
if "%CODE%"=="0" (
    echo ---- Finished OK. ----
) else if "%CODE%"=="2" (
    echo ---- Stopped safely. NOTHING was changed on the server. Read the message above. ----
) else (
    echo ---- Finished with problems ^(code %CODE%^). Read the messages above. ----
)

:end
echo.
pause
endlocal
