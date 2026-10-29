@echo off
:: ============================================================
:: schedule_ncmrwf.bat
:: Register a daily Windows Task Scheduler job that runs the
:: NCMRWF 9km pipeline every day at 07:30 IST (02:00 UTC).
:: Run this file ONCE as Administrator.
:: ============================================================

SET TASK_NAME=NCMRWF_9km_Daily_Pipeline
SET REPO=d:\WEATHER_FOR\wrf-weather-simulator
SET PYTHON=python

:: Remove existing task if it exists
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

:: Register new task
:: Runs daily at 02:00 UTC = 07:30 IST
schtasks /Create ^
  /TN "%TASK_NAME%" ^
  /TR "\"%PYTHON%\" \"%REPO%\ncmrwf_pipeline.py\" --cycle 00" ^
  /SC DAILY ^
  /ST 07:30 ^
  /RU "%USERNAME%" ^
  /RL HIGHEST ^
  /F

IF %ERRORLEVEL% EQU 0 (
    echo.
    echo [OK] Task "%TASK_NAME%" registered successfully.
    echo      It will run daily at 07:30 to fetch the 00Z NCMRWF forecast.
    echo      View / edit it in: Task Scheduler ^> Task Scheduler Library
) ELSE (
    echo [ERR] Failed to register task. Run this script as Administrator.
)
pause
