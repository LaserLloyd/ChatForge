@echo off
setlocal EnableExtensions
title ChatForge

REM Adapted from StudioForge launchers/StudioForge Tray.bat (MIT, LaserLloyd)
REM These launchers live in <repo>\launchers\; everything they need is one
REM level up. Resolve that once, as an absolute path, and work from there.
for %%I in ("%~dp0..") do set "REPO=%%~fI"
cd /d "%REPO%"

REM Settings, models, runtime and logs live in %LOCALAPPDATA%\ChatForge (set
REM CHATFORGE_HOME to move them). Nothing is written inside the checkout.

REM pythonw.exe has no console, so the tray app does not leave a black window
REM sitting behind the icon for as long as it runs. python.exe is the fallback
REM for a venv built without the windowless launcher.
set "PY=%REPO%\.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%REPO%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo.
  echo   The virtual environment is missing:
  echo     %REPO%\.venv\Scripts\
  echo.
  echo   Create it first:  py -3.12 -m uv sync --extra dev
  echo.
  pause
  exit /b 1
)

REM start /b so this console closes immediately instead of waiting for the app
REM to quit; the app keeps running in the notification area. A second launch
REM just brings the existing popup up.
start "" /b "%PY%" -m chatforge --show %*
exit /b 0
