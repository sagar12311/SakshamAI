@echo off
setlocal
title Saksham Public Beta
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Start-Public-Beta.ps1"
if errorlevel 1 (
  echo.
  echo Startup failed. Read the message above. No database was deleted.
  pause
)
