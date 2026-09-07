"""Frozen-exe launcher for VoiceBox STS Bridge.

Windowed (no console) entry point. Mirrors start-bridge.bat: reuse a
compatible running bridge, replace a stale one from this same install,
refuse to start if the port is owned by another application, start the
local VoiceBox service if needed, then serve the bridge and open the
browser once it is ready. Runs the server in a background thread and shows
a system-tray icon (Open Bridge / View Logs / Quit) on the main thread,
since there is no console window to interact with.

This script is the PyInstaller entry point; it is not part of the
installed package and is not imported by the bridge itself.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765
BRIDGE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
REQUIRED_FEATURE = "secure-openvoice-runtime-v1"
VOICEBOX_URL = "http://127.0.0.1:17493"

MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30

log = logging.getLogger("voicebox_sts_bridge.launcher")


def _message_box(title: str, text: str, icon: int = MB_ICONERROR) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, icon)
    except Exception:
        pass


def _resource_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def _get_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _find_listener_pid(port: int) -> int | None:
    try:
        output = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    needle = f":{port} "
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and "LISTENING" in parts and needle in line and f":{port}" in parts[1]:
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return None


def _instance_state(project_root: str) -> int:
    """Return 0=compatible bridge running, 10=stale bridge from this project,
    11=port owned by something else, 20=port free."""
    version = _get_json(f"{BRIDGE_URL}/api/version")
    if version and REQUIRED_FEATURE in version.get("features", []):
        return 0

    status = _get_json(f"{BRIDGE_URL}/api/engine/status")
    if status is not None:
        actual = os.path.normcase(os.path.normpath(str(status.get("project_root", ""))))
        expected = os.path.normcase(os.path.normpath(project_root))
        return 10 if actual == expected else 11

    pid = _find_listener_pid(BRIDGE_PORT)
    return 11 if pid else 20


def _stop_stale_bridge() -> bool:
    pid = _find_listener_pid(BRIDGE_PORT)
    if pid is None:
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for _ in range(20):
        if _find_listener_pid(BRIDGE_PORT) is None:
            return True
        time.sleep(0.1)
    return False


def _open_when_ready() -> None:
    for _ in range(60):
        version = _get_json(f"{BRIDGE_URL}/api/version", timeout=1.0)
        if version and REQUIRED_FEATURE in version.get("features", []):
            webbrowser.open(BRIDGE_URL)
            return
        time.sleep(0.25)
    log.warning("Bridge did not report ready within 15 seconds; not auto-opening the browser.")


def _run_server(server) -> None:
    try:
        asyncio.run(server.serve())
    except Exception:
        log.exception("Bridge server crashed")


def _tray_icon(server, log_path: Path):
    import pystray
    from PIL import Image

    try:
        image = Image.open(_resource_dir() / "icon.ico")
    except Exception:
        image = Image.new("RGB", (32, 32), (56, 189, 248))

    def _open(icon, item) -> None:
        webbrowser.open(BRIDGE_URL)

    def _view_logs(icon, item) -> None:
        try:
            os.startfile(str(log_path))  # noqa: S606 - user-initiated, local file
        except OSError:
            os.startfile(str(log_path.parent))

    def _quit(icon, item) -> None:
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Bridge", _open, default=True),
        pystray.MenuItem("View Logs", _view_logs),
        pystray.MenuItem("Quit", _quit),
    )
    return pystray.Icon("voicebox-sts-bridge", image, "VoiceBox STS Bridge", menu)


def main() -> int:
    os.environ.setdefault("BRIDGE_HOST", BRIDGE_HOST)
    os.environ.setdefault("BRIDGE_PORT", str(BRIDGE_PORT))
    os.environ.setdefault("VOICEBOX_URL", VOICEBOX_URL)
    os.environ.setdefault("VOICEBOX_BASE_URL", VOICEBOX_URL)
    os.environ.setdefault(
        "VOICEBOX_EXE",
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Voicebox", "voicebox.exe"),
    )

    from voicebox_sts_bridge.logging_setup import configure_logging
    from voicebox_sts_bridge.openvoice_engine import OpenVoiceEngine
    from voicebox_sts_bridge.settings import Settings

    settings = Settings.from_env()
    log_path = configure_logging(settings.data_dir, console=False)

    # A windowed PyInstaller build has no real stdio; guard against library
    # code that calls print()/sys.stderr.write() crashing on a None stream.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        log.critical("Unhandled error", exc_info=(exc_type, exc_value, exc_tb))
        _message_box(
            "VoiceBox STS Bridge",
            f"The bridge hit an unexpected error and is closing:\n\n{exc_value}\n\nSee {log_path} for details.",
        )

    sys.excepthook = _excepthook

    project_root = str(OpenVoiceEngine().project_root)
    state = _instance_state(project_root)

    if state == 0:
        webbrowser.open(BRIDGE_URL)
        return 0

    if state == 11:
        _message_box(
            "VoiceBox STS Bridge",
            f"Startup failed: port {BRIDGE_PORT} is owned by another application.\n\n"
            "Close that application or configure a different BRIDGE_PORT.",
        )
        return 1

    if state == 10:
        log.info("Replacing an outdated VoiceBox STS Bridge process...")
        if not _stop_stale_bridge():
            _message_box(
                "VoiceBox STS Bridge",
                "Startup failed: an outdated bridge process could not be stopped.\n\n"
                "Close its process in Task Manager, then try again.",
            )
            return 1

    voicebox_exe = os.environ["VOICEBOX_EXE"]
    if _get_json(f"{VOICEBOX_URL}/health") is None:
        if os.path.isfile(voicebox_exe):
            log.info("Starting VoiceBox...")
            subprocess.Popen([voicebox_exe], close_fds=True)
            for _ in range(30):
                if _get_json(f"{VOICEBOX_URL}/health") is not None:
                    break
                time.sleep(1)
            else:
                log.warning("VoiceBox did not become healthy within 30 seconds.")
        else:
            log.warning("VoiceBox is not running and was not found at: %s", voicebox_exe)

    log.info("Starting VoiceBox STS Bridge at %s ...", BRIDGE_URL)
    threading.Thread(target=_open_when_ready, daemon=True).start()

    import uvicorn

    from voicebox_sts_bridge.api import create_app

    config = uvicorn.Config(
        create_app(settings),
        host=settings.bridge_host,
        port=settings.bridge_port,
        reload=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=_run_server, args=(server,), daemon=True)
    server_thread.start()

    icon = _tray_icon(server, log_path)
    icon.run()  # blocks until Quit; runs the Windows message loop on the main thread

    server.should_exit = True
    server_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
