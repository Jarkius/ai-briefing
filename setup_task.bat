@echo off
REM ===========================================================
REM  setup_task.bat - registers the daily 08:00 AM Windows task
REM  Right-click this file and "Run as administrator".
REM ===========================================================

set TASKFOLDER=\ai
set TASKNAME=AI Briefing Daily
set TASKPATH=%TASKFOLDER%\%TASKNAME%

echo Registering scheduled task "%TASKPATH%" to run daily at 08:00 AM...
echo.

schtasks /Create ^
  /TN "%TASKPATH%" ^
  /TR "\"%~dp0run_briefing.bat\"" ^
  /SC DAILY ^
  /ST 08:00 ^
  /RL HIGHEST ^
  /F

echo.
if %ERRORLEVEL%==0 (
    echo Done. The task "%TASKNAME%" was created under the "%TASKFOLDER%" folder.
    echo It will run every day at 08:00 AM.
    echo You can run it once now with:  schtasks /Run /TN "%TASKPATH%"
) else (
    echo Something went wrong. Try running this file as administrator.
)
echo.
pause
