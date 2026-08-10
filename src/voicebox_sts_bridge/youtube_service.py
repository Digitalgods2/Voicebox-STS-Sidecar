from __future__ import annotations

from array import array
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit
from uuid import UUID, uuid4
import wave

from .audio_effects import AudioEffectsProcessor, validate_audio_adjustments


class YouTubeJobError(RuntimeError):
    """Raised when a local YouTube video conversion pipeline cannot continue."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
DownloadProgress = Callable[[dict[str, Any]], None]
Downloader = Callable[[str, Path, DownloadProgress], tuple[Path, dict[str, Any]]]

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
_VIDEO_SUFFIXES = {".mkv", ".mp4", ".webm", ".mov"}
_MODEL_SAMPLE_RATE = 22_050
_MODEL_FRAME_SIZE = 256


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def validate_youtube_url(value: str) -> str:
    """Accept only direct HTTPS YouTube video URLs, never arbitrary downloader URLs."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("youtube_url is required")
    if len(value) > 2048:
        raise ValueError("youtube_url is too long")
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host not in _YOUTUBE_HOSTS:
        raise ValueError("youtube_url must be a direct https:// YouTube URL")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("youtube_url must not contain credentials or a custom port")

    path_parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    if host == "youtu.be":
        direct_video = len(path_parts) == 1 and bool(path_parts[0])
    else:
        direct_video = (
            (parsed.path.rstrip("/") == "/watch" and len(query.get("v", [])) == 1)
            or (len(path_parts) == 2 and path_parts[0] in {"shorts", "live", "embed"})
        )
    if not direct_video:
        raise ValueError("youtube_url must identify one video, not a playlist, channel, or search page")
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
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
            json.dump(payload, temporary, indent=2, ensure_ascii=False, allow_nan=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            break
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.025 * (attempt + 1))
    else:
        raise YouTubeJobError(f"Job metadata is missing or invalid: {path}") from last_error
    if not isinstance(value, dict):
        raise YouTubeJobError(f"Job metadata is not a JSON object: {path}")
    return value


def _numeric_version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def _inspect_pcm_wav(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        with wave.open(str(path), "rb") as source:
            details = {
                "size_bytes": size,
                "frames": source.getnframes(),
                "sample_rate_hz": source.getframerate(),
                "channels": source.getnchannels(),
                "sample_width_bytes": source.getsampwidth(),
                "compression": source.getcomptype(),
            }
            if details["frames"] <= 0:
                raise YouTubeJobError(f"WAVE file has no audio frames: {path}")
            if details["sample_rate_hz"] != _MODEL_SAMPLE_RATE or details["channels"] != 1:
                raise YouTubeJobError(f"WAVE file must be mono {_MODEL_SAMPLE_RATE} Hz PCM: {path}")
            if details["sample_width_bytes"] != 2 or details["compression"] != "NONE":
                raise YouTubeJobError(f"WAVE file must use uncompressed 16-bit PCM: {path}")
            decoded = 0
            while True:
                block = source.readframes(65_536)
                if not block:
                    break
                decoded += len(block)
            if decoded != details["frames"] * 2:
                raise YouTubeJobError(f"WAVE file is truncated or undecodable: {path}")
        details["duration_seconds"] = round(details["frames"] / _MODEL_SAMPLE_RATE, 6)
        return details
    except YouTubeJobError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise YouTubeJobError(f"File is not a valid PCM WAVE: {path}") from exc


@dataclass(frozen=True, slots=True)
class ChunkSpec:
    index: int
    core_start: int
    core_end: int
    input_start: int
    input_end: int
    padded_frames: int
    source_path: Path
    converted_path: Path

    @property
    def input_frames(self) -> int:
        return self.input_end - self.input_start


class YouTubeJobService:
    """Durable, serialized YouTube -> OpenVoice chunks -> lossless remux pipeline."""

    def __init__(
        self,
        data_dir: str | Path,
        voicebox_client: Any,
        engine: Any,
        *,
        conversion_lock: threading.Lock | None = None,
        downloader: Downloader | None = None,
        runner: Runner = subprocess.run,
        ffmpeg_path: str | Path | None = None,
        ffprobe_path: str | Path | None = None,
        chunk_seconds: float = 30.0,
        overlap_seconds: float = 1.0,
        max_download_bytes: int = 12 * 1024**3,
        audio_processor: Any | None = None,
    ) -> None:
        if not 10.0 <= float(chunk_seconds) <= 120.0:
            raise ValueError("chunk_seconds must be between 10 and 120")
        if not 0.25 <= float(overlap_seconds) <= 3.0:
            raise ValueError("overlap_seconds must be between 0.25 and 3")
        if max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be positive")
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.jobs_dir = self.data_dir / "video_jobs"
        self.voicebox_client = voicebox_client
        self.engine = engine
        self.chunk_seconds = float(chunk_seconds)
        self.overlap_seconds = float(overlap_seconds)
        self.max_download_bytes = int(max_download_bytes)
        self.ffmpeg_path = str(ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg")
        self.ffprobe_path = str(ffprobe_path or shutil.which("ffprobe") or "ffprobe")
        self._runner = runner
        self.audio_processor = audio_processor or AudioEffectsProcessor(
            ffmpeg_path=self.ffmpeg_path,
            runner=runner,
        )
        self._uses_default_downloader = downloader is None
        self._downloader = downloader or self._download_with_yt_dlp
        self._conversion_lock = conversion_lock or threading.Lock()
        self._pipeline_lock = threading.Lock()
        # Readers and writers share this re-entrant lock. Atomic replacement
        # protects the file itself, while this also avoids a Windows sharing
        # race between a browser status request and a background-job update.
        self._manifest_lock = threading.RLock()
        self._download_updates: dict[str, tuple[float, int]] = {}

    def status(self) -> dict[str, Any]:
        yt_dlp_available = not self._uses_default_downloader or importlib.util.find_spec("yt_dlp") is not None
        yt_dlp_version: str | None = None
        patched_yt_dlp = not self._uses_default_downloader
        if yt_dlp_available and self._uses_default_downloader:
            try:
                import yt_dlp

                yt_dlp_version = str(yt_dlp.version.__version__)
                patched_yt_dlp = _numeric_version(yt_dlp_version) >= (2026, 7, 4)
            except Exception:
                yt_dlp_available = False
        node_path = shutil.which("node")
        node_version: str | None = None
        node_ready = not self._uses_default_downloader
        if self._uses_default_downloader and node_path:
            try:
                node_result = subprocess.run(
                    [node_path, "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    check=False,
                )
                node_version = node_result.stdout.strip().lstrip("v")
                node_ready = node_result.returncode == 0 and _numeric_version(node_version) >= (22,)
            except (OSError, subprocess.TimeoutExpired):
                node_ready = False
        checks = {
            "yt_dlp": yt_dlp_available,
            "yt_dlp_security_floor": patched_yt_dlp,
            "node_js_runtime": node_ready,
            "ffmpeg": Path(self.ffmpeg_path).is_file() or shutil.which(self.ffmpeg_path) is not None,
            "ffprobe": Path(self.ffprobe_path).is_file() or shutil.which(self.ffprobe_path) is not None,
        }
        return {
            "ok": True,
            "ready": all(checks.values()),
            "checks": checks,
            "yt_dlp_version": yt_dlp_version,
            "node_version": node_version,
            "ffmpeg": self.ffmpeg_path,
            "ffprobe": self.ffprobe_path,
            "output_container": "Matroska",
            "output_audio_codec": "FLAC",
            "video_reencoded": False,
            "model_sample_rate_hz": _MODEL_SAMPLE_RATE,
        }

    def create_job(
        self,
        youtube_url: str,
        profile_id: str,
        sample_id: str,
        *,
        tau: float = 0.3,
        pitch_semitones: float = 0.0,
        brightness_db: float = 0.0,
        authorized: bool = False,
    ) -> dict[str, Any]:
        if authorized is not True:
            raise ValueError("You must confirm that you own or have permission to process this video")
        url = validate_youtube_url(youtube_url)
        profile = _uuid(profile_id, "profile_id")
        sample = _uuid(sample_id, "sample_id")
        value_tau = float(tau)
        if not math.isfinite(value_tau) or not 0.0 <= value_tau <= 1.0:
            raise ValueError("tau must be between 0 and 1")
        pitch, brightness = validate_audio_adjustments(pitch_semitones, brightness_db)

        status = self.status()
        if not status["ready"]:
            missing = [name for name, ready in status["checks"].items() if not ready]
            raise YouTubeJobError(f"YouTube pipeline is not ready; failed checks: {', '.join(missing)}")

        job_id = str(uuid4())
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        created_at = _utc_now()
        manifest = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "progress_percent": 0.0,
            "created_at": created_at,
            "updated_at": created_at,
            "youtube_url": url,
            "profile_id": profile,
            "sample_id": sample,
            "tau": value_tau,
            "pitch_semitones": pitch,
            "brightness_db": brightness,
            "authorized": True,
            "chunk_seconds": self.chunk_seconds,
            "overlap_seconds": self.overlap_seconds,
            "quality": {
                "inference_precision": "fp32",
                "intermediate_audio": "PCM s16le",
                "output_audio": "lossless FLAC",
                "video": "stream copy (no re-encode)",
                "model_sample_rate_hz": _MODEL_SAMPLE_RATE,
                "pitch_engine": "FFmpeg Rubber Band high-quality, formants preserved",
                "tone_filter": "2.5 kHz high shelf",
            },
            "output_video": str(job_dir / "output.mkv"),
            "media_url": f"/api/media/video-jobs/{job_id}",
        }
        _atomic_json(job_dir / "manifest.json", manifest)
        return manifest

    def run_job(self, job_id: str) -> None:
        job_id = _uuid(job_id, "job_id")
        with self._pipeline_lock:
            manifest = self._read_manifest(job_id)
            if manifest.get("status") == "completed":
                return
            try:
                self._update(job_id, status="running", stage="downloading", progress_percent=1.0, started_at=_utc_now())
                job_dir = self._job_dir(job_id)
                download_dir = job_dir / "download"
                source_video, video_info = self._downloader(
                    manifest["youtube_url"],
                    download_dir,
                    lambda progress: self._record_download_progress(job_id, progress),
                )
                source_video = source_video.resolve(strict=True)
                if not source_video.is_relative_to(download_dir.resolve()):
                    raise YouTubeJobError("Downloader returned a file outside the job download directory")
                source_probe = self._probe(source_video)
                if not any(stream.get("codec_type") == "video" for stream in source_probe.get("streams", [])):
                    raise YouTubeJobError("Downloaded media does not contain a video stream")
                if not any(stream.get("codec_type") == "audio" for stream in source_probe.get("streams", [])):
                    raise YouTubeJobError("Downloaded media does not contain an audio stream")
                source_duration = self._duration_seconds(source_probe)
                self._update(
                    job_id,
                    stage="extracting_audio",
                    progress_percent=16.0,
                    source_video=str(source_video),
                    video=video_info,
                    source_probe=source_probe,
                )

                audio_dir = job_dir / "audio"
                audio_dir.mkdir(parents=True, exist_ok=True)
                source_wav = audio_dir / "source.wav"
                self._run_ffmpeg(
                    [
                        self.ffmpeg_path,
                        "-y",
                        "-v",
                        "error",
                        "-i",
                        str(source_video),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-sn",
                        "-dn",
                        "-ac",
                        "1",
                        "-ar",
                        str(_MODEL_SAMPLE_RATE),
                        "-c:a",
                        "pcm_s16le",
                        "-t",
                        f"{source_duration:.6f}",
                        str(source_wav),
                    ],
                    "audio extraction",
                )
                source_audio = _inspect_pcm_wav(source_wav)
                self._update(job_id, stage="preparing_chunks", progress_percent=20.0, source_audio=source_audio)
                chunks = self._create_chunks(job_dir, source_wav, source_audio["frames"])

                reference = self.voicebox_client.fetch_reference(
                    manifest["profile_id"], manifest["sample_id"], self.data_dir, overwrite=False
                )
                progress_path = job_dir / "conversion-progress.json"
                self._update(
                    job_id,
                    stage="converting_chunks",
                    progress_percent=22.0,
                    chunk_count=len(chunks),
                    chunks_completed=0,
                    reference=reference,
                )
                with self._conversion_lock:
                    batch_result = self.engine.convert_batch(
                        [(chunk.source_path, chunk.converted_path) for chunk in chunks],
                        reference["wav_path"],
                        tau=manifest["tau"],
                        overwrite=True,
                        progress_path=progress_path,
                    )

                self._update(job_id, stage="reconstructing_audio", progress_percent=88.0, batch_result=batch_result)
                converted_wav = audio_dir / "converted.wav"
                converted_audio = self._stitch_chunks(chunks, converted_wav, source_audio["frames"])
                if converted_audio["frames"] != source_audio["frames"]:
                    raise YouTubeJobError("Reconstructed audio frame count does not match the extracted source")

                post_processing: dict[str, Any] = {
                    "ok": True,
                    "applied": False,
                    "pitch_semitones": manifest.get("pitch_semitones", 0.0),
                    "pitch_scale": 1.0,
                    "brightness_db": manifest.get("brightness_db", 0.0),
                    "tempo_preserved": True,
                    "formants_preserved": True,
                    "exact_frame_match": True,
                }
                if manifest.get("pitch_semitones", 0.0) or manifest.get("brightness_db", 0.0):
                    self._update(
                        job_id,
                        stage="post_processing_audio",
                        progress_percent=90.0,
                        converted_audio=converted_audio,
                    )
                    post_processing = self.audio_processor.apply(
                        converted_wav,
                        converted_wav,
                        pitch_semitones=manifest.get("pitch_semitones", 0.0),
                        brightness_db=manifest.get("brightness_db", 0.0),
                        overwrite=True,
                    )
                    converted_audio = post_processing["audio"]
                    if converted_audio["frames"] != source_audio["frames"]:
                        raise YouTubeJobError(
                            "Post-processed audio frame count does not match the extracted source"
                        )

                output_video = job_dir / "output.mkv"
                self._update(
                    job_id,
                    stage="remuxing_video",
                    progress_percent=93.0,
                    converted_audio=converted_audio,
                    post_processing=post_processing,
                )
                self._run_ffmpeg(
                    [
                        self.ffmpeg_path,
                        "-y",
                        "-v",
                        "error",
                        "-i",
                        str(source_video),
                        "-i",
                        str(converted_wav),
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-map",
                        "0:s?",
                        "-map_metadata",
                        "0",
                        "-map_chapters",
                        "0",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "flac",
                        "-c:s",
                        "copy",
                        "-af",
                        f"apad=whole_dur={source_duration:.6f}",
                        "-t",
                        f"{source_duration:.6f}",
                        str(output_video),
                    ],
                    "lossless video remux",
                )
                self._update(job_id, stage="validating_output", progress_percent=97.0)
                output_probe = self._probe(output_video)
                self._decode_validate(output_video)
                output_duration = self._duration_seconds(output_probe)
                timeline_delta = abs(output_duration - source_duration)
                if timeline_delta > 0.05:
                    raise YouTubeJobError(
                        f"Output timeline differs from the source video by {timeline_delta:.6f} seconds"
                    )
                source_video_codec = self._first_codec(source_probe, "video")
                output_video_codec = self._first_codec(output_probe, "video")
                if source_video_codec != output_video_codec:
                    raise YouTubeJobError("Output video codec changed; stream-copy fidelity check failed")
                output_audio_codec = self._first_codec(output_probe, "audio")
                if output_audio_codec != "flac":
                    raise YouTubeJobError("Output audio is not lossless FLAC")

                completed_at = _utc_now()
                self._update(
                    job_id,
                    status="completed",
                    stage="completed",
                    progress_percent=100.0,
                    completed_at=completed_at,
                    output_video=str(output_video),
                    output_probe=output_probe,
                    validation={
                        "full_decode": True,
                        "exact_audio_frame_match": True,
                        "source_audio_frames": source_audio["frames"],
                        "converted_audio_frames": converted_audio["frames"],
                        "sample_rate_hz": _MODEL_SAMPLE_RATE,
                        "video_timeline_seconds": source_duration,
                        "output_timeline_seconds": output_duration,
                        "timeline_delta_seconds": round(timeline_delta, 6),
                        "video_stream_copied": True,
                        "output_audio_codec": "flac",
                        "pitch_and_tone_timing_preserved": True,
                    },
                )
            except Exception as exc:
                failed_at = _utc_now()
                self._update(
                    job_id,
                    status="failed",
                    stage="failed",
                    failed_at=failed_at,
                    error={"type": type(exc).__name__, "message": (str(exc).strip() or type(exc).__name__)[:2000]},
                )

    def get_job(self, job_id: str) -> dict[str, Any]:
        job_id = _uuid(job_id, "job_id")
        manifest = self._read_manifest(job_id)
        if manifest.get("stage") == "converting_chunks":
            progress_path = self._job_dir(job_id) / "conversion-progress.json"
            if progress_path.is_file():
                try:
                    progress = _read_json(progress_path)
                    completed = int(progress.get("completed", 0))
                    total = int(progress.get("total", 0))
                    manifest["chunks_completed"] = completed
                    if total > 0:
                        manifest["progress_percent"] = round(22.0 + 64.0 * completed / total, 1)
                    manifest["conversion_progress"] = progress
                except (ValueError, TypeError, YouTubeJobError):
                    pass
        return manifest

    def resolve_output(self, job_id: str) -> Path:
        job_id = _uuid(job_id, "job_id")
        path = self._job_dir(job_id) / "output.mkv"
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Completed video output does not exist for job {job_id}") from exc
        if not resolved.is_file() or not resolved.is_relative_to(self.jobs_dir.resolve()):
            raise ValueError("Video output escapes the configured jobs directory")
        return resolved

    def _job_dir(self, job_id: str) -> Path:
        path = (self.jobs_dir / job_id).resolve()
        if not path.is_relative_to(self.jobs_dir.resolve()):
            raise ValueError("Job path escapes the configured jobs directory")
        return path

    def _manifest_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "manifest.json"

    def _read_manifest(self, job_id: str) -> dict[str, Any]:
        with self._manifest_lock:
            path = self._manifest_path(job_id)
            if not path.is_file():
                raise FileNotFoundError(f"Video job does not exist: {job_id}")
            return _read_json(path)

    def _update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._manifest_lock:
            manifest = self._read_manifest(job_id)
            manifest.update(changes)
            manifest["updated_at"] = _utc_now()
            _atomic_json(self._manifest_path(job_id), manifest)
            return manifest

    def _record_download_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        downloaded = int(progress.get("downloaded_bytes") or 0)
        total = int(progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0)
        status = str(progress.get("status") or "downloading")
        percent = downloaded / total if total > 0 else 0.0
        now = time.monotonic()
        last_time, last_percent = self._download_updates.get(job_id, (0.0, -1))
        percent_whole = int(percent * 100)
        if status != "finished" and now - last_time < 1.0 and percent_whole <= last_percent:
            return
        self._download_updates[job_id] = (now, percent_whole)
        self._update(
            job_id,
            stage="downloading",
            progress_percent=round(1.0 + min(percent, 1.0) * 14.0, 1),
            download={
                "status": status,
                "downloaded_bytes": downloaded,
                "total_bytes": total or None,
                "eta_seconds": progress.get("eta"),
                "speed_bytes_per_second": progress.get("speed"),
            },
        )

    def _download_with_yt_dlp(
        self, url: str, destination: Path, progress: DownloadProgress
    ) -> tuple[Path, dict[str, Any]]:
        try:
            import yt_dlp
        except ImportError as exc:
            raise YouTubeJobError("yt-dlp is not installed in the bridge environment") from exc
        destination.mkdir(parents=True, exist_ok=True)
        options = {
            "format": "bv*+ba/b",
            "merge_output_format": "mkv",
            "outtmpl": {"default": str(destination / "source.%(ext)s")},
            "noplaylist": True,
            "ignoreconfig": True,
            "quiet": True,
            "no_warnings": True,
            "windowsfilenames": True,
            "overwrites": False,
            "continuedl": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "max_filesize": self.max_download_bytes,
            "progress_hooks": [progress],
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
        except Exception as exc:
            raise YouTubeJobError(f"yt-dlp could not download the video: {exc}") from exc
        if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"}:
            raise YouTubeJobError("YouTube URL did not resolve to one video")
        candidates = [
            path
            for path in destination.glob("source.*")
            if path.is_file() and path.suffix.lower() in _VIDEO_SUFFIXES
        ]
        if not candidates:
            raise YouTubeJobError("yt-dlp completed without producing a supported video file")
        video_path = max(candidates, key=lambda path: path.stat().st_size)
        metadata = {
            "id": info.get("id"),
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration_seconds": info.get("duration"),
            "webpage_url": info.get("webpage_url") or url,
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "source_size_bytes": video_path.stat().st_size,
        }
        return video_path, metadata

    def _run_command(self, command: list[str], label: str, *, timeout: float = 12 * 60 * 60) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise YouTubeJobError(f"{label} could not run: {exc}") from exc
        if completed.returncode != 0:
            diagnostics = (completed.stderr or completed.stdout or "no diagnostics").strip()[-4000:]
            raise YouTubeJobError(f"{label} failed: {diagnostics}")
        return completed

    def _run_ffmpeg(self, command: list[str], label: str) -> None:
        self._run_command(command, label)

    def _probe(self, path: Path) -> dict[str, Any]:
        completed = self._run_command(
            [
                self.ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration,size,format_name:stream=index,codec_type,codec_name,duration,width,height,r_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            "media probe",
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise YouTubeJobError("ffprobe returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise YouTubeJobError("ffprobe returned an invalid result")
        return payload

    def _decode_validate(self, path: Path) -> None:
        self._run_command(
            [
                self.ffmpeg_path,
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                os.devnull,
            ],
            "full output decode validation",
        )

    @staticmethod
    def _first_codec(probe: dict[str, Any], stream_type: str) -> str | None:
        for stream in probe.get("streams", []):
            if stream.get("codec_type") == stream_type:
                return stream.get("codec_name")
        return None

    @staticmethod
    def _duration_seconds(probe: dict[str, Any]) -> float:
        candidates = [
            stream.get("duration")
            for stream in probe.get("streams", [])
            if stream.get("codec_type") == "video"
        ]
        candidates.append(probe.get("format", {}).get("duration"))
        for candidate in candidates:
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                return value
        raise YouTubeJobError("Downloaded video does not report a usable duration")

    def _create_chunks(self, job_dir: Path, source_wav: Path, total_frames: int) -> list[ChunkSpec]:
        core_frames = max(
            _MODEL_FRAME_SIZE,
            round(self.chunk_seconds * _MODEL_SAMPLE_RATE / _MODEL_FRAME_SIZE) * _MODEL_FRAME_SIZE,
        )
        overlap_frames = max(
            _MODEL_FRAME_SIZE,
            round(self.overlap_seconds * _MODEL_SAMPLE_RATE / _MODEL_FRAME_SIZE) * _MODEL_FRAME_SIZE,
        )
        source_dir = job_dir / "chunks" / "source"
        converted_dir = job_dir / "chunks" / "converted"
        source_dir.mkdir(parents=True, exist_ok=True)
        converted_dir.mkdir(parents=True, exist_ok=True)
        chunks: list[ChunkSpec] = []
        with wave.open(str(source_wav), "rb") as source:
            index = 0
            core_start = 0
            while core_start < total_frames:
                core_end = min(total_frames, core_start + core_frames)
                input_start = max(0, core_start - overlap_frames)
                input_end = min(total_frames, core_end + overlap_frames)
                input_frames = input_end - input_start
                padded_frames = math.ceil(input_frames / _MODEL_FRAME_SIZE) * _MODEL_FRAME_SIZE
                source_path = source_dir / f"{index:06d}.wav"
                converted_path = converted_dir / f"{index:06d}.wav"
                source.setpos(input_start)
                frames = source.readframes(input_frames)
                if len(frames) != input_frames * 2:
                    raise YouTubeJobError(f"Could not read complete source audio for chunk {index + 1}")
                if padded_frames > input_frames:
                    frames += b"\x00\x00" * (padded_frames - input_frames)
                with wave.open(str(source_path), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(_MODEL_SAMPLE_RATE)
                    output.writeframes(frames)
                chunks.append(
                    ChunkSpec(
                        index=index,
                        core_start=core_start,
                        core_end=core_end,
                        input_start=input_start,
                        input_end=input_end,
                        padded_frames=padded_frames,
                        source_path=source_path,
                        converted_path=converted_path,
                    )
                )
                index += 1
                core_start = core_end
        if not chunks:
            raise YouTubeJobError("Extracted audio did not produce any conversion chunks")
        return chunks

    @staticmethod
    def _read_converted_samples(chunk: ChunkSpec) -> array:
        details = _inspect_pcm_wav(chunk.converted_path)
        if details["frames"] < chunk.input_frames:
            raise YouTubeJobError(
                f"Converted chunk {chunk.index + 1} is shorter than its unpadded input"
            )
        with wave.open(str(chunk.converted_path), "rb") as source:
            samples = array("h")
            samples.frombytes(source.readframes(details["frames"]))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples

    def _stitch_chunks(self, chunks: list[ChunkSpec], output_path: Path, total_frames: int) -> dict[str, Any]:
        overlap_frames = max(
            _MODEL_FRAME_SIZE,
            round(self.overlap_seconds * _MODEL_SAMPLE_RATE / _MODEL_FRAME_SIZE) * _MODEL_FRAME_SIZE,
        )
        current_chunk = chunks[0]
        current_samples = self._read_converted_samples(current_chunk)
        written = 0
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(_MODEL_SAMPLE_RATE)
            for next_chunk in chunks[1:]:
                next_samples = self._read_converted_samples(next_chunk)
                boundary = current_chunk.core_end
                fade_start = max(written, boundary - overlap_frames)
                fade_end = min(total_frames, boundary + overlap_frames)

                current_start = written - current_chunk.input_start
                current_end = fade_start - current_chunk.input_start
                if current_end > current_start:
                    output.writeframes(current_samples[current_start:current_end].tobytes())
                    written += current_end - current_start

                fade_length = fade_end - fade_start
                blended = array("h")
                if fade_length > 0:
                    denominator = max(1, fade_length - 1)
                    for offset in range(fade_length):
                        global_frame = fade_start + offset
                        left = current_samples[global_frame - current_chunk.input_start]
                        right = next_samples[global_frame - next_chunk.input_start]
                        ratio = offset / denominator
                        sample = round(left * (1.0 - ratio) + right * ratio)
                        blended.append(max(-32_768, min(32_767, sample)))
                    output.writeframes(blended.tobytes())
                    written += fade_length

                current_chunk = next_chunk
                current_samples = next_samples

            final_start = written - current_chunk.input_start
            final_end = total_frames - current_chunk.input_start
            if final_end > final_start:
                output.writeframes(current_samples[final_start:final_end].tobytes())
                written += final_end - final_start

        if written != total_frames:
            raise YouTubeJobError(
                f"Reconstructed audio has {written} frames; expected exactly {total_frames}"
            )
        return _inspect_pcm_wav(output_path)
