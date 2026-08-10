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


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


class MediaStore:
    """Store and resolve browser-provided media beneath the bridge data directory."""

    def __init__(self, data_dir: str | Path, max_input_bytes: int = 1024**3) -> None:
        if isinstance(max_input_bytes, bool) or not isinstance(max_input_bytes, int):
            raise TypeError("Maximum input size must be an integer")
        if max_input_bytes <= 0:
            raise ValueError("Maximum input size must be positive")

        self.data_dir = Path(data_dir).expanduser().resolve()
        self.max_input_bytes = max_input_bytes
        self.inputs_dir = self.data_dir / "inputs"
        self.outputs_dir = self.data_dir / "outputs"
        self.references_dir = self.data_dir / "references"

    async def store_input(
        self,
        filename: str,
        chunks: AsyncIterable[bytes],
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Stream one input to disk and publish it only after a complete upload."""
        original_name, suffix = self._safe_name(filename)
        if content_type is not None and not isinstance(content_type, str):
            raise TypeError("content_type must be a string or None")

        input_id = str(uuid4())
        self.inputs_dir.mkdir(parents=True, exist_ok=True)
        resolved_inputs_dir = self.inputs_dir.resolve(strict=True)
        if not resolved_inputs_dir.is_relative_to(self.data_dir):
            raise ValueError("Input media directory escapes the configured data directory")
        stored_path = self.inputs_dir / f"{input_id}{suffix}"
        metadata_path = self.inputs_dir / f"{input_id}.json"
        temporary_audio: Path | None = None
        temporary_metadata: Path | None = None
        published_audio = False
        published_metadata = False
        size_bytes = 0

        try:
            with tempfile.NamedTemporaryFile(
                dir=self.inputs_dir,
                prefix=f".{input_id}.",
                suffix=".upload",
                delete=False,
            ) as temporary:
                temporary_audio = Path(temporary.name)
                async for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise TypeError("Upload chunks must be bytes-like")
                    chunk_size = len(chunk)
                    if size_bytes + chunk_size > self.max_input_bytes:
                        raise ValueError(
                            f"Input exceeds the {self.max_input_bytes}-byte safety limit"
                        )
                    if chunk_size:
                        temporary.write(chunk)
                        size_bytes += chunk_size

                if size_bytes == 0:
                    raise ValueError("Input file must not be empty")
                temporary.flush()
                os.fsync(temporary.fileno())

            result: dict[str, Any] = {
                "input_id": input_id,
                "original_name": original_name,
                "stored_path": str(stored_path.resolve()),
                "media_url": f"/api/media/inputs/{input_id}",
                "size_bytes": size_bytes,
                "content_type": content_type,
            }

            os.replace(temporary_audio, stored_path)
            temporary_audio = None
            published_audio = True

            with tempfile.NamedTemporaryFile(
                dir=self.inputs_dir,
                prefix=f".{input_id}.",
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
            if temporary_audio is not None:
                temporary_audio.unlink(missing_ok=True)
            if temporary_metadata is not None:
                temporary_metadata.unlink(missing_ok=True)
            if not published_metadata:
                metadata_path.unlink(missing_ok=True)
                if published_audio:
                    stored_path.unlink(missing_ok=True)

    def resolve_input(self, input_id: str) -> Path:
        """Resolve an uploaded input ID without accepting a caller-provided path."""
        input_id = _uuid(input_id, "input_id")
        matches = [
            path
            for suffix in _AUDIO_SUFFIXES
            if (path := self.inputs_dir / f"{input_id}{suffix}").exists()
        ]
        if len(matches) != 1:
            raise FileNotFoundError(f"Input media {input_id} does not exist")
        return self._existing_contained_file(matches[0], self.inputs_dir, "Input media")

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
    def _safe_name(filename: str) -> tuple[str, str]:
        if not isinstance(filename, str):
            raise TypeError("filename must be a string")
        if "\x00" in filename:
            raise ValueError("filename must not contain a NUL character")

        # Browser uploads can include either POSIX paths or Windows fake paths.
        original_name = PurePosixPath(filename.replace("\\", "/")).name.strip()
        if not original_name or original_name in {".", ".."}:
            raise ValueError("filename must include a file name")
        suffix = Path(original_name).suffix.lower()
        if suffix not in _AUDIO_SUFFIXES:
            raise ValueError(f"Unsupported audio extension: {suffix or '(none)'}")
        return original_name, suffix

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
