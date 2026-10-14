@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-za\Scripts\python.exe" (
  echo First-time setup is required. Run these commands from this folder:
  echo py -3 -m venv .venv-za
  echo .venv-za\Scripts\python.exe -m pip install -r requirements-za.txt
  pause
  exit /b 2
)
".venv-za\Scripts\python.exe" -s -B zatrade.py gui %*
if errorlevel 1 pause
