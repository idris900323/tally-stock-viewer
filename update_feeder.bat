@echo off
rem Updates the Tally feeder machine: move to the latest origin/main, install ONLY the packages the
rem feeder needs (requirements-feeder.txt), check the feeder still imports, and restart the scheduled
rem task. If the package install or the import check fails, the previous commit is put back and the
rem task is restarted, so a failed update never leaves the feeder on half-updated code.
rem Run from anywhere; it works on the folder this file lives in.
rem (update_app.bat is for the legacy full-site PC only and refuses to run here.)
rem
rem Safe by design:
rem   * refuses unless .env says MACHINE_ROLE=feeder (so the web app can never be revived here)
rem   * aborts if any TRACKED file has local changes (nothing is touched)
rem   * moves to origin/main with "git checkout -B main origin/main", NOT "git pull", so it
rem     works even when this clone's history diverged from the rewritten history on GitHub.
rem     .env, data\ and logs\ are untracked/ignored and are never touched.
setlocal EnableExtensions

rem ---- 0. run from a temporary copy of this file. "git checkout" below replaces update_feeder.bat while
rem cmd is still reading it (cmd reads batch files by byte offset), which could run garbage half-way. ----
if /i "%~1"=="--from-copy" goto :main
set "SELF_COPY=%TEMP%\update_feeder_run_%RANDOM%.bat"
copy /y "%~f0" "%SELF_COPY%" >nul
if errorlevel 1 goto :no_copy
call "%SELF_COPY%" --from-copy "%~dp0"
set "RC=%ERRORLEVEL%"
del "%SELF_COPY%" >nul 2>&1
exit /b %RC%

:no_copy
echo [ERROR] Could not make a temporary copy of this script in %TEMP%. Nothing was changed.
pause
exit /b 1

:main
cd /d "%~2"

rem ---- 1. role check (plain batch: must work even on the OLD checkout) ----
rem Never prints .env (it holds the sync token). Requires EXACTLY ONE line starting with
rem MACHINE_ROLE, and that line must be MACHINE_ROLE=feeder (any case; trailing spaces ok).
rem Anything else - missing, duplicate, commented out, indented, a BOM on that line - refuses.
if not exist ".env" (
    echo [ERROR] No .env here. This script only runs on a feeder machine ^(MACHINE_ROLE=feeder^). Nothing was changed.
    pause
    exit /b 1
)
set "ROLE_LINES=0"
for /f %%N in ('%SystemRoot%\System32\findstr.exe /B /I /C:"MACHINE_ROLE" ".env" ^| %SystemRoot%\System32\find.exe /c /v ""') do set "ROLE_LINES=%%N"
if not "%ROLE_LINES%"=="1" (
    echo [ERROR] .env must have exactly one MACHINE_ROLE line; found %ROLE_LINES%. Nothing was changed.
    echo         Check with:  findstr /B /I MACHINE_ROLE .env
    pause
    exit /b 1
)
%SystemRoot%\System32\findstr.exe /B /I /R /C:"MACHINE_ROLE=feeder[ ]*$" ".env" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The MACHINE_ROLE line in .env is not exactly  MACHINE_ROLE=feeder  - nothing was changed.
    echo         Check with:  findstr /B /I MACHINE_ROLE .env
    pause
    exit /b 1
)

rem ---- 1b. names: this folder is wherever the .bat lives; the task name defaults to
rem TallyFeeder and can be set with a line  FEEDER_TASK_NAME=<name>  in .env ----
set "TASK_NAME=TallyFeeder"
for /f "usebackq tokens=1,* delims==" %%A in (".env") do if /i "%%A"=="FEEDER_TASK_NAME" set "TASK_NAME=%%B"
set TASK_NAME | %SystemRoot%\System32\findstr.exe /R /C:"^TASK_NAME=[A-Za-z0-9_. -][A-Za-z0-9_. -]*$" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] FEEDER_TASK_NAME in .env has characters other than letters, digits, space, _ . - ; nothing was changed.
    pause
    exit /b 1
)
echo [INFO] Folder: %CD%   Task: %TASK_NAME%
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found in %CD% - wrong folder? Nothing was changed.
    pause
    exit /b 1
)
call schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No scheduled task named "%TASK_NAME%" was found, so it could not be restarted. Nothing was changed.
    echo         Find the real name:  schtasks /Query /FO LIST ^| findstr /I "TaskName"
    echo         then add a line  FEEDER_TASK_NAME=<that name>  to .env and run this again.
    pause
    exit /b 1
)

rem ---- 2. refuse if tracked files were modified locally (staged, unstaged or deleted) ----
git update-index -q --refresh
git diff-index --quiet HEAD --
if errorlevel 1 (
    echo [ERROR] Tracked files have local changes - aborting, nothing was changed:
    git status --short --untracked-files=no
    echo         Undo them with  git checkout -- .  ^(or ask for help^) and run this again.
    pause
    exit /b 1
)

set "OLDHASH="
set "OLDFULL="
for /f %%H in ('git rev-parse --short HEAD') do set "OLDHASH=%%H"
for /f %%H in ('git rev-parse HEAD') do set "OLDFULL=%%H"
if not defined OLDFULL (
    echo [ERROR] Could not read the current commit - is this a git folder? Nothing was changed.
    pause
    exit /b 1
)
echo [INFO] Currently at %OLDHASH%.

rem ---- 3. fetch and move to origin/main (works with diverged history) ----
echo [INFO] Fetching latest code...
git fetch origin
if errorlevel 1 (
    echo [ERROR] git fetch failed. Check the internet connection and try again. Nothing was changed.
    pause
    exit /b 1
)
git checkout -B main origin/main
if errorlevel 1 (
    echo [ERROR] Could not move to origin/main. Nothing else was changed; the previous commit was %OLDHASH%.
    pause
    exit /b 1
)

echo [INFO] Updating packages ^(requirements-feeder.txt - only what the feeder needs^)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-feeder.txt
if errorlevel 1 (
    set "FAIL_REASON=pip could not install the packages in requirements-feeder.txt"
    goto :rollback
)

rem ---- 4. confirm the feeder still imports (uses its own log file; touches no database) ----
echo [INFO] Checking feeder.py still loads...
set "LOG_FILE=logs\import_check.log"
".venv\Scripts\python.exe" -c "import feeder" >nul 2>&1
if errorlevel 1 (
    set "LOG_FILE="
    set "FAIL_REASON=feeder.py no longer loads after the update"
    goto :rollback
)
set "LOG_FILE="

echo [INFO] Restarting the feeder task...
call schtasks /End /TN "%TASK_NAME%" >nul 2>&1
%SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul
call schtasks /Run /TN "%TASK_NAME%"
if errorlevel 1 (
    echo [ERROR] Could not start the "%TASK_NAME%" task. Was it registered? See FEEDER_SETUP.md, step 7.
    pause
    exit /b 1
)

set "NEWHASH="
for /f %%H in ('git rev-parse --short HEAD') do set "NEWHASH=%%H"
echo [OK] Feeder updated: %OLDHASH% -^> %NEWHASH% ^(now on origin/main^). Check logs\feeder.log.
pause
exit /b 0

rem ---- any failure AFTER the code was moved lands here: put the previous commit back and keep the feeder running ----
:rollback
echo.
echo [ROLLBACK] %FAIL_REASON%.
echo [ROLLBACK] Putting the previous code back ^(%OLDHASH%^)...
git checkout -q -B main %OLDFULL%
if errorlevel 1 (
    echo [CRITICAL] Could not put the previous code back automatically. Run this by hand:  git checkout -B main %OLDFULL%
    pause
    exit /b 3
)
echo [ROLLBACK] Restarting the feeder task so it keeps running the previous code...
call schtasks /End /TN "%TASK_NAME%" >nul 2>&1
%SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul
call schtasks /Run /TN "%TASK_NAME%"
if errorlevel 1 (
    echo [CRITICAL] The previous code is back, but the "%TASK_NAME%" task could not be started. Start it by hand:  schtasks /Run /TN "%TASK_NAME%"
    pause
    exit /b 3
)
echo [ROLLBACK] Done. Back on %OLDHASH%; the update was NOT applied. .env and data were not touched.
pause
exit /b 1
