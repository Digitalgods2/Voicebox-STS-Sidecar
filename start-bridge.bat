@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "BRIDGE_HOST=127.0.0.1"
set "BRIDGE_PORT=8765"
set "BRIDGE_URL=http://127.0.0.1:8765"
set "BRIDGE_REQUIRED_FEATURE=youtube-source-cache-v1"
set "VOICEBOX_URL=http://127.0.0.1:17493"
set "VOICEBOX_BASE_URL=%VOICEBOX_URL%"
set "VOICEBOX_EXE=%ProgramFiles%\Voicebox\voicebox.exe"
set "PYTHONPATH=%CD%\src"

rem Reuse only a backend that advertises the API contract required by this UI.
rem Exit codes: 0 = compatible bridge, 10 = stale bridge from this project,
rem 11 = another service owns the port, 20 = the port is available.
powershell.exe -NoProfile -Command "$expected = [IO.Path]::GetFullPath('%CD%').TrimEnd('\'); try { $version = Invoke-RestMethod -Uri '%BRIDGE_URL%/api/version' -TimeoutSec 2; if (@($version.features) -contains '%BRIDGE_REQUIRED_FEATURE%') { exit 0 } } catch {}; try { $status = Invoke-RestMethod -Uri '%BRIDGE_URL%/api/engine/status' -TimeoutSec 2; $actual = [IO.Path]::GetFullPath([string]$status.project_root).TrimEnd('\'); if ([string]::Equals($actual, $expected, [StringComparison]::OrdinalIgnoreCase)) { exit 10 }; exit 11 } catch {}; $client = [Net.Sockets.TcpClient]::new(); try { $client.Connect('127.0.0.1', %BRIDGE_PORT%); exit 11 } catch { exit 20 } finally { $client.Dispose() }" >nul 2>&1
set "BRIDGE_INSTANCE_STATE=%ERRORLEVEL%"

if "%BRIDGE_INSTANCE_STATE%"=="0" goto bridge_running

if "%BRIDGE_INSTANCE_STATE%"=="11" (
  echo.
  echo Startup failed: port %BRIDGE_PORT% is owned by another application.
  echo Close that application or configure a different BRIDGE_PORT.
  if /I "%~1"=="--check" exit /b 1
  pause
  exit /b 1
)

if "%BRIDGE_INSTANCE_STATE%"=="10" (
  if /I "%~1"=="--check" (
    echo Startup check failed: an outdated bridge process is still running.
    echo Run start-bridge.bat normally to replace it safely.
    exit /b 1
  )
  echo Replacing an outdated VoiceBox STS Bridge process...
  powershell.exe -NoProfile -Command "$listenerPid = $null; try { $listenerPid = (Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort %BRIDGE_PORT% -State Listen -ErrorAction Stop | Select-Object -First 1).OwningProcess } catch {}; if (-not $listenerPid) { $pattern = '^\s*TCP\s+\S+:%BRIDGE_PORT%\s+\S+\s+LISTENING\s+(\d+)\s*$'; foreach ($line in netstat -ano -p tcp) { if ($line -match $pattern) { $listenerPid = [int]$Matches[1]; break } } }; if (-not $listenerPid) { exit 1 }; try { Stop-Process -Id $listenerPid -Force -ErrorAction Stop } catch { exit 1 }; for ($attempt = 0; $attempt -lt 20; $attempt++) { $client = [Net.Sockets.TcpClient]::new(); try { $client.Connect('127.0.0.1', %BRIDGE_PORT%) } catch { exit 0 } finally { $client.Dispose() }; Start-Sleep -Milliseconds 100 }; exit 1" >nul 2>&1
  if errorlevel 1 (
    echo Startup failed: the outdated bridge process could not be stopped.
    echo Close its Python process in Task Manager, then try again.
    pause
    exit /b 1
  )
)

goto start_bridge

:bridge_running
if /I "%~1"=="--check" (
  powershell.exe -NoProfile -Command "try { $status = Invoke-RestMethod -Uri '%BRIDGE_URL%/api/youtube/status' -TimeoutSec 2; if ($status.ready) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
  if errorlevel 1 (
    echo Startup check failed: the running bridge does not have a ready YouTube pipeline.
    exit /b 1
  )
  echo Startup check passed: the compatible bridge is already running.
  exit /b 0
)
echo VoiceBox STS Bridge is already running and compatible.
start "" "%BRIDGE_URL%"
exit /b 0

:start_bridge

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
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command "for ($attempt = 0; $attempt -lt 60; $attempt++) { try { $version = Invoke-RestMethod -Uri '%BRIDGE_URL%/api/version' -TimeoutSec 1; if (@($version.features) -contains '%BRIDGE_REQUIRED_FEATURE%') { Start-Process '%BRIDGE_URL%'; exit 0 } } catch {}; Start-Sleep -Milliseconds 250 }; exit 1"
"%BRIDGE_PYTHON%" -m voicebox_sts_bridge serve
set "BRIDGE_EXIT=%ERRORLEVEL%"

if not "%BRIDGE_EXIT%"=="0" (
  echo.
  echo VoiceBox STS Bridge exited with code %BRIDGE_EXIT%.
  pause
)
exit /b %BRIDGE_EXIT%
