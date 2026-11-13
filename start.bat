@echo off
cd /d "%~dp0"
if exist "dist\YunKeAssistant_v8.exe" (
  taskkill /F /T /IM YunKeAssistant_v8.exe >nul 2>nul
  taskkill /F /T /IM YunKeAssistant_v7.exe >nul 2>nul
  taskkill /F /T /IM YunKeAssistant.exe >nul 2>nul
  start "" "dist\YunKeAssistant_v8.exe"
  exit /b
)
if exist "dist\YunKeAssistant_v7.exe" (
  taskkill /F /T /IM YunKeAssistant_v7.exe >nul 2>nul
  taskkill /F /T /IM YunKeAssistant.exe >nul 2>nul
  start "" "dist\YunKeAssistant_v7.exe"
  exit /b
)
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
