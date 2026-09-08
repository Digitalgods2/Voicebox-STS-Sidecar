"""PyInstaller entry point for the macOS/Linux console build.

Run with no arguments (the normal double-click-from-Terminal case), this
mirrors build_assets/launcher.py's Windows startup sequence: reuse a
compatible running bridge, refuse to start if the port is owned by
something else, start VoiceBox if it isn't already running, then serve
the bridge and open the browser once it is ready. There is no tray icon
or ctypes message-box equivalent here - this stays a foreground console
process (Ctrl+C to stop), since the Windows launcher's windowed/tray
pieces rely on APIs that don't exist on macOS.

Run with a subcommand (engine-status, convert, ...), it falls through to
the regular CLI dispatcher unchanged.

The package's own __main__.py uses a relative import (``from .cli import
main``), which only resolves when the module is loaded as part of the
``voicebox_sts_bridge`` package. PyInstaller instead runs the given script
as a top-level ``__main__`` module with no package context, so that
relative import fails at runtime - hence this standalone entry point.
"""

from __future__ import annotations

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

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765
BRIDGE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
REQUIRED_FEATURE = "secure-openvoice-runtime-v1"
VOICEBOX_URL = "http://127.0.0.1:17493"
VOICEBOX_APP_NAME = "Voicebox"

log = logging.getLogger("voicebox_sts_bridge.console_launcher")


def _get_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _find_listener_pid(port: int) -> int | None:
    try:
        output = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def _instance_state(project_root: str) -> int:
    """Return 0=compatible bridge running, 10=stale bridge from this
    project, 11=port owned by something else, 20=port free."""
    version = _get_json(f"{BRIDGE_URL}/api/version")
    if version and REQUIRED_FEATURE in version.get("features", []):
        return 0

    status = _get_json(f"{BRIDGE_URL}/api/engine/status")
    if status is not None:
        actual = os.path.normpath(str(status.get("project_root", "")))
        expected = os.path.normpath(project_root)
        return 10 if actual == expected else 11

    return 11 if _find_listener_pid(BRIDGE_PORT) else 20


def _stop_stale_bridge() -> bool:
    pid = _find_listener_pid(BRIDGE_PORT)
    if pid is None:
        return False
    try:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=5)
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


def _start_voicebox_if_needed() -> None:
    if _get_json(f"{VOICEBOX_URL}/health") is not None:
        return
    # Prefer an explicit .app path over `open -a <name>`: when more than one
    # app is registered under the same name (e.g. a stray copy elsewhere, or
    # a locally built dev copy), name-based lookup can resolve to the wrong
    # one - `open -a Voicebox` observed doing exactly this during testing.
    app_path = os.environ.get("VOICEBOX_APP", "/Applications/Voicebox.app")
    log.info("VoiceBox is not running; attempting to start %s...", app_path)
    try:
        if os.path.isdir(app_path):
            subprocess.Popen(["open", app_path], close_fds=True)
        else:
            subprocess.Popen(["open", "-a", VOICEBOX_APP_NAME], close_fds=True)
    except OSError:
        log.warning("Could not launch VoiceBox.")
        return
    for _ in range(30):
        if _get_json(f"{VOICEBOX_URL}/health") is not None:
            return
        time.sleep(1)
    log.warning("VoiceBox did not become healthy within 30 seconds.")


def _launch() -> int:
    os.environ.setdefault("BRIDGE_HOST", BRIDGE_HOST)
    os.environ.setdefault("BRIDGE_PORT", str(BRIDGE_PORT))
    os.environ.setdefault("VOICEBOX_URL", VOICEBOX_URL)
    os.environ.setdefault("VOICEBOX_BASE_URL", VOICEBOX_URL)

    from voicebox_sts_bridge.logging_setup import configure_logging
    from voicebox_sts_bridge.openvoice_engine import OpenVoiceEngine
    from voicebox_sts_bridge.settings import Settings

    settings = Settings.from_env()
    configure_logging(settings.data_dir)

    project_root = str(OpenVoiceEngine().project_root)
    state = _instance_state(project_root)

    if state == 0:
        log.info("A compatible bridge is already running; opening the browser.")
        webbrowser.open(BRIDGE_URL)
        return 0

    if state == 11:
        print(f"error: port {BRIDGE_PORT} is owned by another application.", file=sys.stderr)
        print("Close that application or set BRIDGE_PORT to a different value.", file=sys.stderr)
        return 1

    if state == 10:
        log.info("Replacing an outdated VoiceBox STS Bridge process...")
        if not _stop_stale_bridge():
            print("error: an outdated bridge process could not be stopped.", file=sys.stderr)
            return 1

    _start_voicebox_if_needed()

    log.info("Starting VoiceBox STS Bridge at %s ...", BRIDGE_URL)
    threading.Thread(target=_open_when_ready, daemon=True).start()

    import uvicorn

    from voicebox_sts_bridge.api import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.bridge_host,
        port=settings.bridge_port,
        reload=False,
        log_config=None,
    )
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        from voicebox_sts_bridge.cli import main as cli_main

        return cli_main()
    return _launch()


if __name__ == "__main__":
    raise SystemExit(main())
