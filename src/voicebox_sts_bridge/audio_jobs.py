from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from .conversion_service import ConversionService, _utc_now
from .media_store import MediaStore, _uuid


class AudioJobs:
    """Durable API job records around the existing serialized converter."""

    def __init__(self, data_dir: Path, conversions: ConversionService) -> None:
        self.root = Path(data_dir) / "audio_jobs"
        self.conversions = conversions
        self._lock = threading.Lock()
        self._record_lock = threading.Lock()

    def recover_interrupted(self) -> None:
        # The supported launcher runs one server process. Never present work
        # abandoned by a previous process as still progressing.
        for path in self.root.glob("*.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["status"] in {"queued", "running"}:
                job.update(status="failed", updated_at=_utc_now(), error={
                    "type": "Interrupted", "message": "Server stopped; submit a new job."
                })
                self._save(job)

    def _save(self, job: dict[str, Any]) -> None:
        # Windows cannot replace a file while a status reader has it open.
        with self._record_lock:
            ConversionService._write_manifest(self.root / f"{job['job_id']}.json", job)

    def create(self, options: dict[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        job_id = str(uuid4())
        job = dict(options, job_id=job_id, status="queued", created_at=_utc_now(),
                   updated_at=_utc_now(), status_url=f"/api/audio/jobs/{job_id}",
                   download_url=None)
        self._save(job)
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        job_id = _uuid(job_id, "job_id")
        with self._record_lock:
            return json.loads((self.root / f"{job_id}.json").read_text(encoding="utf-8"))

    def list(self, limit: int, offset: int) -> list[dict[str, Any]]:
        jobs = [self.get(path.stem) for path in self.root.glob("*.json")]
        jobs.sort(key=lambda job: (job["created_at"], job["job_id"]), reverse=True)
        return jobs[offset:offset + limit]

    def run(self, job_id: str, media: MediaStore) -> None:
        # Also prevents duplicate execution of a job within this process.
        with self._lock:
            job = self.get(job_id)
            if job["status"] != "queued":
                return
            job.update(status="running", updated_at=_utc_now())
            self._save(job)
            try:
                result = self.conversions.convert(
                    media.resolve_input(job["input_id"]), job["profile_id"], job["sample_id"],
                    tau=job["tau"], pitch_semitones=job["pitch_semitones"],
                    brightness_db=job["brightness_db"],
                )
                media.resolve_output(result["job_id"])
                job.update(status="completed", conversion_id=result["job_id"],
                           audio=result["result"].get("audio"),
                           download_url=f"/api/audio/jobs/{job_id}/audio")
            except Exception as exc:
                job.update(status="failed", error={
                    "type": type(exc).__name__, "message": str(exc)[:1000]
                })
            job["updated_at"] = _utc_now()
            self._save(job)
