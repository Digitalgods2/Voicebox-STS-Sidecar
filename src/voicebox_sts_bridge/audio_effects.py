from __future__ import annotations

from collections.abc import Callable
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
import wave


class AudioEffectsError(RuntimeError):
    """Raised when duration-preserving post-conversion DSP fails."""


Runner = Callable[..., subprocess.CompletedProcess[str]]

MAX_PITCH_SEMITONES = 6.0
MAX_BRIGHTNESS_DB = 6.0
TONE_SHELF_FREQUENCY_HZ = 2_500


def validate_audio_adjustments(
    pitch_semitones: float = 0.0,
    brightness_db: float = 0.0,
) -> tuple[float, float]:
    """Normalize and bound the two user-facing voice adjustments."""
    pitch = float(pitch_semitones)
    brightness = float(brightness_db)
    if not math.isfinite(pitch) or not -MAX_PITCH_SEMITONES <= pitch <= MAX_PITCH_SEMITONES:
        raise ValueError(
            f"pitch_semitones must be between {-MAX_PITCH_SEMITONES:g} and {MAX_PITCH_SEMITONES:g}"
        )
    if not math.isfinite(brightness) or not -MAX_BRIGHTNESS_DB <= brightness <= MAX_BRIGHTNESS_DB:
        raise ValueError(
            f"brightness_db must be between {-MAX_BRIGHTNESS_DB:g} and {MAX_BRIGHTNESS_DB:g}"
        )
    # Avoid negative zero in JSON, filter strings, and UI diagnostics.
    return (0.0 if pitch == 0 else pitch, 0.0 if brightness == 0 else brightness)


def inspect_pcm_wav(path: str | Path) -> dict[str, Any]:
    """Fully read and validate the mono PCM WAV produced by OpenVoice."""
    audio_path = Path(path).expanduser().resolve(strict=True)
    try:
        size = audio_path.stat().st_size
        with wave.open(str(audio_path), "rb") as audio:
            details = {
                "size_bytes": size,
                "frames": audio.getnframes(),
                "sample_rate_hz": audio.getframerate(),
                "channels": audio.getnchannels(),
                "sample_width_bytes": audio.getsampwidth(),
                "compression": audio.getcomptype(),
            }
            if details["frames"] <= 0 or details["sample_rate_hz"] <= 0:
                raise AudioEffectsError("WAVE file contains no decodable audio frames")
            if details["channels"] != 1 or details["sample_width_bytes"] != 2:
                raise AudioEffectsError("Post-processing requires mono 16-bit PCM WAVE audio")
            if details["compression"] != "NONE":
                raise AudioEffectsError("Post-processing requires uncompressed PCM WAVE audio")
            decoded = 0
            while True:
                block = audio.readframes(65_536)
                if not block:
                    break
                decoded += len(block)
            if decoded != details["frames"] * details["channels"] * details["sample_width_bytes"]:
                raise AudioEffectsError("WAVE audio is truncated or undecodable")
    except AudioEffectsError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioEffectsError(f"File is not a valid PCM WAVE: {audio_path}") from exc
    details["duration_seconds"] = round(details["frames"] / details["sample_rate_hz"], 6)
    return details


class AudioEffectsProcessor:
    """Apply high-quality offline pitch and tone DSP without changing timing."""

    def __init__(
        self,
        *,
        ffmpeg_path: str | Path | None = None,
        runner: Runner = subprocess.run,
        timeout_seconds: float = 12 * 60 * 60,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.ffmpeg_path = str(ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg")
        self._runner = runner
        self.timeout_seconds = float(timeout_seconds)

    def apply(
        self,
        input_audio: str | Path,
        output_audio: str | Path | None = None,
        *,
        pitch_semitones: float = 0.0,
        brightness_db: float = 0.0,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        pitch, brightness = validate_audio_adjustments(pitch_semitones, brightness_db)
        source = Path(input_audio).expanduser().resolve(strict=True)
        destination = (
            source if output_audio is None else Path(output_audio).expanduser().resolve(strict=False)
        )
        source_details = inspect_pcm_wav(source)
        same_path = source == destination

        if not pitch and not brightness:
            if not same_path:
                if destination.exists() and not overwrite:
                    raise FileExistsError(f"Output already exists: {destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            return self._result(
                destination,
                pitch,
                brightness,
                source_details,
                applied=False,
            )

        if destination.suffix.lower() != ".wav":
            raise ValueError("Post-processed output must use a .wav extension")
        if destination.exists() and not same_path and not overwrite:
            raise FileExistsError(f"Output already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        filters: list[str] = []
        pitch_scale = 1.0
        if pitch:
            pitch_scale = 2.0 ** (pitch / 12.0)
            filters.append(
                "rubberband="
                f"tempo=1:pitch={pitch_scale:.12f}:"
                "transients=mixed:detector=soft:phase=laminar:window=short:"
                "smoothing=off:formant=preserved:pitchq=quality"
            )
        if brightness:
            filters.append(
                "highshelf="
                f"frequency={TONE_SHELF_FREQUENCY_HZ}:width_type=q:width=0.7:"
                f"gain={brightness:.3f}:poles=2:precision=f32"
            )
        # The limiter is unity-gain unless an adjusted peak would clip. Latency
        # compensation keeps the first sample aligned with the source timeline.
        filters.append("alimiter=limit=0.98:attack=5:release=50:level=false:latency=true")
        filters.extend(
            [
                f"apad=whole_len={source_details['frames']}",
                f"atrim=end_sample={source_details['frames']}",
                "asetpts=N/SR/TB",
            ]
        )

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.stem}.effects.",
                suffix=".wav",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)

            command = [
                self.ffmpeg_path,
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                ",".join(filters),
                "-ar",
                str(source_details["sample_rate_hz"]),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(temporary_path),
            ]
            try:
                completed = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AudioEffectsError(f"FFmpeg audio adjustment could not run: {exc}") from exc
            if completed.returncode != 0:
                diagnostics = (completed.stderr or completed.stdout or "no diagnostics").strip()[-4000:]
                raise AudioEffectsError(f"FFmpeg audio adjustment failed: {diagnostics}")

            processed_details = inspect_pcm_wav(temporary_path)
            if processed_details["frames"] != source_details["frames"]:
                raise AudioEffectsError(
                    "Post-processing changed the audio frame count: "
                    f"{processed_details['frames']} != {source_details['frames']}"
                )
            if processed_details["sample_rate_hz"] != source_details["sample_rate_hz"]:
                raise AudioEffectsError("Post-processing changed the audio sample rate")

            os.replace(temporary_path, destination)
            temporary_path = None
            return self._result(
                destination,
                pitch,
                brightness,
                processed_details,
                applied=True,
                pitch_scale=pitch_scale,
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _result(
        destination: Path,
        pitch: float,
        brightness: float,
        audio: dict[str, Any],
        *,
        applied: bool,
        pitch_scale: float = 1.0,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "applied": applied,
            "output_path": str(destination),
            "pitch_semitones": pitch,
            "pitch_scale": round(pitch_scale, 12),
            "brightness_db": brightness,
            "tone_shelf_frequency_hz": TONE_SHELF_FREQUENCY_HZ,
            "tempo_preserved": True,
            "formants_preserved": True,
            "exact_frame_match": True,
            "limiter_ceiling": 0.98 if applied else None,
            "audio": audio,
        }
