from __future__ import annotations

import mimetypes
from pathlib import Path
import threading
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .audio_effects import AudioEffectsError, MAX_BRIGHTNESS_DB, MAX_PITCH_SEMITONES
from .conversion_service import ConversionService
from .media_store import MediaStore
from .openvoice_engine import OpenVoiceEngine, OpenVoiceError
from .settings import Settings
from .voicebox_client import VoiceBoxClient, VoiceBoxError
from .youtube_service import YouTubeJobError, YouTubeJobService


class ReferenceRequest(BaseModel):
    profile_id: str
    sample_id: str
    overwrite: bool = False


class ConversionRequest(BaseModel):
    input_id: str
    profile_id: str
    sample_id: str
    tau: float = Field(default=0.3, ge=0.0, le=1.0)
    pitch_semitones: float = Field(
        default=0.0, ge=-MAX_PITCH_SEMITONES, le=MAX_PITCH_SEMITONES
    )
    brightness_db: float = Field(default=0.0, ge=-MAX_BRIGHTNESS_DB, le=MAX_BRIGHTNESS_DB)


class YouTubeJobRequest(BaseModel):
    youtube_url: str = Field(min_length=1, max_length=2048)
    profile_id: str
    sample_id: str
    tau: float = Field(default=0.3, ge=0.0, le=1.0)
    pitch_semitones: float = Field(
        default=0.0, ge=-MAX_PITCH_SEMITONES, le=MAX_PITCH_SEMITONES
    )
    brightness_db: float = Field(default=0.0, ge=-MAX_BRIGHTNESS_DB, le=MAX_BRIGHTNESS_DB)
    authorized: bool = False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="VoiceBox STS Bridge", version="0.2.0")
    client = VoiceBoxClient(
        settings.voicebox_base_url,
        timeout_seconds=settings.request_timeout_seconds,
        max_reference_bytes=settings.max_reference_bytes,
    )
    engine = OpenVoiceEngine()
    conversion_lock = threading.Lock()
    conversions = ConversionService(
        settings.data_dir,
        client,
        engine,
        conversion_lock=conversion_lock,
    )
    youtube = YouTubeJobService(
        settings.data_dir,
        client,
        engine,
        conversion_lock=conversion_lock,
    )
    media = MediaStore(settings.data_dir)

    def call(operation: Any) -> Any:
        try:
            return operation()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except VoiceBoxError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def engine_call(operation: Any) -> Any:
        try:
            return operation()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OpenVoiceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        page = Path(__file__).with_name("static") / "index.html"
        return page.read_text(encoding="utf-8")

    @app.get("/api/voicebox/health")
    def health() -> dict[str, Any]:
        return call(client.health)

    @app.get("/api/profiles")
    def profiles() -> list[dict[str, Any]]:
        return call(client.profiles)

    @app.get("/api/engine/status")
    def engine_status() -> dict[str, Any]:
        return engine_call(engine.status)

    @app.post("/api/engine/probe")
    def engine_probe() -> dict[str, Any]:
        return engine_call(engine.probe)

    @app.get("/api/profiles/{profile_id}/samples")
    def samples(profile_id: str) -> list[dict[str, Any]]:
        return call(lambda: client.samples(profile_id))

    @app.post("/api/references")
    def fetch_reference(request: ReferenceRequest) -> dict[str, Any]:
        return call(
            lambda: client.fetch_reference(
                request.profile_id,
                request.sample_id,
                settings.data_dir,
                overwrite=request.overwrite,
            )
        )

    @app.post("/api/inputs")
    async def upload_input(
        request: Request,
        filename: str = Query(min_length=1, max_length=255),
    ) -> dict[str, Any]:
        async def chunks() -> Any:
            async for chunk in request.stream():
                if chunk:
                    yield chunk

        try:
            return await media.store_input(filename, chunks(), request.headers.get("content-type"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not store input: {exc}") from exc

    def media_response(path: Path) -> FileResponse:
        media_type, _ = mimetypes.guess_type(path.name)
        return FileResponse(
            path,
            media_type=media_type or "application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "Accept-Ranges": "bytes",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/media/inputs/{input_id}", response_class=FileResponse)
    def input_media(input_id: str) -> FileResponse:
        try:
            return media_response(media.resolve_input(input_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/media/outputs/{job_id}", response_class=FileResponse)
    def output_media(job_id: str) -> FileResponse:
        try:
            return media_response(media.resolve_output(job_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/media/references/{profile_id}/{sample_id}", response_class=FileResponse)
    def reference_media(profile_id: str, sample_id: str) -> FileResponse:
        try:
            return media_response(media.resolve_reference(profile_id, sample_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/conversions")
    def convert(request: ConversionRequest) -> dict[str, Any]:
        try:
            return conversions.convert(
                media.resolve_input(request.input_id),
                request.profile_id,
                request.sample_id,
                tau=request.tau,
                pitch_semitones=request.pitch_semitones,
                brightness_db=request.brightness_db,
            )
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VoiceBoxError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except OpenVoiceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AudioEffectsError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/youtube/status")
    def youtube_status() -> dict[str, Any]:
        return youtube.status()

    @app.post("/api/youtube/jobs", status_code=202)
    def create_youtube_job(
        request: YouTubeJobRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            job = youtube.create_job(
                request.youtube_url,
                request.profile_id,
                request.sample_id,
                tau=request.tau,
                pitch_semitones=request.pitch_semitones,
                brightness_db=request.brightness_db,
                authorized=request.authorized,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except YouTubeJobError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        background_tasks.add_task(youtube.run_job, job["job_id"])
        return job

    @app.get("/api/youtube/jobs/{job_id}")
    def youtube_job(job_id: str) -> dict[str, Any]:
        try:
            return youtube.get_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except YouTubeJobError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/media/video-jobs/{job_id}", response_class=FileResponse)
    def youtube_output_media(job_id: str) -> FileResponse:
        try:
            return media_response(youtube.resolve_output(job_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


app = create_app()
