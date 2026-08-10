@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "BRIDGE_HOST=127.0.0.1"
set "BRIDGE_PORT=8765"
set "BRIDGE_URL=http://127.0.0.1:8765"
set "VOICEBOX_URL=http://127.0.0.1:17493"
set "VOICEBOX_BASE_URL=%VOICEBOX_URL%"
set "VOICEBOX_EXE=%ProgramFiles%\Voicebox\voicebox.exe"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"

rem Reuse an already-running bridge.
powershell.exe -NoProfile -Command "try { $null = Invoke-RestMethod -Uri '%BRIDGE_URL%/api/engine/status' -TimeoutSec 2; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  if /I "%~1"=="--check" (
    powershell.exe -NoProfile -Command "try { $status = Invoke-RestMethod -Uri '%BRIDGE_URL%/api/youtube/status' -TimeoutSec 2; if ($status.ready) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
    if errorlevel 1 (
      echo Startup check failed: the running bridge does not have a ready YouTube pipeline.
      exit /b 1
    )
    echo Startup check passed: the bridge is already running.
    exit /b 0
  )
  echo VoiceBox STS Bridge is already running.
  start "" "%BRIDGE_URL%"
  exit /b 0
)

rem Prefer a dedicated bridge environment, then fall back to Python on PATH.
if exist "%CD%\.venv\Scripts\python.exe" (
  set "BRIDGE_PYTHON=%CD%\.venv\Scripts\python.exe"
) else (
  set "BRIDGE_PYTHON=python"
)

"%BRIDGE_PYTHON%" -c "import fastapi, uvicorn, yt_dlp" >nul 2>&1
if errorlevel 1 (
  echo.
  echo The bridge Python dependencies are not installed.
  echo From this folder, run:
  echo   python -m venv .venv
  echo   .venv\Scripts\python -m pip install -e ".[dev]"
  echo.
  pause
  exit /b 1
)

if /I "%~1"=="--check" (
  "%BRIDGE_PYTHON%" -m voicebox_sts_bridge engine-status >nul
  if errorlevel 1 (
    echo Startup check failed: the OpenVoice engine is not ready.
    exit /b 1
  )
  "%BRIDGE_PYTHON%" -c "import sys; from voicebox_sts_bridge.youtube_service import YouTubeJobService; sys.exit(0 if YouTubeJobService('data', None, None).status()['ready'] else 1)" >nul
  if errorlevel 1 (
    echo Startup check failed: patched yt-dlp, FFmpeg, or FFprobe is unavailable.
    exit /b 1
  )
  echo Startup check passed: bridge and engine dependencies are ready.
  exit /b 0
)

rem VoiceBox is an external local service. Start it only when its API is unavailable.
powershell.exe -NoProfile -Command "try { $null = Invoke-RestMethod -Uri '%VOICEBOX_URL%/health' -TimeoutSec 2; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
  if exist "%VOICEBOX_EXE%" (
    echo Starting VoiceBox...
    start "VoiceBox" "%VOICEBOX_EXE%"
    for /L %%I in (1,1,30) do (
      powershell.exe -NoProfile -Command "try { $null = Invoke-RestMethod -Uri '%VOICEBOX_URL%/health' -TimeoutSec 2; exit 0 } catch { exit 1 }" >nul 2>&1
      if not errorlevel 1 goto voicebox_ready
      timeout /t 1 /nobreak >nul
    )
    echo Warning: VoiceBox did not become healthy within 30 seconds.
    echo The bridge will still open and display the current service status.
  ) else (
    echo Warning: VoiceBox is not running and was not found at:
    echo   %VOICEBOX_EXE%
  )
)

:voicebox_ready
echo Starting VoiceBox STS Bridge at %BRIDGE_URL% ...
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%BRIDGE_URL%'"
"%BRIDGE_PYTHON%" -m voicebox_sts_bridge serve
set "BRIDGE_EXIT=%ERRORLEVEL%"

if not "%BRIDGE_EXIT%"=="0" (
  echo.
  echo VoiceBox STS Bridge exited with code %BRIDGE_EXIT%.
  pause
)
exit /b %BRIDGE_EXIT%
