from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or not _is_loopback_host(parsed.hostname):
        raise ValueError("VoiceBox URL must be an http:// loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("VoiceBox URL must not contain credentials, a query, or a fragment")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class Settings:
    voicebox_base_url: str = "http://127.0.0.1:17493"
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8765
    data_dir: Path = Path("data")
    request_timeout_seconds: float = 15.0
    max_reference_bytes: int = 100 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "voicebox_base_url", _validate_loopback_url(self.voicebox_base_url))
        if not _is_loopback_host(self.bridge_host):
            raise ValueError("Bridge host must be a loopback address")
        if not 1 <= self.bridge_port <= 65535:
            raise ValueError("Bridge port must be between 1 and 65535")
        if self.request_timeout_seconds <= 0:
            raise ValueError("Request timeout must be positive")
        if self.max_reference_bytes <= 0:
            raise ValueError("Maximum reference size must be positive")
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        return cls(
            voicebox_base_url=os.getenv("VOICEBOX_BASE_URL", defaults.voicebox_base_url),
            bridge_host=os.getenv("BRIDGE_HOST", defaults.bridge_host),
            bridge_port=int(os.getenv("BRIDGE_PORT", str(defaults.bridge_port))),
            data_dir=Path(os.getenv("BRIDGE_DATA_DIR", str(defaults.data_dir))),
        )
