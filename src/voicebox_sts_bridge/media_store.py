from __future__ import annotations

from collections.abc import AsyncIterable
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any
from uuid import UUID, uuid4


_AUDIO_SUFFIXES = frozenset(
    {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
)
_VIDEO_SUFFIXES = frozenset(
    {
        ".avi",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ts",
        ".webm",
        ".wmv",
    }
)


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


class MediaStore:
    """Store and resolve browser-provided media beneath the bridge data directory."""

    def __init__(
        self,
        data_dir: str | Path,
        max_input_bytes: int = 1024**3,
        max_video_input_bytes: int = 12 * 1024**3,
    ) -> None:
        self._validate_limit(max_input_bytes, "Maximum audio input size")
        self._validate_limit(max_video_input_bytes, "Maximum video input size")

        self.data_dir = Path(data_dir).expanduser().resolve()
        self.max_input_bytes = max_input_bytes
        self.max_video_input_bytes = max_video_input_bytes
        self.inputs_dir = self.data_dir / "inputs"
        self.video_inputs_dir = self.data_dir / "video_inputs"
        self.outputs_dir = self.data_dir / "outputs"
        self.references_dir = self.data_dir / "references"

    async def store_input(
        self,
        filename: str,
        chunks: AsyncIterable[bytes],
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Stream one input to disk and publish it only after a complete upload."""
        return await self._store_media(
            filename=filename,
            chunks=chunks,
            content_type=content_type,
            allowed_suffixes=_AUDIO_SUFFIXES,
            media_dir=self.inputs_dir,
            max_bytes=self.max_input_bytes,
            id_field="input_id",
            media_url_prefix="/api/media/inputs",
            kind="audio",
        )

    async def store_video_input(
        self,
        filename: str,
        chunks: AsyncIterable[bytes],
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Stream one local video to the isolated video-input directory."""
        return await self._store_media(
            filename=filename,
            chunks=chunks,
            content_type=content_type,
            allowed_suffixes=_VIDEO_SUFFIXES,
            media_dir=self.video_inputs_dir,
            max_bytes=self.max_video_input_bytes,
            id_field="video_input_id",
            media_url_prefix="/api/media/video-inputs",
            kind="video",
        )

    async def _store_media(
        self,
        *,
        filename: str,
        chunks: AsyncIterable[bytes],
        content_type: str | None,
        allowed_suffixes: frozenset[str],
        media_dir: Path,
        max_bytes: int,
        id_field: str,
        media_url_prefix: str,
        kind: str,
    ) -> dict[str, Any]:
        original_name, suffix = self._safe_name(filename, allowed_suffixes, kind)
        if content_type is not None and not isinstance(content_type, str):
            raise TypeError("content_type must be a string or None")

        media_id = str(uuid4())
        media_dir.mkdir(parents=True, exist_ok=True)
        resolved_inputs_dir = media_dir.resolve(strict=True)
        if not resolved_inputs_dir.is_relative_to(self.data_dir):
            raise ValueError("Input media directory escapes the configured data directory")
        stored_path = media_dir / f"{media_id}{suffix}"
        metadata_path = media_dir / f"{media_id}.json"
        temporary_media: Path | None = None
        temporary_metadata: Path | None = None
        published_media = False
        published_metadata = False
        size_bytes = 0

        try:
            with tempfile.NamedTemporaryFile(
                dir=media_dir,
                prefix=f".{media_id}.",
                suffix=".upload",
                delete=False,
            ) as temporary:
                temporary_media = Path(temporary.name)
                async for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise TypeError("Upload chunks must be bytes-like")
                    chunk_size = len(chunk)
                    if size_bytes + chunk_size > max_bytes:
                        raise ValueError(f"Input exceeds the {max_bytes}-byte safety limit")
                    if chunk_size:
                        temporary.write(chunk)
                        size_bytes += chunk_size

                if size_bytes == 0:
                    raise ValueError("Input file must not be empty")
                temporary.flush()
                os.fsync(temporary.fileno())

            result: dict[str, Any] = {
                id_field: media_id,
                "original_name": original_name,
                "stored_path": str(stored_path.resolve()),
                "media_url": f"{media_url_prefix}/{media_id}",
                "size_bytes": size_bytes,
                "content_type": content_type,
            }

            os.replace(temporary_media, stored_path)
            temporary_media = None
            published_media = True

            with tempfile.NamedTemporaryFile(
                dir=media_dir,
                prefix=f".{media_id}.",
                suffix=".metadata.tmp",
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
            ) as temporary:
                json.dump(result, temporary, indent=2, ensure_ascii=False, allow_nan=False)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_metadata = Path(temporary.name)

            os.replace(temporary_metadata, metadata_path)
            temporary_metadata = None
            published_metadata = True
            return result
        finally:
            if temporary_media is not None:
                temporary_media.unlink(missing_ok=True)
            if temporary_metadata is not None:
                temporary_metadata.unlink(missing_ok=True)
            if not published_metadata:
                metadata_path.unlink(missing_ok=True)
                if published_media:
                    stored_path.unlink(missing_ok=True)

    def resolve_input(self, input_id: str) -> Path:
        """Resolve an uploaded input ID without accepting a caller-provided path."""
        return self._resolve_uploaded(
            input_id, "input_id", self.inputs_dir, _AUDIO_SUFFIXES, "Input media"
        )

    def resolve_video_input(self, video_input_id: str) -> Path:
        """Resolve an uploaded video ID without accepting a caller-provided path."""
        return self._resolve_uploaded(
            video_input_id,
            "video_input_id",
            self.video_inputs_dir,
            _VIDEO_SUFFIXES,
            "Video input media",
        )

    def describe_video_input(self, video_input_id: str) -> dict[str, Any]:
        """Return trusted upload metadata for a local video."""
        video_input_id = _uuid(video_input_id, "video_input_id")
        media_path = self.resolve_video_input(video_input_id)
        metadata_path = self._existing_contained_file(
            self.video_inputs_dir / f"{video_input_id}.json",
            self.video_inputs_dir,
            "Video input metadata",
        )
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Video input metadata is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("Video input metadata is invalid")
        if value.get("video_input_id") != video_input_id:
            raise ValueError("Video input metadata does not match its media ID")
        if Path(str(value.get("stored_path", ""))).resolve() != media_path:
            raise ValueError("Video input metadata does not match its media file")
        return value

    def _resolve_uploaded(
        self,
        media_id: str,
        id_label: str,
        media_dir: Path,
        suffixes: frozenset[str],
        label: str,
    ) -> Path:
        media_id = _uuid(media_id, id_label)
        matches = [
            path
            for suffix in suffixes
            if (path := media_dir / f"{media_id}{suffix}").exists()
        ]
        if len(matches) != 1:
            raise FileNotFoundError(f"{label} {media_id} does not exist")
        return self._existing_contained_file(matches[0], media_dir, label)

    def resolve_output(self, job_id: str) -> Path:
        """Resolve the standard WAV result for a conversion job."""
        job_id = _uuid(job_id, "job_id")
        return self._existing_contained_file(
            self.outputs_dir / f"{job_id}.wav", self.outputs_dir, "Output media"
        )

    def resolve_reference(self, profile_id: str, sample_id: str) -> Path:
        """Resolve a cached VoiceBox sample by its profile and sample UUIDs."""
        profile_id = _uuid(profile_id, "profile_id")
        sample_id = _uuid(sample_id, "sample_id")
        return self._existing_contained_file(
            self.references_dir / profile_id / f"{sample_id}.wav",
            self.references_dir,
            "Reference media",
        )

    @staticmethod
    def _safe_name(
        filename: str, allowed_suffixes: frozenset[str], kind: str
    ) -> tuple[str, str]:
        if not isinstance(filename, str):
            raise TypeError("filename must be a string")
        if "\x00" in filename:
            raise ValueError("filename must not contain a NUL character")

        # Browser uploads can include either POSIX paths or Windows fake paths.
        original_name = PurePosixPath(filename.replace("\\", "/")).name.strip()
        if not original_name or original_name in {".", ".."}:
            raise ValueError("filename must include a file name")
        suffix = Path(original_name).suffix.lower()
        if suffix not in allowed_suffixes:
            raise ValueError(f"Unsupported {kind} extension: {suffix or '(none)'}")
        return original_name, suffix

    @staticmethod
    def _validate_limit(value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{label} must be an integer")
        if value <= 0:
            raise ValueError(f"{label} must be positive")

    def _existing_contained_file(self, path: Path, root: Path, label: str) -> Path:
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"{label} does not exist") from exc

        resolved_root = root.resolve()
        if not resolved_root.is_relative_to(self.data_dir) or not resolved.is_relative_to(
            resolved_root
        ):
            raise ValueError(f"{label} escapes its media directory")
        if not resolved.is_file():
            raise FileNotFoundError(f"{label} does not exist")
        return resolved
