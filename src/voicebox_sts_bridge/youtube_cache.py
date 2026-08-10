from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4


class YouTubeCacheError(RuntimeError):
    """Raised when the single-entry YouTube source cache is unsafe or invalid."""


_CACHE_SCHEMA_VERSION = 1
_VIDEO_SUFFIXES = {".mkv", ".mp4", ".webm", ".mov"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        raise YouTubeCacheError(f"YouTube cache metadata is missing or invalid: {path}") from last_error
    if not isinstance(value, dict):
        raise YouTubeCacheError("YouTube cache metadata is not a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while block := source.read(8 * 1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise YouTubeCacheError(f"Could not hash cached YouTube media: {path}") from exc
    return digest.hexdigest()


class YouTubeSourceCache:
    """Maintain exactly one validated, reusable YouTube source-video entry."""

    def __init__(self, data_dir: str | Path, *, max_media_bytes: int) -> None:
        if max_media_bytes <= 0:
            raise ValueError("max_media_bytes must be positive")
        self.root = Path(data_dir).expanduser().resolve() / "youtube_cache"
        self.current_dir = self.root / "current"
        self.max_media_bytes = int(max_media_bytes)
        self._lock = threading.RLock()
        with self._lock:
            self._recover_locked()

    def prepare_staging(self, job_id: str) -> Path:
        """Return a clean, non-active staging directory for one download."""
        with self._lock:
            self._recover_locked()
            staging = self.root / f".staging-{job_id}"
            if staging.exists() or staging.is_symlink():
                self._remove_managed_dir(staging)
            return staging

    def discard_staging(self, staging: str | Path) -> None:
        with self._lock:
            path = Path(staging).expanduser().resolve(strict=False)
            if path.exists() or path.is_symlink():
                self._remove_managed_dir(path)

    def lookup(self, video_id: str) -> dict[str, Any] | None:
        """Return the active entry when its canonical video ID matches."""
        with self._lock:
            self._recover_locked()
            if not self.current_dir.exists():
                return None
            try:
                manifest, media = self._entry_locked()
            except YouTubeCacheError:
                self._remove_managed_dir(self.current_dir)
                return None
            if manifest["video_id"] != video_id:
                return None
            return {**manifest, "source_path": str(media)}

    def record_hit(self, video_id: str) -> dict[str, Any]:
        """Persist successful reuse only after the caller has probed the media."""
        with self._lock:
            manifest, media = self._entry_locked()
            if manifest["video_id"] != video_id:
                raise YouTubeCacheError("Active YouTube cache changed before reuse was recorded")
            manifest["last_used_at"] = _utc_now()
            manifest["hit_count"] = int(manifest.get("hit_count", 0)) + 1
            _atomic_json(self.current_dir / "manifest.json", manifest)
            return {**manifest, "source_path": str(media)}

    def publish(
        self,
        staging: str | Path,
        source_media: str | Path,
        *,
        video_id: str,
        source_url: str,
        video: dict[str, Any],
        source_probe: dict[str, Any],
        yt_dlp_version: str | None,
    ) -> dict[str, Any]:
        """Validate and atomically promote a staged download to the active entry."""
        with self._lock:
            staging_path = Path(staging).expanduser().resolve(strict=True)
            expected_parent = self.root.resolve(strict=False)
            if staging_path.parent != expected_parent or not staging_path.name.startswith(".staging-"):
                raise YouTubeCacheError("YouTube cache staging path is outside the managed cache")
            media = Path(source_media).expanduser().resolve(strict=True)
            if not media.is_file() or media.is_symlink() or not media.is_relative_to(staging_path):
                raise YouTubeCacheError("Downloaded YouTube media is outside its staging directory")
            if media.suffix.lower() not in _VIDEO_SUFFIXES:
                raise YouTubeCacheError("Downloaded YouTube media uses an unsupported container")
            stat = media.stat()
            if stat.st_size <= 0 or stat.st_size > self.max_media_bytes:
                raise YouTubeCacheError("Downloaded YouTube media exceeds the configured cache limit")

            cached_at = _utc_now()
            manifest = {
                "schema_version": _CACHE_SCHEMA_VERSION,
                "video_id": video_id,
                "source_url": source_url,
                "media_file": media.name,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": _sha256(media),
                "cached_at": cached_at,
                "last_used_at": cached_at,
                "hit_count": 0,
                "yt_dlp_version": yt_dlp_version,
                "video": video,
                "source_probe": source_probe,
                "validated": True,
            }
            _atomic_json(staging_path / "manifest.json", manifest)

            self.root.mkdir(parents=True, exist_ok=True)
            retired = self.root / f".retired-{uuid4()}"
            had_current = self.current_dir.exists() or self.current_dir.is_symlink()
            if had_current:
                os.replace(self.current_dir, retired)
            try:
                os.replace(staging_path, self.current_dir)
            except Exception:
                if had_current and retired.exists() and not self.current_dir.exists():
                    os.replace(retired, self.current_dir)
                raise
            if retired.exists() or retired.is_symlink():
                self._remove_managed_dir(retired)

            active_manifest, active_media = self._entry_locked()
            return {**active_manifest, "source_path": str(active_media)}

    def status(self) -> dict[str, Any]:
        with self._lock:
            if not self.current_dir.exists():
                return {"ok": True, "active": False}
            try:
                manifest, _media = self._entry_locked()
            except YouTubeCacheError as exc:
                return {"ok": True, "active": False, "invalid": True, "error": str(exc)}
            return self._public_status(manifest)

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._recover_locked()
            cleared = self.current_dir.exists() or self.current_dir.is_symlink()
            if cleared:
                self._remove_managed_dir(self.current_dir)
            for transient in self._transient_dirs():
                self._remove_managed_dir(transient)
            return {"ok": True, "cleared": cleared, "cache": {"ok": True, "active": False}}

    def _entry_locked(self) -> tuple[dict[str, Any], Path]:
        if not self.current_dir.is_dir() or self.current_dir.is_symlink():
            raise YouTubeCacheError("Active YouTube cache is not a regular directory")
        manifest = _read_json(self.current_dir / "manifest.json")
        if manifest.get("schema_version") != _CACHE_SCHEMA_VERSION:
            raise YouTubeCacheError("YouTube cache schema is unsupported")
        video_id = manifest.get("video_id")
        if not isinstance(video_id, str) or not video_id:
            raise YouTubeCacheError("YouTube cache is missing its video ID")
        media_name = manifest.get("media_file")
        if not isinstance(media_name, str) or Path(media_name).name != media_name:
            raise YouTubeCacheError("YouTube cache contains an unsafe media filename")
        media = (self.current_dir / media_name).resolve(strict=True)
        current_resolved = self.current_dir.resolve(strict=True)
        if not media.is_relative_to(current_resolved) or not media.is_file() or media.is_symlink():
            raise YouTubeCacheError("YouTube cache media is missing or unsafe")
        stat = media.stat()
        expected_size = manifest.get("size_bytes")
        if not isinstance(expected_size, int) or stat.st_size != expected_size:
            raise YouTubeCacheError("YouTube cache media size changed")
        if stat.st_size <= 0 or stat.st_size > self.max_media_bytes:
            raise YouTubeCacheError("YouTube cache media exceeds the configured limit")
        expected_hash = manifest.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise YouTubeCacheError("YouTube cache checksum is missing")
        if stat.st_mtime_ns != manifest.get("mtime_ns"):
            if _sha256(media) != expected_hash:
                raise YouTubeCacheError("YouTube cache checksum changed")
            manifest["mtime_ns"] = stat.st_mtime_ns
            _atomic_json(self.current_dir / "manifest.json", manifest)
        return manifest, media

    def _recover_locked(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        retired = sorted(
            (path for path in self.root.iterdir() if path.name.startswith(".retired-")),
            key=lambda path: path.lstat().st_mtime_ns,
            reverse=True,
        )
        if not self.current_dir.exists() and retired:
            os.replace(retired.pop(0), self.current_dir)
        for transient in [*retired, *self._staging_dirs()]:
            self._remove_managed_dir(transient)

    def _transient_dirs(self) -> list[Path]:
        return [
            path
            for path in self.root.iterdir()
            if path.name.startswith((".staging-", ".retired-"))
        ]

    def _staging_dirs(self) -> list[Path]:
        return [path for path in self.root.iterdir() if path.name.startswith(".staging-")]

    def _remove_managed_dir(self, target: Path) -> None:
        absolute = Path(os.path.abspath(target))
        root = Path(os.path.abspath(self.root))
        current = Path(os.path.abspath(self.current_dir))
        allowed = absolute == current or (
            absolute.parent == root and absolute.name.startswith((".staging-", ".retired-"))
        )
        if not allowed:
            raise YouTubeCacheError(f"Refusing to remove a path outside the YouTube cache: {absolute}")
        if absolute.is_symlink() or absolute.is_file():
            absolute.unlink(missing_ok=True)
        elif absolute.is_dir():
            shutil.rmtree(absolute)

    @staticmethod
    def _public_status(manifest: dict[str, Any]) -> dict[str, Any]:
        video = manifest.get("video") if isinstance(manifest.get("video"), dict) else {}
        return {
            "ok": True,
            "active": True,
            "video_id": manifest["video_id"],
            "title": video.get("title"),
            "channel": video.get("channel"),
            "duration_seconds": video.get("duration_seconds"),
            "size_bytes": manifest["size_bytes"],
            "cached_at": manifest["cached_at"],
            "last_used_at": manifest["last_used_at"],
            "hit_count": manifest["hit_count"],
            "validated": bool(manifest.get("validated")),
        }
