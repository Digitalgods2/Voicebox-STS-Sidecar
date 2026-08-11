from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import hmac
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
import wave


_MINIMUM_TORCH_VERSION = (2, 13, 0)
_MINIMUM_TORCH_VERSION_TEXT = "2.13.0"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_RUNTIME_DEPENDENCIES = (
    "torch",
    "openvoice",
    "numpy",
    "librosa",
    "soundfile",
    "inflect",
    "unidecode",
    "eng_to_ipa",
    "pypinyin",
    "jieba",
    "cn2an",
)


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(f"invalid worker arguments: {message}")


def _project_paths(project_root: Path) -> dict[str, Path]:
    converter = project_root / "data" / "models" / "openvoice-v2" / "converter"
    return {
        "openvoice_source": project_root / "third_party" / "OpenVoice",
        "converter_config": converter / "config.json",
        "converter_checkpoint": converter / "checkpoint.pth",
        "converter_provenance": project_root / "config" / "openvoice-v2.provenance.json",
    }


def _validate_project_root(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"project root does not exist: {path}")
    return path


def _require_file(value: str | Path, label: str) -> Path:
    if not str(value).strip():
        raise ValueError(f"{label} must be a non-empty filesystem path")
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} does not exist: {value}") from exc
    if not path.is_file():
        raise ValueError(f"{label} must be a file: {path}")
    return path


def _validate_output(value: str | Path, source: Path | None = None, target: Path | None = None) -> Path:
    if not str(value).strip():
        raise ValueError("output must be a non-empty filesystem path")
    path = Path(value).expanduser().resolve(strict=False)
    if path.suffix.lower() != ".wav":
        raise ValueError("output must use the .wav extension")
    if path.exists() and not path.is_file():
        raise ValueError(f"output must not be a directory: {path}")
    if source is not None and path == source:
        raise ValueError("output must be different from source")
    if target is not None and path == target:
        raise ValueError("output must be different from target reference")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validate_device(device: str) -> str:
    if re.fullmatch(r"(?:cpu|cuda(?::\d+)?)", device) is None:
        raise ValueError("device must be 'cpu', 'cuda', or 'cuda:<index>'")
    return device


def _validate_tau(tau: float) -> float:
    value = float(tau)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("tau must be between 0 and 1")
    return value


def _require_model_files(project_root: Path) -> dict[str, Path]:
    paths = _project_paths(project_root)
    missing = [name for name, path in paths.items() if not (path.is_dir() if name == "openvoice_source" else path.is_file())]
    if missing:
        raise FileNotFoundError(f"OpenVoice installation is incomplete; missing: {', '.join(missing)}")
    _verify_converter_checkpoint(paths)
    source_text = str(paths["openvoice_source"])
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    return paths


def _dependency_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _installed_dependency_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _torch_version_is_secure(version: str | None) -> bool:
    if not isinstance(version, str):
        return False
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    return match is not None and tuple(int(part) for part in match.groups()) >= _MINIMUM_TORCH_VERSION


def _require_secure_torch_version(torch_module: Any) -> str:
    version = getattr(torch_module, "__version__", None) or _installed_dependency_version("torch")
    if not _torch_version_is_secure(version):
        found = version if isinstance(version, str) else "unknown"
        raise RuntimeError(
            f"PyTorch {_MINIMUM_TORCH_VERSION_TEXT} or newer is required; found {found}. "
            "Reinstall the pinned isolated OpenVoice runtime before loading a checkpoint."
        )
    return str(version)


def _expected_checkpoint_sha256(provenance_path: Path) -> str:
    if provenance_path.stat().st_size > 64 * 1024:
        raise RuntimeError("OpenVoice provenance file exceeds the 64 KiB safety limit")
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        expected = provenance["converter_model"]["sha256"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("OpenVoice provenance does not contain a valid converter SHA-256") from exc
    if not isinstance(expected, str) or _SHA256_PATTERN.fullmatch(expected.lower()) is None:
        raise RuntimeError("OpenVoice provenance does not contain a valid converter SHA-256")
    return expected.lower()


def _checkpoint_sha256(checkpoint_path: Path) -> str:
    digest = hashlib.sha256()
    with checkpoint_path.open("rb") as checkpoint:
        for block in iter(lambda: checkpoint.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_converter_checkpoint(paths: dict[str, Path]) -> str:
    expected = _expected_checkpoint_sha256(paths["converter_provenance"])
    actual = _checkpoint_sha256(paths["converter_checkpoint"])
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError("OpenVoice converter checkpoint SHA-256 does not match the audited provenance")
    return actual


def status_payload(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    paths = _project_paths(root)
    if paths["openvoice_source"].is_dir():
        source_text = str(paths["openvoice_source"])
        if source_text not in sys.path:
            sys.path.insert(0, source_text)
    checks: dict[str, bool] = {
        "project_root": root.is_dir(),
        "openvoice_source": paths["openvoice_source"].is_dir(),
        "converter_config": paths["converter_config"].is_file(),
        "converter_checkpoint": False,
        "converter_provenance": paths["converter_provenance"].is_file(),
    }
    checks.update({name: _dependency_available(name) for name in _RUNTIME_DEPENDENCIES})
    torch_version = _installed_dependency_version("torch")
    checks["torch"] = checks["torch"] and _torch_version_is_secure(torch_version)
    checkpoint_sha256: str | None = None
    if checks["converter_provenance"] and paths["converter_checkpoint"].is_file():
        try:
            checkpoint_sha256 = _verify_converter_checkpoint(paths)
            checks["converter_checkpoint"] = True
        except (OSError, RuntimeError):
            pass
    return {
        "ok": True,
        "operation": "status",
        "ready": all(checks.values()),
        "checks": checks,
        "project_root": str(root),
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "torch_version": torch_version,
        "minimum_torch_version": _MINIMUM_TORCH_VERSION_TEXT,
        "checkpoint_verified": checks["converter_checkpoint"],
        "checkpoint_sha256": checkpoint_sha256,
        "model_loaded": False,
    }


def _load_converter(project_root: Path, device: str) -> Any:
    paths = _require_model_files(project_root)
    # Import only in probe/convert. Status remains usable before heavyweight dependencies load.
    import torch

    torch_version = _require_secure_torch_version(torch)
    from openvoice.api import OpenVoiceBaseClass, ToneColorConverter

    requested_device = _validate_device(device)

    class NoWatermarkToneColorConverter(ToneColorConverter):
        """Work around upstream forwarding enable_watermark to its base constructor."""

        def __init__(self, config_path: str, model_device: str) -> None:
            OpenVoiceBaseClass.__init__(self, config_path, device=model_device)
            self.watermark_model = None
            self.version = getattr(self.hps, "_version_", "v1")

    # Deserialize tensor weights only and keep both model construction and
    # checkpoint loading on CPU to minimize CUDA peak memory.
    converter = NoWatermarkToneColorConverter(str(paths["converter_config"]), "cpu")
    checkpoint = torch.load(
        str(paths["converter_checkpoint"]),
        map_location="cpu",
        weights_only=True,
    )
    if not isinstance(checkpoint, dict) or "model" not in checkpoint:
        raise RuntimeError("OpenVoice checkpoint must contain a 'model' state dictionary")
    model_state = checkpoint["model"]
    if not isinstance(model_state, dict):
        raise RuntimeError("OpenVoice checkpoint 'model' entry must be a state dictionary")
    incompatible = converter.model.load_state_dict(model_state, strict=False)
    if hasattr(incompatible, "missing_keys") and hasattr(incompatible, "unexpected_keys"):
        missing_keys = list(incompatible.missing_keys)
        unexpected_keys = list(incompatible.unexpected_keys)
    else:
        missing_keys = list(incompatible[0])
        unexpected_keys = list(incompatible[1])
    if missing_keys or unexpected_keys:
        details: list[str] = []
        if missing_keys:
            details.append(f"missing keys: {', '.join(missing_keys)}")
        if unexpected_keys:
            details.append(f"unexpected keys: {', '.join(unexpected_keys)}")
        raise RuntimeError(f"OpenVoice checkpoint does not match the converter model ({'; '.join(details)})")
    converter.model.to(requested_device).eval()
    converter.device = requested_device
    converter.bridge_torch_version = torch_version
    return converter


def _reset_cuda_metrics(device: str) -> Any | None:
    requested_device = _validate_device(device)
    if not requested_device.startswith("cuda"):
        return None
    import torch

    # Some Windows PyTorch builds reject reset_peak_memory_stats before the
    # first CUDA context exists, even when given a valid torch.device.
    torch.cuda.init()
    torch.cuda.reset_peak_memory_stats(torch.device(requested_device))
    return torch


def _device_metrics(device: str, torch_module: Any | None) -> dict[str, Any]:
    if torch_module is None:
        return {
            "cuda_device_name": None,
            "peak_memory_allocated_mb": None,
            "peak_memory_reserved_mb": None,
        }
    torch_device = torch_module.device(device)
    torch_module.cuda.synchronize(torch_device)
    scale = 1024 * 1024
    return {
        "cuda_device_name": torch_module.cuda.get_device_name(torch_device),
        "peak_memory_allocated_mb": round(torch_module.cuda.max_memory_allocated(torch_device) / scale, 2),
        "peak_memory_reserved_mb": round(torch_module.cuda.max_memory_reserved(torch_device) / scale, 2),
    }


def probe_model(project_root: str | Path, *, device: str = "cuda:0") -> dict[str, Any]:
    root = _validate_project_root(project_root)
    requested_device = _validate_device(device)
    started = time.monotonic()
    torch_module = _reset_cuda_metrics(requested_device)
    converter = _load_converter(root, requested_device)
    result = {
        "ok": True,
        "operation": "probe",
        "ready": True,
        "model_loaded": converter is not None,
        "device": requested_device,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "watermark_enabled": False,
    }
    result.update(_device_metrics(requested_device, torch_module))
    return result


def _inspect_wav(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size <= 0:
            raise RuntimeError("OpenVoice produced an empty output file")
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            sample_rate = audio.getframerate()
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            if frames <= 0 or sample_rate <= 0:
                raise RuntimeError("OpenVoice produced a WAVE file with no decodable audio frames")
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
                raise RuntimeError("OpenVoice output WAVE data is truncated or undecodable")
            return {
                "size_bytes": size,
                "duration_seconds": round(frames / sample_rate, 3),
                "sample_rate_hz": sample_rate,
                "channels": channels,
                "sample_width_bytes": sample_width,
                "frames": frames,
            }
    except RuntimeError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise RuntimeError("OpenVoice output is not a valid, decodable PCM WAVE file") from exc


def run_conversion(
    project_root: str | Path,
    source_audio: str | Path,
    target_reference: str | Path,
    output_audio: str | Path,
    *,
    device: str = "cuda:0",
    tau: float = 0.3,
) -> dict[str, Any]:
    root = _validate_project_root(project_root)
    source = _require_file(source_audio, "source")
    target = _require_file(target_reference, "target reference")
    output = _validate_output(output_audio, source, target)
    value_tau = _validate_tau(tau)
    requested_device = _validate_device(device)
    started = time.monotonic()

    torch_module = _reset_cuda_metrics(requested_device)
    converter = _load_converter(root, requested_device)
    # Deliberately bypass openvoice.se_extractor: it invokes VAD/Whisper. Direct
    # converter embeddings preserve the offline source-audio conversion path.
    source_embedding = converter.extract_se(str(source))
    target_embedding = converter.extract_se(str(target))
    converter.convert(
        audio_src_path=str(source),
        src_se=source_embedding,
        tgt_se=target_embedding,
        output_path=str(output),
        tau=value_tau,
    )
    audio = _inspect_wav(output)
    result = {
        "ok": True,
        "operation": "convert",
        "output_path": str(output),
        "audio": audio,
        "device": requested_device,
        "tau": value_tau,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "watermark_enabled": False,
    }
    result.update(_device_metrics(requested_device, torch_module))
    return result


def _write_progress(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            json.dump(payload, temporary, ensure_ascii=True, separators=(",", ":"))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run_batch_conversion(
    project_root: str | Path,
    batch_file: str | Path,
    *,
    device: str = "cuda:0",
    tau: float = 0.3,
) -> dict[str, Any]:
    """Convert many source chunks while loading the model and target voice once."""
    root = _validate_project_root(project_root)
    batch_path = _require_file(batch_file, "batch file")
    if batch_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("batch file exceeds the 2 MiB safety limit")
    try:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("batch file must contain valid UTF-8 JSON") from exc
    if not isinstance(batch, dict):
        raise ValueError("batch file must contain a JSON object")
    target = _require_file(batch.get("target_reference", ""), "target reference")
    raw_items = batch.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("batch must contain at least one conversion item")
    if len(raw_items) > 2000:
        raise ValueError("batch cannot contain more than 2000 conversion items")
    progress_value = batch.get("progress_path")
    progress_path = Path(progress_value).expanduser().resolve() if progress_value else None
    if progress_path is not None and progress_path.suffix.lower() != ".json":
        raise ValueError("progress_path must use the .json extension")

    items: list[tuple[Path, Path]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"batch item {index} must be a JSON object")
        source = _require_file(item.get("source", ""), f"batch item {index} source")
        output = _validate_output(item.get("output", ""), source, target)
        items.append((source, output))

    requested_device = _validate_device(device)
    value_tau = _validate_tau(tau)
    started = time.monotonic()
    torch_module = _reset_cuda_metrics(requested_device)
    converter = _load_converter(root, requested_device)
    target_embedding = converter.extract_se(str(target))
    results: list[dict[str, Any]] = []
    total = len(items)
    if progress_path is not None:
        _write_progress(progress_path, {"status": "running", "completed": 0, "total": total})

    for index, (source, output) in enumerate(items):
        try:
            source_embedding = converter.extract_se(str(source))
            converter.convert(
                audio_src_path=str(source),
                src_se=source_embedding,
                tgt_se=target_embedding,
                output_path=str(output),
                tau=value_tau,
            )
            audio = _inspect_wav(output)
            results.append({"index": index, "source": str(source), "output_path": str(output), "audio": audio})
            if progress_path is not None:
                _write_progress(
                    progress_path,
                    {
                        "status": "running" if index + 1 < total else "completed",
                        "completed": index + 1,
                        "total": total,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    },
                )
        except Exception as exc:
            if progress_path is not None:
                _write_progress(
                    progress_path,
                    {
                        "status": "failed",
                        "completed": index,
                        "total": total,
                        "failed_index": index,
                        "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
                    },
                )
            raise RuntimeError(f"batch conversion failed at chunk {index + 1} of {total}: {exc}") from exc

    result = {
        "ok": True,
        "operation": "convert-batch",
        "items": results,
        "chunks_completed": total,
        "device": requested_device,
        "tau": value_tau,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "watermark_enabled": False,
    }
    result.update(_device_metrics(requested_device, torch_module))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="Isolated local OpenVoice V2 worker")
    subcommands = parser.add_subparsers(dest="operation", required=True)

    for name in ("status", "probe"):
        command = subcommands.add_parser(name)
        command.add_argument("--project-root", required=True)
        if name == "probe":
            command.add_argument("--device", default="cuda:0")

    convert = subcommands.add_parser("convert")
    convert.add_argument("--project-root", required=True)
    convert.add_argument("--source", required=True)
    convert.add_argument("--target-reference", required=True)
    convert.add_argument("--output", required=True)
    convert.add_argument("--device", default="cuda:0")
    convert.add_argument("--tau", type=float, default=0.3)

    convert_batch = subcommands.add_parser("convert-batch")
    convert_batch.add_argument("--project-root", required=True)
    convert_batch.add_argument("--batch-file", required=True)
    convert_batch.add_argument("--device", default="cuda:0")
    convert_batch.add_argument("--tau", type=float, default=0.3)
    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.operation == "status":
            payload = status_payload(arguments.project_root)
        else:
            # OpenVoice prints checkpoint diagnostics. Send all library stdout to
            # stderr so stdout remains exactly one machine-readable JSON object.
            with redirect_stdout(sys.stderr):
                if arguments.operation == "probe":
                    payload = probe_model(arguments.project_root, device=arguments.device)
                elif arguments.operation == "convert":
                    payload = run_conversion(
                        arguments.project_root,
                        arguments.source,
                        arguments.target_reference,
                        arguments.output,
                        device=arguments.device,
                        tau=arguments.tau,
                    )
                else:
                    payload = run_batch_conversion(
                        arguments.project_root,
                        arguments.batch_file,
                        device=arguments.device,
                        tau=arguments.tau,
                    )
        _emit(payload)
        return 0
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "operation": getattr(locals().get("arguments"), "operation", None),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
