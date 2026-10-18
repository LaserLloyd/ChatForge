@echo off
setlocal EnableExtensions
title Install ChatForge

rem  Double-click to install or upgrade ChatForge for the current user (no administrator rights).
rem  The work is done by deploy\windows\Install-ChatForge.ps1; this file only finds it and passes your options on.
rem
rem  Options (all optional):  --yes  start at sign-in without asking      --no-autostart  never register sign-in start
rem                           --no-launch  do not open ChatForge at the end   --no-shortcuts
rem                           --install-dir "D:\Apps\ChatForge"   --ref v0.2.0  (which version to download when no app is beside this file)
rem  Where the app comes from: the "app" folder next to this file (release zip), else this file's own folder
rem  (a git clone), else it is downloaded from GitHub.

set "HERE=%~dp0"
set "PS1=%HERE%deploy\windows\Install-ChatForge.ps1"
set "SRC="
if exist "%HERE%app\pyproject.toml" for %%I in ("%HERE%app") do set "SRC=%%~fI"
if not defined SRC if exist "%HERE%pyproject.toml" for %%I in ("%HERE%.") do set "SRC=%%~fI"

set "PSARGS="
:parse
if "%~1"=="" goto parsed
if /I "%~1"=="--yes" set "PSARGS=%PSARGS% -Yes"
if /I "%~1"=="--no-autostart" set "PSARGS=%PSARGS% -NoAutostart"
if /I "%~1"=="--no-launch" set "PSARGS=%PSARGS% -NoLaunch"
if /I "%~1"=="--no-shortcuts" set "PSARGS=%PSARGS% -NoShortcuts"
if /I "%~1"=="--install-dir" (set "PSARGS=%PSARGS% -InstallDir "%~2"" & shift)
if /I "%~1"=="--ref" (set "PSARGS=%PSARGS% -Ref "%~2"" & shift)
shift
goto parse
:parsed

if not exist "%PS1%" (
  rem  Only this one file was downloaded: fetch the installer script too.
  set "PS1=%TEMP%\chatforge-installer\Install-ChatForge.ps1"
  if not exist "%TEMP%\chatforge-installer" mkdir "%TEMP%\chatforge-installer"
  echo Downloading the ChatForge installer script ...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/LaserLloyd/ChatForge/main/deploy/windows/Install-ChatForge.ps1' -OutFile (Join-Path $env:TEMP 'chatforge-installer\Install-ChatForge.ps1')"
  if errorlevel 1 (
    echo.
    echo Could not download the installer script. Check the internet connection and try again.
    pause
    exit /b 1
  )
)

if defined SRC (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -SourceDir "%SRC%" %PSARGS%
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %PSARGS%
)
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo The installer stopped with an error ^(code %RC%^). The messages above say why.
pause
exit /b %RC%
