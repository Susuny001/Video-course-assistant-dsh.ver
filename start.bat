@echo off
cd /d "%~dp0"
if exist "dist\YunKeAssistant.exe" (
  start "" "dist\YunKeAssistant.exe"
  exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "run.py"
  exit /b
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "run.py"
  exit /b
)
python "run.py"
