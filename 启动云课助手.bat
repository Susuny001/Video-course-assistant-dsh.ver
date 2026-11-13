@echo off
cd /d "%~dp0"
if exist "dist\YunKeAssistant_v6.exe" (
  start "" "dist\YunKeAssistant_v6.exe"
  exit /b
)
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run.py
) else (
  python run.py
)
