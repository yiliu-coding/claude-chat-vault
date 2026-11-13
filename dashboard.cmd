@echo off
rem Opens the Claude chat vault dashboard in the browser. Close this window to stop it.
cd /d "%~dp0"
python vault.py serve
if errorlevel 1 pause
