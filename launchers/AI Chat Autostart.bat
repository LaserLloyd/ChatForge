@echo off
setlocal EnableExtensions
title AI Chat - start at login

REM Adapted from StudioForge launchers/StudioForge Autostart.bat (MIT, LaserLloyd)
REM These launchers live in <repo>\launchers\; everything they need is one
REM level up. Resolve that once, as an absolute path, and work from there.
for %%I in ("%~dp0..") do set "REPO=%%~fI"
cd /d "%REPO%"

set "PY=%REPO%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo.
  echo   The virtual environment is missing:
  echo     %PY%
  echo.
  echo   Create it first:  py -3.12 -m uv sync --extra dev
  echo.
  pause
  exit /b 1
)

echo.
echo   AI Chat - start at login
echo   ========================
echo.
"%PY%" -m aichat autostart status
echo.
echo   [1] Enable  - start AI Chat hidden in the notification area at login
echo   [2] Disable - stop starting automatically
echo   [3] Cancel
echo.
set /p "CHOICE=Choose 1-3: "

if "%CHOICE%"=="1" goto :enable
if "%CHOICE%"=="2" goto :disable
goto :done

:enable
"%PY%" -m aichat autostart enable
goto :done

:disable
"%PY%" -m aichat autostart disable
goto :done

:done
echo.
REM Enabling writes a small hidden-launch script (AIChat.vbs) into your Startup
REM folder; it needs no administrator rights and option 2 removes it again.
"%PY%" -m aichat autostart status
echo.
pause
exit /b 0
