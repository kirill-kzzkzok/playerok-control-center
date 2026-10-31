@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
title Playerok Control Center
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run install.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -u -m app %*
if errorlevel 1 echo Playerok Control Center stopped with an error. See the safe diagnostic above and logs.
pause
