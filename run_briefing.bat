@echo off
REM ===========================================================
REM  run_briefing.bat - runs the daily AI briefing script
REM  Logs each run to logs\briefing_YYYY-MM-DD.log
REM ===========================================================

REM Move to the folder this script lives in
cd /d "%~dp0"

REM Make sure a logs folder exists
if not exist "logs" mkdir "logs"

REM Build a date stamp (YYYY-MM-DD) that is locale-independent
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set STAMP=%%i

echo ============================================== >> "logs\briefing_%STAMP%.log"
echo Run started %DATE% %TIME% >> "logs\briefing_%STAMP%.log"

REM Requires the project venv created by setup.sh (or an equivalent manual
REM venv — see README Windows section for bootstrap steps). This runs the
REM new run.py orchestrator, not the legacy ai_briefing.py.
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv\Scripts\python.exe not found. Create the venv first ^(see README Windows section^): >> "logs\briefing_%STAMP%.log"
    echo   py -3.11 -m venv .venv >> "logs\briefing_%STAMP%.log"
    echo   .venv\Scripts\pip install -e . >> "logs\briefing_%STAMP%.log"
    exit /b 1
)

".venv\Scripts\python.exe" run.py >> "logs\briefing_%STAMP%.log" 2>&1
set RC=%ERRORLEVEL%

echo Run finished %DATE% %TIME% with exit code %RC% >> "logs\briefing_%STAMP%.log"

REM Propagate the Python exit code so Task Scheduler shows the real result
exit /b %RC%
