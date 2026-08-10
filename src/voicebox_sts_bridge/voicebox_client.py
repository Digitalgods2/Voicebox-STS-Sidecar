from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID
import wave


class VoiceBoxError(RuntimeError):
    """Raised when the local VoiceBox service returns an unusable response."""


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def _as_list(payload: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    raise VoiceBoxError(f"VoiceBox returned an invalid {label} response")


def inspect_wav(payload: bytes) -> dict[str, Any]:
    if len(payload) < 12 or payload[:4] not in (b"RIFF", b"RF64") or payload[8:12] != b"WAVE":
        raise VoiceBoxError("VoiceBox sample is not a RIFF/RF64 WAVE file")

    details: dict[str, Any] = {"container": payload[:4].decode("ascii"), "size_bytes": len(payload)}
    try:
        with closing(wave.open(BytesIO(payload), "rb")) as wav:
            frame_rate = wav.getframerate()
            frames = wav.getnframes()
            details.update(
                channels=wav.getnchannels(),
                sample_rate_hz=frame_rate,
                sample_width_bytes=wav.getsampwidth(),
                frames=frames,
                duration_seconds=round(frames / frame_rate, 3) if frame_rate else None,
            )
    except (EOFError, wave.Error):
        # Some valid WAVE files use codecs unsupported by Python's wave module.
        details["pcm_details_available"] = False
    return details


class VoiceBoxClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        max_reference_bytes: int = 100 * 1024 * 1024,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_reference_bytes = max_reference_bytes
        self._opener = opener

    def _request(self, path: str, accept: str) -> tuple[bytes, Any]:
        request = Request(
            f"{self.base_url}{path}",
            headers={"Accept": accept, "User-Agent": "VoiceBox-STS-Bridge/0.3"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                limit = self.max_reference_bytes if accept == "audio/wav" else 2 * 1024 * 1024
                payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise VoiceBoxError(f"VoiceBox response exceeded the {limit}-byte safety limit")
                return payload, response.headers
        except HTTPError as exc:
            raise VoiceBoxError(f"VoiceBox returned HTTP {exc.code} for {path}") from exc
        except URLError as exc:
            raise VoiceBoxError(f"Cannot reach VoiceBox at {self.base_url}: {exc.reason}") from exc

    def _json(self, path: str) -> Any:
        payload, _ = self._request(path, "application/json")
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VoiceBoxError("VoiceBox returned invalid JSON") from exc

    def health(self) -> dict[str, Any]:
        payload = self._json("/health")
        if not isinstance(payload, dict):
            raise VoiceBoxError("VoiceBox returned an invalid health response")
        return payload

    def profiles(self) -> list[dict[str, Any]]:
        return _as_list(self._json("/profiles"), "profiles")

    def profile(self, profile_id: str) -> dict[str, Any]:
        profile_id = _uuid(profile_id, "profile_id")
        payload = self._json(f"/profiles/{profile_id}")
        if not isinstance(payload, dict):
            raise VoiceBoxError("VoiceBox returned an invalid profile response")
        return payload

    def samples(self, profile_id: str) -> list[dict[str, Any]]:
        profile_id = _uuid(profile_id, "profile_id")
        return _as_list(self._json(f"/profiles/{profile_id}/samples"), "samples")

    def fetch_reference(
        self,
        profile_id: str,
        sample_id: str,
        data_dir: Path,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        profile_id = _uuid(profile_id, "profile_id")
        sample_id = _uuid(sample_id, "sample_id")
        matching = next((sample for sample in self.samples(profile_id) if sample.get("id") == sample_id), None)
        if matching is None:
            raise VoiceBoxError("The selected sample does not belong to the selected profile")

        destination_dir = Path(data_dir).resolve() / "references" / profile_id
        destination_dir.mkdir(parents=True, exist_ok=True)
        wav_path = destination_dir / f"{sample_id}.wav"
        metadata_path = destination_dir / f"{sample_id}.json"

        if wav_path.exists() and metadata_path.exists() and not overwrite:
            payload = wav_path.read_bytes()
            details = inspect_wav(payload)
            return self._reference_result(profile_id, sample_id, matching, wav_path, metadata_path, details, True)

        payload, headers = self._request(f"/samples/{sample_id}", "audio/wav")
        content_type = headers.get("Content-Type", "") if headers else ""
        if content_type and not content_type.lower().startswith(("audio/wav", "audio/x-wav", "application/octet-stream")):
            raise VoiceBoxError(f"VoiceBox returned unexpected content type {content_type!r}")
        details = inspect_wav(payload)

        wav_temp_path: Path | None = None
        metadata_temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination_dir, suffix=".tmp", delete=False) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                wav_temp_path = Path(temporary.name)

            metadata = {
                "profile_id": profile_id,
                "sample_id": sample_id,
                "reference_text": matching.get("reference_text"),
                "source": f"{self.base_url}/samples/{sample_id}",
                "audio": details,
            }
            with tempfile.NamedTemporaryFile(
                dir=destination_dir, suffix=".tmp", mode="w", encoding="utf-8", delete=False
            ) as temporary:
                temporary.write(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                metadata_temp_path = Path(temporary.name)

            os.replace(metadata_temp_path, metadata_path)
            metadata_temp_path = None
            os.replace(wav_temp_path, wav_path)
            wav_temp_path = None
        finally:
            if wav_temp_path is not None:
                wav_temp_path.unlink(missing_ok=True)
            if metadata_temp_path is not None:
                metadata_temp_path.unlink(missing_ok=True)

        return self._reference_result(profile_id, sample_id, matching, wav_path, metadata_path, details, False)

    @staticmethod
    def _reference_result(
        profile_id: str,
        sample_id: str,
        sample: dict[str, Any],
        wav_path: Path,
        metadata_path: Path,
        audio: dict[str, Any],
        cached: bool,
    ) -> dict[str, Any]:
        return {
            "profile_id": profile_id,
            "sample_id": sample_id,
            "reference_text": sample.get("reference_text"),
            "wav_path": str(wav_path),
            "metadata_path": str(metadata_path),
            "audio": audio,
            "cached": cached,
        }
