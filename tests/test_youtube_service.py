from __future__ import annotations

from array import array
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

from voicebox_sts_bridge.youtube_service import (
    YouTubeJobError,
    YouTubeJobService,
    _read_json,
    validate_youtube_url,
    youtube_video_id,
)


def write_wave(path: Path, frames: int, *, sample_rate: int = 22_050) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = array("h", ((index % 20_000) - 10_000 for index in range(frames)))
    payload = samples.tobytes()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(payload)
    return payload


class FakeVoiceBox:
    def __init__(self, reference: Path) -> None:
        self.reference = reference

    def fetch_reference(self, profile_id, sample_id, data_dir, *, overwrite=False):
        return {
            "profile_id": profile_id,
            "sample_id": sample_id,
            "wav_path": str(self.reference),
            "cached": True,
        }


class FakeBatchEngine:
    def __init__(self) -> None:
        self.calls = []

    def convert_batch(self, conversions, target_reference, **options):
        self.calls.append((list(conversions), target_reference, options))
        for source, output in conversions:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
        progress_path = options.get("progress_path")
        if progress_path:
            Path(progress_path).write_text(
                json.dumps({"status": "completed", "completed": len(conversions), "total": len(conversions)}),
                encoding="utf-8",
            )
        return {"ok": True, "operation": "convert-batch", "chunks_completed": len(conversions)}


class FakeAudioProcessor:
    def __init__(self) -> None:
        self.calls = []

    def apply(self, input_audio, output_audio, **options):
        self.calls.append((Path(input_audio), Path(output_audio), options))
        with wave.open(str(input_audio), "rb") as audio:
            frames = audio.getnframes()
            sample_rate = audio.getframerate()
        return {
            "ok": True,
            "applied": True,
            "output_path": str(output_audio),
            "pitch_semitones": options["pitch_semitones"],
            "brightness_db": options["brightness_db"],
            "tempo_preserved": True,
            "formants_preserved": True,
            "exact_frame_match": True,
            "audio": {
                "size_bytes": Path(input_audio).stat().st_size,
                "frames": frames,
                "sample_rate_hz": sample_rate,
                "channels": 1,
                "sample_width_bytes": 2,
                "compression": "NONE",
                "duration_seconds": round(frames / sample_rate, 6),
            },
        }


class YouTubeUrlTests(unittest.TestCase):
    def test_accepts_direct_video_urls_and_removes_fragments(self) -> None:
        self.assertEqual(
            validate_youtube_url("https://youtu.be/abc123?t=2#chapter"),
            "https://youtu.be/abc123?t=2",
        )
        self.assertEqual(
            validate_youtube_url("https://www.youtube.com/watch?v=abc123&list=queue"),
            "https://www.youtube.com/watch?v=abc123&list=queue",
        )
        self.assertEqual(
            validate_youtube_url("https://youtube.com/shorts/abc123"),
            "https://youtube.com/shorts/abc123",
        )

    def test_rejects_non_youtube_and_non_video_pages(self) -> None:
        for value in (
            "http://www.youtube.com/watch?v=abc",
            "https://youtube.example/watch?v=abc",
            "https://127.0.0.1/watch?v=abc",
            "https://www.youtube.com/playlist?list=abc",
            "https://www.youtube.com/@channel",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_youtube_url(value)

    def test_extracts_one_canonical_id_from_supported_url_forms(self) -> None:
        for value in (
            "https://youtu.be/abc123?t=4",
            "https://www.youtube.com/watch?v=abc123&list=queue",
            "https://youtube.com/shorts/abc123",
            "https://youtube.com/live/abc123",
            "https://youtube.com/embed/abc123",
        ):
            with self.subTest(value=value):
                self.assertEqual(youtube_video_id(value), "abc123")


class DurableManifestTests(unittest.TestCase):
    def test_read_json_retries_a_transient_windows_file_error(self) -> None:
        with (
            patch.object(
                Path,
                "read_text",
                side_effect=[PermissionError("temporarily busy"), '{"status": "running"}'],
            ),
            patch("voicebox_sts_bridge.youtube_service.time.sleep") as sleep,
        ):
            result = _read_json(Path("manifest.json"))

        self.assertEqual(result, {"status": "running"})
        sleep.assert_called_once_with(0.025)


class YouTubeAudioTimelineTests(unittest.TestCase):
    def test_overlapped_chunks_reconstruct_exact_original_frames_and_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            total_frames = 22_050 * 21 + 73
            original = write_wave(source, total_frames)
            service = YouTubeJobService(
                root / "data",
                object(),
                object(),
                downloader=lambda *_: None,  # type: ignore[arg-type,return-value]
                ffmpeg_path=sys.executable,
                ffprobe_path=sys.executable,
                chunk_seconds=10,
                overlap_seconds=0.5,
            )

            chunks = service._create_chunks(root / "job", source, total_frames)
            self.assertEqual(len(chunks), 3)
            for chunk in chunks:
                self.assertEqual(chunk.padded_frames % 256, 0)
                shutil.copyfile(chunk.source_path, chunk.converted_path)

            reconstructed = root / "reconstructed.wav"
            details = service._stitch_chunks(chunks, reconstructed, total_frames)
            with wave.open(str(reconstructed), "rb") as output:
                rebuilt = output.readframes(output.getnframes())

            self.assertEqual(details["frames"], total_frames)
            self.assertEqual(rebuilt, original)


class YouTubeJobPipelineTests(unittest.TestCase):
    def test_job_runs_download_extract_batch_stitch_lossless_remux_and_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.wav"
            write_wave(reference, 22_050)
            source_frames = 22_050 * 11 + 19
            download_calls: list[str] = []

            def downloader(url, destination, progress):
                download_calls.append(url)
                destination.mkdir(parents=True)
                source_video = destination / "source.mkv"
                source_video.write_bytes(b"mock source video")
                progress({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
                progress({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})
                return source_video, {"id": "abc123", "title": "Authorized test", "webpage_url": url}

            def runner(command, **_options):
                command = [str(item) for item in command]
                if "-show_entries" in command:
                    target = Path(command[-1])
                    output = target.name == "output.mkv"
                    payload = {
                        "format": {"duration": "11.1", "size": str(target.stat().st_size)},
                        "streams": [
                            {"index": 0, "codec_type": "video", "codec_name": "h264"},
                            {"index": 1, "codec_type": "audio", "codec_name": "flac" if output else "aac"},
                        ],
                    }
                    return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
                destination = Path(command[-1])
                if destination.name == "source.wav":
                    write_wave(destination, source_frames)
                elif destination.name == "output.mkv":
                    destination.write_bytes(b"mock lossless remux")
                return subprocess.CompletedProcess(command, 0, "", "")

            engine = FakeBatchEngine()
            audio_processor = FakeAudioProcessor()
            service = YouTubeJobService(
                root / "data",
                FakeVoiceBox(reference),
                engine,
                downloader=downloader,
                runner=runner,
                ffmpeg_path=sys.executable,
                ffprobe_path=sys.executable,
                chunk_seconds=10,
                overlap_seconds=0.5,
                audio_processor=audio_processor,
            )
            profile_id, sample_id = str(uuid4()), str(uuid4())
            manifest = service.create_job(
                "https://www.youtube.com/watch?v=abc123",
                profile_id,
                sample_id,
                pitch_semitones=-2,
                brightness_db=-3,
                authorized=True,
            )

            service.run_job(manifest["job_id"])
            completed = service.get_job(manifest["job_id"])

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["source_type"], "youtube")
            self.assertEqual(completed["progress_percent"], 100.0)
            self.assertTrue(completed["validation"]["full_decode"])
            self.assertTrue(completed["validation"]["exact_audio_frame_match"])
            self.assertEqual(completed["validation"]["source_audio_frames"], source_frames)
            self.assertEqual(completed["validation"]["converted_audio_frames"], source_frames)
            self.assertEqual(len(engine.calls), 1)
            self.assertEqual(len(engine.calls[0][0]), 2)
            self.assertEqual(completed["pitch_semitones"], -2.0)
            self.assertEqual(completed["brightness_db"], -3.0)
            self.assertTrue(completed["post_processing"]["applied"])
            self.assertEqual(len(audio_processor.calls), 1)
            self.assertEqual(
                audio_processor.calls[0][2],
                {"pitch_semitones": -2.0, "brightness_db": -3.0, "overwrite": True},
            )
            self.assertTrue(service.resolve_output(manifest["job_id"]).is_file())
            self.assertFalse(completed["cache"]["hit"])
            self.assertEqual(completed["cache"]["status"], "downloaded_and_cached")

            rerun = service.create_job(
                "https://youtu.be/abc123?t=4",
                profile_id,
                sample_id,
                pitch_semitones=-1,
                brightness_db=-2,
                authorized=True,
            )
            self.assertTrue(rerun["cache"]["candidate_hit"])
            service.run_job(rerun["job_id"])
            rerun_completed = service.get_job(rerun["job_id"])

            self.assertEqual(len(download_calls), 1)
            self.assertTrue(rerun_completed["cache"]["hit"])
            self.assertEqual(rerun_completed["download"]["status"], "cache_hit")
            self.assertEqual(service.cache_status()["hit_count"], 1)
            self.assertEqual(len(engine.calls), 2)
            first_output = service.resolve_output(manifest["job_id"])
            rerun_output = service.resolve_output(rerun["job_id"])

            cleared = service.clear_cache()

            self.assertTrue(cleared["cleared"])
            self.assertTrue(first_output.is_file())
            self.assertTrue(rerun_output.is_file())

    def test_local_upload_uses_same_timeline_pipeline_without_downloader_or_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            reference = root / "reference.wav"
            write_wave(reference, 22_050)
            source_frames = 22_050 * 11 + 19
            video_input_id = str(uuid4())
            source_video = data_dir / "video_inputs" / f"{video_input_id}.mp4"
            source_video.parent.mkdir(parents=True)
            source_video.write_bytes(b"mock local source video")

            def downloader(*_args):
                raise AssertionError("A local video job must never invoke yt-dlp")

            def runner(command, **_options):
                command = [str(item) for item in command]
                if "-show_entries" in command:
                    target = Path(command[-1])
                    output = target.name == "output.mkv"
                    payload = {
                        "format": {"duration": "11.1", "size": str(target.stat().st_size)},
                        "streams": [
                            {"index": 0, "codec_type": "video", "codec_name": "h264"},
                            {
                                "index": 1,
                                "codec_type": "audio",
                                "codec_name": "flac" if output else "aac",
                            },
                        ],
                    }
                    return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
                destination = Path(command[-1])
                if destination.name == "source.wav":
                    write_wave(destination, source_frames)
                elif destination.name == "output.mkv":
                    destination.write_bytes(b"mock local lossless remux")
                return subprocess.CompletedProcess(command, 0, "", "")

            engine = FakeBatchEngine()
            service = YouTubeJobService(
                data_dir,
                FakeVoiceBox(reference),
                engine,
                downloader=downloader,
                runner=runner,
                ffmpeg_path=sys.executable,
                ffprobe_path=sys.executable,
                chunk_seconds=10,
                overlap_seconds=0.5,
            )
            manifest = service.create_local_job(
                source_video,
                video_input_id,
                "Authorized local clip.mp4",
                str(uuid4()),
                str(uuid4()),
                authorized=True,
            )

            service.run_job(manifest["job_id"])
            completed = service.get_job(manifest["job_id"])

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["source_type"], "local_upload")
            self.assertEqual(completed["video"]["source"], "local_upload")
            self.assertEqual(completed["cache"]["status"], "not_applicable")
            self.assertEqual(completed["validation"]["source_audio_frames"], source_frames)
            self.assertEqual(completed["validation"]["converted_audio_frames"], source_frames)
            self.assertTrue(completed["validation"]["video_stream_copied"])
            self.assertEqual(len(engine.calls), 1)
            self.assertEqual(len(engine.calls[0][0]), 2)
            self.assertTrue(service.resolve_output(manifest["job_id"]).is_file())
            self.assertFalse(service.cache_status()["active"])

            with self.assertRaisesRegex(ValueError, "own or have permission"):
                service.create_local_job(
                    source_video,
                    video_input_id,
                    "Authorized local clip.mp4",
                    str(uuid4()),
                    str(uuid4()),
                )

    def test_failed_new_download_preserves_the_existing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def failed_downloader(*_args):
                raise YouTubeJobError("simulated rate limit")

            service = YouTubeJobService(
                root / "data",
                object(),
                object(),
                downloader=failed_downloader,
                ffmpeg_path=sys.executable,
                ffprobe_path=sys.executable,
            )
            staging = service.cache.prepare_staging("seed")
            staging.mkdir(parents=True)
            source = staging / "source.mkv"
            source.write_bytes(b"existing authorized video")
            service.cache.publish(
                staging,
                source,
                video_id="existing",
                source_url="https://youtu.be/existing",
                video={"id": "existing", "title": "Existing"},
                source_probe={
                    "format": {"duration": "10.0", "size": "25"},
                    "streams": [
                        {"codec_type": "video", "codec_name": "h264"},
                        {"codec_type": "audio", "codec_name": "aac"},
                    ],
                },
                yt_dlp_version=None,
            )
            job = service.create_job(
                "https://youtu.be/replacement",
                str(uuid4()),
                str(uuid4()),
                authorized=True,
            )

            service.run_job(job["job_id"])

            self.assertEqual(service.get_job(job["job_id"])["status"], "failed")
            self.assertEqual(service.cache_status()["video_id"], "existing")

    def test_job_requires_rights_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = YouTubeJobService(
                Path(directory) / "data",
                object(),
                object(),
                downloader=lambda *_: None,  # type: ignore[arg-type,return-value]
                ffmpeg_path=sys.executable,
                ffprobe_path=sys.executable,
            )
            with self.assertRaisesRegex(ValueError, "own or have permission"):
                service.create_job(
                    "https://youtu.be/abc123",
                    str(uuid4()),
                    str(uuid4()),
                    authorized=False,
                )


if __name__ == "__main__":
    unittest.main()
