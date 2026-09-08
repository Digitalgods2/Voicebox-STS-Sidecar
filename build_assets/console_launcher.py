"""PyInstaller entry point for the macOS/Linux console build.

The package's own ``__main__.py`` uses a relative import (``from .cli import
main``), which only resolves when the module is loaded as part of the
``voicebox_sts_bridge`` package. PyInstaller instead runs the given script as
a top-level ``__main__`` module with no package context, so that relative
import fails at runtime. This wrapper imports absolutely instead.
"""

from __future__ import annotations

from voicebox_sts_bridge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
