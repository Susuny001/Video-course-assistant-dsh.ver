@echo off
cd /d "%~dp0"
set ROOT=%~dp0
set PY=%ROOT%.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r "%ROOT%requirements.txt" pyinstaller
"%PY%" -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name YunKeAssistant ^
  --specpath "%ROOT%build" ^
  --workpath "%ROOT%build\work" ^
  --distpath "%ROOT%dist" ^
  --icon "%ROOT%assets\icon.ico" ^
  --add-data "%ROOT%web;web" ^
  --collect-submodules webview ^
  --collect-submodules uvicorn ^
  --hidden-import webview.platforms.edgechromium ^
  "%ROOT%run.py"
echo.
echo Build finished: dist\YunKeAssistant.exe
pause
