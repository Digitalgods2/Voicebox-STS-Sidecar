from __future__ import annotations

from collections.abc import Callable
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any
import wave


class OpenVoiceError(RuntimeError):
    """Raised when the isolated OpenVoice worker cannot complete an operation."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _require_input_file(value: str | Path, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{label} must be a non-empty filesystem path")
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} does not exist: {value}") from exc
    if not path.is_file():
        raise ValueError(f"{label} must be a file: {path}")
    return path


def _output_path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("output_audio must be a non-empty filesystem path")
    try:
        path = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"output_audio is not a valid path: {value}") from exc
    if path.suffix.lower() != ".wav":
        raise ValueError("output_audio must use the .wav extension")
    if path.exists() and not path.is_file():
        raise ValueError(f"output_audio must not be a directory: {path}")
    return path


def _validate_device(device: str) -> str:
    if not isinstance(device, str) or re.fullmatch(r"(?:cpu|cuda(?::\d+)?)", device) is None:
        raise ValueError("device must be 'cpu', 'cuda', or 'cuda:<index>'")
    return device


def _validate_tau(tau: float) -> float:
    try:
        value = float(tau)
    except (TypeError, ValueError) as exc:
        raise ValueError("tau must be a number between 0 and 1") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("tau must be a number between 0 and 1")
    return value


def _inspect_wav(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size <= 0:
            raise OpenVoiceError("OpenVoice produced an empty output file")
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            sample_rate = audio.getframerate()
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            if frames <= 0 or sample_rate <= 0:
                raise OpenVoiceError("OpenVoice produced a WAVE file with no decodable audio frames")
            decoded_bytes = 0
            remaining = frames
            while remaining:
                frame_batch = min(remaining, 65_536)
                payload = audio.readframes(frame_batch)
                if not payload:
                    break
                decoded_bytes += len(payload)
                remaining -= len(payload) // (channels * sample_width)
            expected_bytes = frames * channels * sample_width
            if decoded_bytes != expected_bytes:
                raise OpenVoiceError("OpenVoice output WAVE data is truncated or undecodable")
            return {
                "size_bytes": size,
                "duration_seconds": round(frames / sample_rate, 3),
                "sample_rate_hz": sample_rate,
                "channels": channels,
                "sample_width_bytes": sample_width,
                "frames": frames,
            }
    except OpenVoiceError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise OpenVoiceError("OpenVoice output is not a valid, decodable PCM WAVE file") from exc


class OpenVoiceEngine:
    """Base-environment bridge to the project-local OpenVoice V2 environment."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        timeout_seconds: float = 30 * 60,
        python_path: str | Path | None = None,
        worker_path: str | Path | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.project_root = Path(project_root or _default_project_root()).expanduser().resolve()
        self.python_path = Path(python_path or self.project_root / ".envs" / "openvoice-v2" / "python.exe").resolve()
        self.worker_path = Path(worker_path or Path(__file__).with_name("openvoice_worker.py")).resolve()
        if timeout_seconds <= 0 or not math.isfinite(float(timeout_seconds)):
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner

    def status(self) -> dict[str, Any]:
        """Check filesystem and dependency readiness without importing or loading models."""
        local_checks = {
            "project_root": self.project_root.is_dir(),
            "python": self.python_path.is_file(),
            "worker": self.worker_path.is_file(),
        }
        if not local_checks["python"] or not local_checks["worker"]:
            return {
                "ok": True,
                "operation": "status",
                "ready": False,
                "checks": local_checks,
                "project_root": str(self.project_root),
                "python": str(self.python_path),
            }
        return self._invoke("status", timeout_seconds=min(self.timeout_seconds, 30.0))

    def probe(self, *, device: str = "cuda:0") -> dict[str, Any]:
        """Import OpenVoice in its isolated environment and load the local converter model."""
        return self._invoke("probe", "--device", _validate_device(device))

    def convert(
        self,
        source_audio: str | Path,
        target_reference: str | Path,
        output_audio: str | Path,
        *,
        device: str = "cuda:0",
        tau: float = 0.3,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        source = _require_input_file(source_audio, "source_audio")
        target = _require_input_file(target_reference, "target_reference")
        destination = _output_path(output_audio)
        if destination == source:
            raise ValueError("output_audio must be different from source_audio")
        if destination == target:
            raise ValueError("output_audio must be different from target_reference")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"output_audio already exists: {destination}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.stem}.",
                suffix=".wav",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name).resolve()

            result = self._invoke(
                "convert",
                "--source",
                str(source),
                "--target-reference",
                str(target),
                "--output",
                str(temporary_path),
                "--device",
                _validate_device(device),
                "--tau",
                str(_validate_tau(tau)),
            )
            audio = _inspect_wav(temporary_path)
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        result["output_path"] = str(destination)
        result["audio"] = audio
        return result

    def convert_batch(
        self,
        conversions: list[tuple[str | Path, str | Path]],
        target_reference: str | Path,
        *,
        device: str = "cuda:0",
        tau: float = 0.3,
        overwrite: bool = False,
        progress_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Convert many chunks in one worker so the model and target embedding load once."""
        if not isinstance(conversions, list) or not conversions:
            raise ValueError("conversions must contain at least one source/output pair")
        if len(conversions) > 2000:
            raise ValueError("conversions cannot contain more than 2000 items")
        target = _require_input_file(target_reference, "target_reference")
        requested_device = _validate_device(device)
        value_tau = _validate_tau(tau)

        validated: list[tuple[Path, Path]] = []
        seen_destinations: set[Path] = set()
        for index, pair in enumerate(conversions):
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(f"conversion item {index} must be a source/output pair")
            source = _require_input_file(pair[0], f"conversion item {index} source")
            destination = _output_path(pair[1])
            if destination in {source, target}:
                raise ValueError(f"conversion item {index} output must differ from its inputs")
            if destination in seen_destinations:
                raise ValueError("conversion outputs must be unique")
            if destination.exists() and not overwrite:
                raise FileExistsError(f"output_audio already exists: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            seen_destinations.add(destination)
            validated.append((source, destination))

        resolved_progress: Path | None = None
        if progress_path is not None:
            resolved_progress = Path(progress_path).expanduser().resolve()
            if resolved_progress.suffix.lower() != ".json":
                raise ValueError("progress_path must use the .json extension")
            resolved_progress.parent.mkdir(parents=True, exist_ok=True)

        temporary_outputs: list[tuple[Path, Path]] = []
        batch_path: Path | None = None
        try:
            for _, destination in validated:
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent,
                    prefix=f".{destination.stem}.",
                    suffix=".wav",
                    delete=False,
                ) as temporary:
                    temporary_outputs.append((Path(temporary.name).resolve(), destination))

            batch_directory = self.project_root / "data"
            batch_directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=batch_directory,
                prefix=".openvoice-batch.",
                suffix=".json",
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
            ) as batch_file:
                payload: dict[str, Any] = {
                    "target_reference": str(target),
                    "items": [
                        {"source": str(source), "output": str(temporary_outputs[index][0])}
                        for index, (source, _) in enumerate(validated)
                    ],
                }
                if resolved_progress is not None:
                    payload["progress_path"] = str(resolved_progress)
                json.dump(payload, batch_file, ensure_ascii=True, separators=(",", ":"))
                batch_file.write("\n")
                batch_file.flush()
                os.fsync(batch_file.fileno())
                batch_path = Path(batch_file.name).resolve()

            result = self._invoke(
                "convert-batch",
                "--batch-file",
                str(batch_path),
                "--device",
                requested_device,
                "--tau",
                str(value_tau),
                timeout_seconds=max(self.timeout_seconds, len(validated) * 120.0),
            )

            published_items: list[dict[str, Any]] = []
            for index, (temporary_output, destination) in enumerate(temporary_outputs):
                audio = _inspect_wav(temporary_output)
                os.replace(temporary_output, destination)
                published_items.append(
                    {
                        "index": index,
                        "source": str(validated[index][0]),
                        "output_path": str(destination),
                        "audio": audio,
                    }
                )
            temporary_outputs.clear()
            result["items"] = published_items
            result["chunks_completed"] = len(published_items)
            return result
        finally:
            if batch_path is not None:
                batch_path.unlink(missing_ok=True)
            for temporary_output, _ in temporary_outputs:
                temporary_output.unlink(missing_ok=True)

    def _invoke(self, operation: str, *arguments: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        if not self.python_path.is_file():
            raise OpenVoiceError(f"OpenVoice Python environment is missing: {self.python_path}")
        if not self.worker_path.is_file():
            raise OpenVoiceError(f"OpenVoice worker is missing: {self.worker_path}")

        command = [
            str(self.python_path),
            str(self.worker_path),
            operation,
            "--project-root",
            str(self.project_root),
            *arguments,
        ]
        environment = os.environ.copy()
        # The launcher exposes the bridge's src/ tree through PYTHONPATH. Do not
        # leak that base-environment package path into the isolated ML runtime.
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            }
        )
        limit = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            completed = self._runner(
                command,
                cwd=str(self.project_root),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenVoiceError(f"OpenVoice {operation} timed out after {limit:g} seconds") from exc
        except OSError as exc:
            raise OpenVoiceError(f"Could not start the isolated OpenVoice worker: {exc}") from exc

        stdout = completed.stdout.strip()
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            diagnostics = completed.stderr.strip()
            suffix = f": {diagnostics[-1000:]}" if diagnostics else ""
            raise OpenVoiceError(f"OpenVoice worker returned invalid JSON{suffix}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise OpenVoiceError("OpenVoice worker returned an invalid response object")

        if completed.returncode != 0 or not payload["ok"]:
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
            else:
                message = error
            if not isinstance(message, str) or not message.strip():
                message = f"worker exited with code {completed.returncode}"
            raise OpenVoiceError(f"OpenVoice {operation} failed: {message}")
        if completed.returncode != 0:
            raise OpenVoiceError(f"OpenVoice worker exited with code {completed.returncode}")
        return payload
