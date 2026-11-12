@echo off
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
  python -m venv .venv
)
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt pyinstaller
"%PY%" -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name YunKeAssistant ^
  --icon assets\icon.ico ^
  --add-data "web;web" ^
  --collect-submodules webview ^
  --collect-submodules uvicorn ^
  --hidden-import webview.platforms.edgechromium ^
  run.py
echo.
echo Build finished: dist\YunKeAssistant.exe
pause
