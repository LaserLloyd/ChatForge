@echo off
rem  No setlocal on purpose: see the last block.
title Uninstall ChatForge

rem  Double-click to remove ChatForge. Your settings, chats and models are KEPT unless you pass --purge.
rem  Options:  --purge  also delete %LOCALAPPDATA%\ChatForge (settings, chats, models, runtime) and saved API keys
rem            --yes    do not ask questions

set "HERE=%~dp0"
set "PS1="
if exist "%HERE%deploy\windows\Uninstall-ChatForge.ps1" set "PS1=%HERE%deploy\windows\Uninstall-ChatForge.ps1"
if not defined PS1 if exist "%HERE%Uninstall-ChatForge.ps1" set "PS1=%HERE%Uninstall-ChatForge.ps1"
if not defined PS1 (
  echo Cannot find Uninstall-ChatForge.ps1 next to this file.
  pause
  exit /b 1
)

set "PSARGS="
set "INSTARG="
set "ASKED=0"
rem  Started from <install folder>\uninstall\ (Settings > Installed apps): the install folder is the parent.
if exist "%HERE%..\install.json" for %%I in ("%HERE%..") do set "INSTARG= -InstallDir "%%~fI""
:parse
if "%~1"=="" goto parsed
if /I "%~1"=="--purge" (set "PSARGS=%PSARGS% -Purge" & set "ASKED=1")
if /I "%~1"=="--yes" (set "PSARGS=%PSARGS% -Yes" & set "ASKED=1")
if /I "%~1"=="--keep-data" set "ASKED=1"
shift
goto parse
:parsed

rem  Run a copy from %TEMP%, so the script can delete the folder it was started from.
set "RUN=%TEMP%\chatforge-uninstall-%RANDOM%.ps1"
copy /Y "%PS1%" "%RUN%" >nul

if "%ASKED%"=="0" (
  echo.
  echo This removes ChatForge from this PC. Your settings, chats and downloaded models are kept
  echo unless you also choose to delete them.
  echo.
  choice /C YN /N /M "Also delete your settings, chats, models and saved API keys? [y/N] "
  if errorlevel 2 (set "PSARGS=") else (set "PSARGS=-Purge")
)

rem  Everything below is ONE block, which cmd reads before running it: this file may live in the folder
rem  the uninstall deletes, and cmd must not go back to read a file that is gone. "(goto) 2>nul" leaves the
rem  batch context quietly first (a setlocal, or a plain "exit /b", would print "The system cannot find the
rem  path specified" once the file is gone). The exit code is therefore always 0; the script prints any failure.
(
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%RUN%"%INSTARG% %PSARGS%
  if errorlevel 1 echo The uninstall reported a problem: see the messages above.
  del "%RUN%" >nul 2>&1
  echo.
  pause
  (goto) 2>nul & exit /b 0
)
