@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  echo Create and populate .venv first: see README.md.
  exit /b 1
)

".venv\Scripts\python.exe" -m pip show pyinstaller >nul 2>&1 || ".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m pip show pillow >nul 2>&1 || ".venv\Scripts\python.exe" -m pip install pillow
".venv\Scripts\python.exe" -m pip show pystray >nul 2>&1 || ".venv\Scripts\python.exe" -m pip install pystray

".venv\Scripts\python.exe" build_assets\make_icon.py

".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean ^
  --onefile --windowed ^
  --name VoiceBoxBridge ^
  --icon build_assets\icon.ico ^
  --paths src ^
  --add-data "src\voicebox_sts_bridge\static;voicebox_sts_bridge\static" ^
  --add-data "src\voicebox_sts_bridge\openvoice_worker.py;voicebox_sts_bridge" ^
  --add-data "build_assets\icon.ico;." ^
  --collect-all uvicorn ^
  --collect-all pystray ^
  --hidden-import voicebox_sts_bridge.api ^
  --hidden-import PIL._tkinter_finder ^
  build_assets\launcher.py

copy /Y "dist\VoiceBoxBridge.exe" "VoiceBoxBridge.exe" >nul

echo.
echo Built: VoiceBoxBridge.exe (project root)
echo Keep it here, next to .envs\ and start-bridge.bat - it locates the
echo isolated OpenVoice environment relative to its own location.
echo Runs with no console window; use the system-tray icon to open the
echo bridge, view its log file, or quit.
