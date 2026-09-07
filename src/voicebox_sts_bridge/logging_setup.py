from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
import sys

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 4
_MARKER = "_voicebox_sts_bridge_handler"


def log_file_path(data_dir: Path) -> Path:
    return data_dir / "logs" / "bridge.log"


def configure_logging(data_dir: Path, *, console: bool | None = None) -> Path:
    """Route app and uvicorn logs to a small rotating file so the server is
    diagnosable even when it has no attached console (a frozen, windowed
    build). When a real console is attached, logs also go to stdout.

    Safe to call more than once (e.g. once per test's create_app call): a
    prior call's handlers are replaced, not stacked.
    """
    path = log_file_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    if console is None:
        console = sys.stdout is not None and not getattr(sys, "frozen", False)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [h for h in root.handlers if not getattr(h, _MARKER, False)]

    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    setattr(file_handler, _MARKER, True)
    root.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter(_FORMAT))
        setattr(stream_handler, _MARKER, True)
        root.addHandler(stream_handler)

    return path


def tail_log(data_dir: Path, max_lines: int = 200) -> list[str]:
    path = log_file_path(data_dir)
    if not path.is_file():
        return []
    max_lines = max(1, min(max_lines, 2000))
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    return [line.rstrip("\n") for line in lines[-max_lines:]]
