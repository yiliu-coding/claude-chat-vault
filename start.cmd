@echo off
rem Claude chat vault: first run scans this PC and writes config.json, then opens the dashboard.
rem Keep this window open while you use the dashboard.
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH", then run start.cmd again.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(sys.version_info < (3, 8))" || (
  echo Python 3.8 or newer is needed.
  pause
  exit /b 1
)
%PY% vault.py start
if errorlevel 1 pause
