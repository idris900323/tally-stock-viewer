rem SHOP_UPDATE.bat  version 2026-10-21.2  built on commit 885127b  -  save inside the feeder folder (the one with feeder.py and .env) and double-click
@echo off
set "SYS=%SystemRoot%\System32"
if /i "%~1"=="--run" goto :main
rem ---- stage 1 (single-line commands only, so it also works when a paste left LF-only line endings) ----
set "LAST=" & for /f "usebackq delims=" %%L in ("%~f0") do set "LAST=%%L"
if not "%LAST:~0,22%"=="rem END-OF-SHOP-UPDATE" echo The file looks cut off. Copy it again and paste the whole thing.& pause & exit /b 2
set "RUN=%TEMP%\shop_update_run_%RANDOM%.bat"
%SYS%\find.exe /v "" < "%~f0" > "%RUN%"
set "DONE=%TEMP%\shop_update_done_%RANDOM%.flag"
call "%RUN%" --run "%~dp0" "%DONE%"
set "RC=%ERRORLEVEL%" & del "%RUN%" >nul 2>&1
if not exist "%DONE%" echo The update stopped unexpectedly. Photograph this whole screen and touch nothing else.& pause
del "%DONE%" >nul 2>&1 & exit /b %RC%
:main
set "FOLDER=%~2" & set "DONE=%~3"
cd /d "%FOLDER%"
set "LOG=%FOLDER%SHOP_UPDATE_log.txt" & set "NAMES=%TEMP%\shop_names_%RANDOM%.txt"
set "SCHTASKS=%SYS%\schtasks.exe" & if defined SHOP_UPDATE_SCHTASKS set "SCHTASKS=%SHOP_UPDATE_SCHTASKS%"
set "WAITS=240" & if defined SHOP_UPDATE_WAIT_SECONDS set "WAITS=%SHOP_UPDATE_WAIT_SECONDS%"
set "WHY=" & set "MOVED=" & set "ENVCH=" & set "STOPPED=" & set "STASHED=" & set "BK=" & set "TASK=" & set "OLDFULL=" & set "NEEDROLE=" & set "MANY=" & set "BAD=" & set "MISSING="
> "%LOG%" echo ===== SHOP_UPDATE %DATE% %TIME% in %FOLDER% =====
call :say "Shop PC feeder update. Details: SHOP_UPDATE_log.txt. Step 1 of 6: checking git and this folder..."
git --version >nul 2>&1 || (set "WHY=Git is not installed on this PC (the git program was not found)." & goto :fail)
if not exist ".git" (set "WHY=This folder is not a git checkout: there is no .git folder here." & goto :fail)
git remote get-url origin >nul 2>&1 || (set "WHY=This git checkout has no remote called origin." & goto :fail)
if not exist "feeder.py" (set "WHY=feeder.py is not in this folder. Save SHOP_UPDATE.bat inside the feeder folder." & goto :fail)
if not exist ".venv\Scripts\python.exe" (set "WHY=.venv\Scripts\python.exe is missing in this folder." & goto :fail)
if not exist ".env" (set "WHY=The .env file is missing in this folder." & goto :fail)
call :say "Step 2 of 6: checking .env (it is never shown)..."
%SYS%\findstr.exe /I /C:"CLOUD_SYNC_TOKEN" ".env" >nul 2>&1 || (set "WHY=The .env file has no CLOUD_SYNC_TOKEN line or cannot be read, so it does not look right." & goto :fail)
set "N=0" & for /f %%N in ('%SYS%\findstr.exe /I /C:"MACHINE_ROLE" ".env" ^| %SYS%\find.exe /c /v ""') do set "N=%%N"
if "%N%"=="0" set "NEEDROLE=1"
if not "%N%"=="0" if not "%N%"=="1" (set "WHY=The .env file mentions MACHINE_ROLE %N% times; it must be 0 or exactly one clean line MACHINE_ROLE=feeder." & goto :fail)
if "%N%"=="1" %SYS%\findstr.exe /B /I /R /C:"MACHINE_ROLE=feeder[ ]*$" ".env" >nul 2>&1 || (set "WHY=The MACHINE_ROLE line in .env is not exactly MACHINE_ROLE=feeder (conflicting, commented out or extra text)." & goto :fail)
for /f "usebackq tokens=1,* delims==" %%A in (".env") do if /i "%%A"=="FEEDER_TASK_NAME" set "TASK=%%B"
set "TASKX=%TASK%" & if defined TASK set TASKX | %SYS%\findstr.exe /R /C:"^TASKX=[A-Za-z0-9_. -][A-Za-z0-9_. -]*$" >nul 2>&1 || (set "WHY=FEEDER_TASK_NAME in .env has odd characters." & goto :fail)
set "TURL=%TALLY_URL%" & for /f "usebackq tokens=1,* delims==" %%A in (".env") do if /i "%%A"=="TALLY_URL" set "TURL=%%B"
if not defined TURL set "TURL=http://localhost:9000"
%SYS%\WindowsPowerShell\v1.0\powershell.exe -NoProfile -Command "try { $u = [Uri]($env:TURL.Trim().Trim([char]34).Trim([char]39)); $c = New-Object Net.Sockets.TcpClient; $r = $c.BeginConnect($u.Host, $u.Port, $null, $null); if ($r.AsyncWaitHandle.WaitOne(4000) -and $c.Connected) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if errorlevel 1 call :say "Tally does not seem to be open. Open Tally with the company loaded, then press any key to continue anyway, or close this window." & pause
call :say "Step 3 of 6: backing up .env and the top-level files in data (never subfolders)..."
set "STAMP=" & for /f %%T in ('%SYS%\WindowsPowerShell\v1.0\powershell.exe -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss" 2^>nul') do set "STAMP=%%T"
if not defined STAMP set "STAMP=%RANDOM%%RANDOM%"
set "BK=%FOLDER%SHOP_UPDATE_BACKUP_%STAMP%" & set "BKN=1" & set "BKKB=0"
mkdir "%BK%\data" >nul 2>&1 & copy /y ".env" "%BK%\.env" >nul 2>&1 || (set "WHY=Could not back up .env." & goto :fail)
for %%E in (".env") do set /a BKKB=%%~zE/1024 & for %%F in ("data\*.json" "data\*.db" "data\*.flag") do call :bk "%%~F"
if defined BAD (set "WHY=%BAD%" & goto :fail)
call :say "Backup saved in %BK% : %BKN% files, about %BKKB% KB. Subfolders of data (such as S.S IMAGE) are not copied and never touched."
call :say "Step 4 of 6: finding the scheduled task that runs this feeder..." & if not defined TASK call :findtask
if defined MANY (set "WHY=More than one scheduled task runs a feeder from this folder. Add a line FEEDER_TASK_NAME=name to .env." & goto :fail)
if not defined TASK (set "MISSING=1" & set "WHY=Could not find a scheduled task that runs the feeder from this folder. Add a line FEEDER_TASK_NAME=name to .env." & goto :fail)
call "%SCHTASKS%" /Query /TN "%TASK%" >nul 2>&1 || (set "WHY=There is no scheduled task called %TASK%." & goto :fail)
call :say "Task: %TASK%. Step 5 of 6: updating (a few minutes)..."
set "GIT_TERMINAL_PROMPT=0" & set "GCM_INTERACTIVE=never"
git -c credential.helper= fetch origin >> "%LOG%" 2>&1 || (set "WHY=git fetch failed: no internet, or GitHub asked for a login." & goto :fail)
git update-index -q --refresh >nul 2>&1 & for /f %%H in ('git rev-parse HEAD') do set "OLDFULL=%%H"
for /f %%H in ('git rev-parse --short HEAD') do set "OLDHASH=%%H"
set "OLDBR=HEAD" & for /f %%H in ('git rev-parse --abbrev-ref HEAD') do set "OLDBR=%%H"
if not defined OLDFULL (set "WHY=Could not read the current git commit." & goto :fail)
git diff-index --quiet HEAD -- || call :stash
if defined NEEDROLE if not defined BAD call :addrole
if defined BAD (set "WHY=%BAD%" & goto :fail)
set "STOPPED=1"
call "%SCHTASKS%" /End /TN "%TASK%" >> "%LOG%" 2>&1 & %SYS%\ping.exe -n 4 127.0.0.1 >nul 2>&1
if exist "logs\feeder.log" move /y "logs\feeder.log" "logs\feeder.log.before_update_%STAMP%" >nul 2>&1 || (set "WHY=logs\feeder.log is still in use, so an old feeder is still running." & goto :fail)
set "MOVED=1"
git checkout -q -B main origin/main >> "%LOG%" 2>&1 || (set "WHY=Could not move to the latest code (origin/main)." & goto :fail)
if not exist "machine_role.py" (set "WHY=The new code has no machine_role.py." & goto :fail)
for /f %%H in ('git rev-parse --short HEAD') do set "NEWHASH=%%H"
call :say "Now at commit %NEWHASH% (was %OLDHASH%). Installing packages and checking the program..."
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-feeder.txt >> "%LOG%" 2>&1 || (set "WHY=pip could not install the packages in requirements-feeder.txt." & goto :fail)
set "LOG_FILE=logs\import_check.log" & ".venv\Scripts\python.exe" -c "import feeder" >> "%LOG%" 2>&1 || (set "LOG_FILE=" & set "WHY=The new feeder program does not load (import feeder failed)." & goto :fail)
set "LOG_FILE="
set "SHOP_PY=%CD%\.venv\Scripts\python.exe" & set "SHOP_OUT=%TEMP%\shop_so_%RANDOM%.txt" & set "SHOP_ERR=%TEMP%\shop_se_%RANDOM%.txt"
%SYS%\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Start-Process -FilePath $env:SHOP_PY -ArgumentList 'serve.py' -WorkingDirectory (Get-Location).Path -PassThru -WindowStyle Hidden -RedirectStandardOutput $env:SHOP_OUT -RedirectStandardError $env:SHOP_ERR; $h = $p.Handle; if (-not $p.WaitForExit(15000)) { Stop-Process -Id $p.Id -Force; exit 99 }; $p.WaitForExit(); exit $p.ExitCode" >> "%LOG%" 2>&1
set "RC3=%ERRORLEVEL%" & del "%SHOP_OUT%" "%SHOP_ERR%" >nul 2>&1
if not "%RC3%"=="3" (set "WHY=The safety guard did not stop serve.py (expected exit code 3, got %RC3%). The web app must never run on this PC." & goto :fail)
call :say "Step 6 of 6: starting the feeder and waiting up to %WAITS% seconds for it to push..."
call "%SCHTASKS%" /Run /TN "%TASK%" >> "%LOG%" 2>&1 || (set "WHY=Could not start the scheduled task %TASK%." & goto :fail)
set /a W=0
:wait
set "OK=1"
for %%P in ("machine role=feeder" "reason=startup" "Cloud sync push succeeded") do %SYS%\findstr.exe /C:%%P "logs\feeder.log" >nul 2>&1 || set "OK="
if not exist "logs\feeder.log" set "OK="
if defined OK goto :pass
if %W% GEQ %WAITS% (set "WHY=After %WAITS% seconds the new feeder.log did not show all of: machine role=feeder, reason=startup, Cloud sync push succeeded." & goto :fail)
<nul set /p "=." & %SYS%\ping.exe -n 6 127.0.0.1 >nul 2>&1 & set /a W+=5 & goto :wait
:pass
echo. & call :say "PASS: the new feeder is running and pushed to the site (commit %NEWHASH%)."
color 2F
echo. & echo     ####    ###    ####   #### & echo     #   #  #   #  #      # & echo     ####   #####   ###    ### & echo     #      #   #      #      # & echo     #      #   #  ####   ####
echo. & echo   The feeder was updated from %OLDHASH% to %NEWHASH% and is pushing stock to the site. Nothing else to do. & echo   Backup of .env and data files: %BK%
> "%DONE%" echo done
pause
exit /b 0
:fail
echo. & call :say "FAILED: %WHY%"
if defined MOVED if "%OLDBR%"=="HEAD" (git checkout -q %OLDFULL% >> "%LOG%" 2>&1 || set "BAD=the previous code could not be put back") else (git checkout -q -B %OLDBR% %OLDFULL% >> "%LOG%" 2>&1 || set "BAD=the previous code could not be put back")
if defined STASHED git -c user.name=shopupdate -c user.email=shopupdate@example.invalid stash pop -q >> "%LOG%" 2>&1
if defined ENVCH copy /y "%BK%\.env" ".env" >nul 2>&1 || set "BAD=.env could not be restored from the backup"
if defined STOPPED (call "%SCHTASKS%" /End /TN "%TASK%" >> "%LOG%" 2>&1 & %SYS%\ping.exe -n 4 127.0.0.1 >nul 2>&1 & call "%SCHTASKS%" /Run /TN "%TASK%" >> "%LOG%" 2>&1 || set "BAD=the feeder task could not be restarted")
color 4F
echo. & echo     #####   ###   #####  # & echo     #      #   #    #    # & echo     ####   #####    #    # & echo     #      #   #    #    # & echo     #      #   #  #####  #####
echo. & echo   WHAT WENT WRONG: %WHY%
if defined MISSING echo   Scheduled tasks that mention feeder or tally: & type "%NAMES%" & echo   Take a photo and send it to Idris.
if defined MOVED echo   The previous version was put back and the feeder was restarted. Your .env and data are safe.
if not defined MOVED if defined STOPPED echo   The feeder was restarted on the previous version. Your .env and data are safe.
if not defined MOVED if not defined STOPPED echo   Nothing was changed.
if defined BAD echo   BUT: %BAD%. Photograph this and say so.
echo   WHAT TO DO: take a PHOTO of this whole window, then close it. Do not touch anything else. Details: SHOP_UPDATE_log.txt in this folder.
if exist "logs\feeder.log" %SYS%\WindowsPowerShell\v1.0\powershell.exe -NoProfile -Command "Write-Host '--- last lines of logs\feeder.log ---'; Get-Content -Tail 6 'logs\feeder.log'" 2>nul
del "%NAMES%" >nul 2>&1 & > "%DONE%" echo done
pause
exit /b 1
:say
echo %~1
>> "%LOG%" echo [%TIME%] %~1
exit /b 0
:bk
copy /y "%~1" "%BK%\data\" >nul 2>&1 && (set /a BKN+=1 & set /a BKKB+=%~z1/1024) || set "BAD=Could not back up the data file %~nx1."
exit /b 0
:addrole
set "ENVCH=1"
>> ".env" echo.
>> ".env" echo MACHINE_ROLE=feeder
%SYS%\findstr.exe /B /I /R /C:"MACHINE_ROLE=feeder[ ]*$" ".env" >nul 2>&1 || set "BAD=Adding MACHINE_ROLE=feeder to .env did not work."
exit /b 0
:stash
git diff HEAD > "%BK%\local_changes.patch" 2>> "%LOG%"
git -c user.name=shopupdate -c user.email=shopupdate@example.invalid stash push -q >> "%LOG%" 2>&1 && set "STASHED=1"
if not defined STASHED set "BAD=Some program files had local edits that could not be set aside safely."
exit /b 0
:findtask
set "Q=%TEMP%\shop_q_%RANDOM%.txt" & set "P=%TEMP%\shop_p_%RANDOM%.txt" & set "M=%TEMP%\shop_m_%RANDOM%.txt"
call "%SCHTASKS%" /Query /V /FO CSV /NH > "%Q%" 2>> "%LOG%"
> "%P%" echo %CD%\
>> "%P%" echo %CD%"
%SYS%\findstr.exe /I /L /G:"%P%" "%Q%" 2>nul | %SYS%\findstr.exe /I /C:"feeder" > "%M%" 2>nul
for /f "usebackq tokens=2 delims=," %%A in ("%M%") do call :taketask "%%~A"
type nul > "%NAMES%.t" & if not defined TASK %SYS%\findstr.exe /I /C:"feeder" /C:"tally" "%Q%" > "%M%" 2>nul
if not defined TASK for /f "usebackq tokens=2 delims=," %%A in ("%M%") do >> "%NAMES%.t" echo     %%~A
%SYS%\sort.exe /unique "%NAMES%.t" /o "%NAMES%" & del "%Q%" "%P%" "%M%" "%NAMES%.t" >nul 2>&1
exit /b 0
:taketask
if not defined TASK set "TASK=%~1"
if /i not "%TASK%"=="%~1" set "MANY=1"
exit /b 0
rem END-OF-SHOP-UPDATE
