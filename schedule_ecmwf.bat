@echo off
:: ============================================================
:: schedule_ecmwf.bat
:: Register a daily Windows Task Scheduler job that runs the
:: ECMWF IFS pipeline every day at 09:00 IST (03:30 UTC).
:: 00Z run is published ~4-6 h after init, so 09:00 IST is safe.
:: Run this file ONCE as Administrator.
:: ============================================================

SET TASK_NAME=ECMWF_IFS_Daily_Pipeline
SET REPO=d:\WEATHER_FOR\wrf-weather-simulator
SET PYTHON=python

:: Remove old task if present
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

:: Register daily task at 09:00 IST
schtasks /Create ^
  /TN "%TASK_NAME%" ^
  /TR "\"%PYTHON%\" \"%REPO%\ecmwf_pipeline.py\" --source opendata --cycle 00" ^
  /SC DAILY ^
  /ST 09:00 ^
  /RU "%USERNAME%" ^
  /RL HIGHEST ^
  /F

IF %ERRORLEVEL% EQU 0 (
    echo.
    echo [OK] Task "%TASK_NAME%" registered.
    echo      Runs daily at 09:00 IST to fetch ECMWF IFS 00Z forecast.
    echo      No account or API key required!
    echo.
    echo      To also schedule the 12Z run at 21:00 IST, run:
    echo      schtasks /Create /TN "ECMWF_IFS_12Z" /TR "python %REPO%\ecmwf_pipeline.py --cycle 12" /SC DAILY /ST 21:00 /RU %USERNAME% /RL HIGHEST /F
) ELSE (
    echo [ERR] Failed. Run this script as Administrator.
)
pause

