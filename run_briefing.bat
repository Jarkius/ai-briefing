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

REM Prefer the Python launcher; fall back to python on PATH
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 ai_briefing.py >> "logs\briefing_%STAMP%.log" 2>&1
) else (
    python ai_briefing.py >> "logs\briefing_%STAMP%.log" 2>&1
)
set RC=%ERRORLEVEL%

echo Run finished %DATE% %TIME% with exit code %RC% >> "logs\briefing_%STAMP%.log"

REM Propagate the Python exit code so Task Scheduler shows the real result
exit /b %RC%
