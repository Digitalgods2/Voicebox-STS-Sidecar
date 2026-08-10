from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
from uuid import UUID, uuid4


def _utc_now() -> str:
    """Return an unambiguous, JSON-friendly UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    """Normalize the path-like values returned by collaborators."""
    if isinstance(value, (Path, UUID)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class ConversionService:
    """Coordinate one serialized local conversion and its durable manifest."""

    def __init__(
        self,
        data_dir: Path,
        voicebox_client: Any,
        engine: Any,
        *,
        conversion_lock: threading.Lock | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.voicebox_client = voicebox_client
        self.engine = engine
        self._conversion_lock = conversion_lock or threading.Lock()

    def convert(
        self,
        source_audio: str | Path,
        profile_id: str,
        sample_id: str,
        *,
        tau: float = 0.3,
        overwrite: bool = False,
        output_audio: str | Path | None = None,
    ) -> dict[str, Any]:
        """Fetch the exact reference, run OpenVoice, and persist job state."""
        with self._conversion_lock:
            job_id = str(uuid4())
            jobs_dir = self.data_dir / "jobs"
            outputs_dir = self.data_dir / "outputs"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            outputs_dir.mkdir(parents=True, exist_ok=True)

            destination: str | Path
            if output_audio is None:
                destination = outputs_dir / f"{job_id}.wav"
            else:
                # Preserve the caller's value so OpenVoice remains responsible
                # for validating the output path and extension.
                destination = output_audio

            started_at = _utc_now()
            manifest_path = jobs_dir / f"{job_id}.json"
            manifest: dict[str, Any] = {
                "job_id": job_id,
                "status": "running",
                "created_at": started_at,
                "started_at": started_at,
                "updated_at": started_at,
                "source_audio": str(source_audio),
                "profile_id": str(profile_id),
                "sample_id": str(sample_id),
                "tau": _json_safe(tau),
                "overwrite": bool(overwrite),
                "output_audio": str(destination),
            }
            self._write_manifest(manifest_path, manifest)

            try:
                reference = self.voicebox_client.fetch_reference(
                    profile_id,
                    sample_id,
                    self.data_dir,
                    overwrite=overwrite,
                )
                reference_wav = reference["wav_path"]
                result = self.engine.convert(
                    source_audio,
                    reference_wav,
                    destination,
                    tau=tau,
                    overwrite=overwrite,
                )

                completed_at = _utc_now()
                safe_result = _json_safe(result)
                manifest.update(
                    status="completed",
                    updated_at=completed_at,
                    completed_at=completed_at,
                    reference=_json_safe(reference),
                    result=safe_result,
                    output_audio=str(safe_result.get("output_path", destination))
                    if isinstance(safe_result, dict)
                    else str(destination),
                )
                self._write_manifest(manifest_path, manifest)
                return manifest
            except Exception as exc:
                failed_at = _utc_now()
                message = str(exc).strip() or type(exc).__name__
                manifest.update(
                    status="failed",
                    updated_at=failed_at,
                    failed_at=failed_at,
                    error={"type": type(exc).__name__, "message": message[:1000]},
                )
                self._write_manifest(manifest_path, manifest)
                raise

    @staticmethod
    def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
        """Replace a manifest atomically without exposing partial JSON."""
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
            ) as temporary:
                json.dump(manifest, temporary, indent=2, ensure_ascii=False, allow_nan=False)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
