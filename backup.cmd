@echo off
rem Runs one Claude chat vault backup (only new and changed files are copied).
cd /d "%~dp0"
python vault.py backup
pause
