from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]


def test_windows_launcher_requires_the_current_backend_contract() -> None:
    launcher = (PROJECT_ROOT / "start-bridge.bat").read_text(encoding="utf-8")

    assert "BRIDGE_REQUIRED_FEATURE=youtube-source-cache-v1" in launcher
    assert "/api/version" in launcher
    assert "@($version.features) -contains '%BRIDGE_REQUIRED_FEATURE%'" in launcher
    assert "Startup check passed: the compatible bridge is already running." in launcher


def test_windows_launcher_replaces_only_a_stale_bridge_from_this_project() -> None:
    launcher = (PROJECT_ROOT / "start-bridge.bat").read_text(encoding="utf-8")

    assert "[string]$status.project_root" in launcher
    assert "[StringComparison]::OrdinalIgnoreCase" in launcher
    assert "Replacing an outdated VoiceBox STS Bridge process" in launcher
    assert "Get-NetTCPConnection" in launcher
    assert "Stop-Process -Id $listenerPid" in launcher
    assert "port %BRIDGE_PORT% is owned by another application" in launcher


def test_windows_launcher_forces_imports_from_the_worktree() -> None:
    launcher = (PROJECT_ROOT / "start-bridge.bat").read_text(encoding="utf-8")

    assert 'set "PYTHONPATH=%CD%\\src"' in launcher
    assert 'set "PYTHONPATH=%CD%\\src;%PYTHONPATH%"' not in launcher
