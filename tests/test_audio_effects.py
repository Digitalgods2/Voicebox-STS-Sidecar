from __future__ import annotations

from array import array
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave

from pydantic import ValidationError

from voicebox_sts_bridge import __version__
from voicebox_sts_bridge.api import (
    ConversionRequest,
    LocalVideoJobRequest,
    YouTubeJobRequest,
    create_app,
)
from voicebox_sts_bridge.audio_effects import (
    AudioEffectsError,
    AudioEffectsProcessor,
    inspect_pcm_wav,
    validate_audio_adjustments,
)
from voicebox_sts_bridge.settings import Settings


def write_wave(path: Path, frames: int = 4_096, *, sample_rate: int = 22_050) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = array(
        "h",
        (
            round(10_000 * math.sin(2 * math.pi * 220 * index / sample_rate))
            for index in range(frames)
        ),
    )
    payload = samples.tobytes()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(payload)
    return payload


class AudioAdjustmentValidationTests(unittest.TestCase):
    def test_accepts_bounds_and_normalizes_negative_zero(self) -> None:
        self.assertEqual(validate_audio_adjustments(-6, 6), (-6.0, 6.0))
        self.assertEqual(validate_audio_adjustments(-0.0, -0.0), (0.0, 0.0))

    def test_rejects_non_finite_and_out_of_range_values(self) -> None:
        for pitch, brightness in ((float("nan"), 0), (6.1, 0), (0, float("inf")), (0, -6.1)):
            with self.subTest(pitch=pitch, brightness=brightness), self.assertRaises(ValueError):
                validate_audio_adjustments(pitch, brightness)

    def test_api_models_bound_both_controls(self) -> None:
        request = ConversionRequest(
            input_id="input",
            profile_id="profile",
            sample_id="sample",
            pitch_semitones=-2.5,
            brightness_db=-3,
        )
        self.assertEqual(request.pitch_semitones, -2.5)
        self.assertEqual(request.brightness_db, -3)
        with self.assertRaises(ValidationError):
            ConversionRequest(
                input_id="input",
                profile_id="profile",
                sample_id="sample",
                pitch_semitones=7,
            )
        with self.assertRaises(ValidationError):
            YouTubeJobRequest(
                youtube_url="https://youtu.be/abc",
                profile_id="profile",
                sample_id="sample",
                brightness_db=-7,
                authorized=True,
            )
        local_video = LocalVideoJobRequest(
            video_input_id="input",
            profile_id="profile",
            sample_id="sample",
            pitch_semitones=1.5,
            brightness_db=-2,
            authorized=True,
        )
        self.assertEqual(local_video.pitch_semitones, 1.5)
        with self.assertRaises(ValidationError):
            LocalVideoJobRequest(
                video_input_id="input",
                profile_id="profile",
                sample_id="sample",
                pitch_semitones=-7,
            )


class AudioEffectsProcessorTests(unittest.TestCase):
    def test_zero_adjustments_bypass_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            write_wave(source)

            def unexpected_runner(*_args, **_kwargs):
                raise AssertionError("FFmpeg must not run for neutral settings")

            result = AudioEffectsProcessor(runner=unexpected_runner).apply(source, source)

            self.assertFalse(result["applied"])
            self.assertEqual(result["audio"]["frames"], 4_096)

    def test_filter_chain_uses_quality_pitch_formants_tone_and_exact_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            original = write_wave(source)
            calls: list[list[str]] = []

            def runner(command, **_options):
                command = [str(item) for item in command]
                calls.append(command)
                shutil.copyfile(Path(command[command.index("-i") + 1]), Path(command[-1]))
                return subprocess.CompletedProcess(command, 0, "", "")

            result = AudioEffectsProcessor(ffmpeg_path="ffmpeg", runner=runner).apply(
                source,
                source,
                pitch_semitones=-2,
                brightness_db=-3.5,
            )

            self.assertTrue(result["applied"])
            self.assertEqual(source.read_bytes()[-len(original) :], original)
            self.assertEqual(result["audio"]["frames"], 4_096)
            filter_chain = calls[0][calls[0].index("-af") + 1]
            self.assertIn("rubberband=tempo=1", filter_chain)
            self.assertIn("formant=preserved", filter_chain)
            self.assertIn("pitchq=quality", filter_chain)
            self.assertIn("highshelf=frequency=2500", filter_chain)
            self.assertIn("gain=-3.500", filter_chain)
            self.assertIn("alimiter=limit=0.98", filter_chain)
            self.assertIn("apad=whole_len=4096", filter_chain)
            self.assertIn("atrim=end_sample=4096", filter_chain)

    def test_failed_filter_preserves_original_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            original = write_wave(source)

            def runner(command, **_options):
                return subprocess.CompletedProcess(command, 1, "", "rubberband failed")

            with self.assertRaisesRegex(AudioEffectsError, "rubberband failed"):
                AudioEffectsProcessor(runner=runner).apply(source, source, pitch_semitones=1)

            self.assertEqual(source.read_bytes()[-len(original) :], original)
            self.assertEqual(list(root.glob(".*.effects.*.wav")), [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for the application")
    def test_real_ffmpeg_preserves_exact_frames_and_decodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            write_wave(source, 22_050)

            result = AudioEffectsProcessor().apply(
                source,
                source,
                pitch_semitones=-2,
                brightness_db=-2,
            )

            self.assertEqual(result["audio"]["frames"], 22_050)
            self.assertEqual(result["audio"]["sample_rate_hz"], 22_050)
            self.assertEqual(inspect_pcm_wav(source)["duration_seconds"], 1.0)


class AdjustmentUiContractTests(unittest.TestCase):
    def test_ui_contains_both_controls_and_forwards_them_to_both_workflows(self) -> None:
        page = (
            Path(__file__).parents[1]
            / "src"
            / "voicebox_sts_bridge"
            / "static"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="pitch-correction"', page)
        self.assertIn('id="brightness"', page)
        self.assertIn("pitch_semitones", page)
        self.assertIn("brightness_db", page)
        self.assertGreaterEqual(page.count("...adjustments"), 3)
        self.assertIn("effects_chain", page)
        self.assertIn("voicebox-sts-profile-adjustments-v1", page)
        self.assertIn("Waiting for durable job status", page)
        self.assertIn('id="youtube-cache-summary"', page)
        self.assertIn('id="youtube-cache-clear"', page)
        self.assertIn("Convert using cached video", page)
        self.assertIn("youtube-source-cache-v1", page)
        self.assertIn('id="local-video-input"', page)
        self.assertIn('id="local-video-player"', page)
        self.assertIn("/api/video-inputs", page)
        self.assertIn("/api/video/jobs", page)
        self.assertIn("local-video-upload-v1", page)

    def test_backend_advertises_the_ui_compatibility_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Settings(data_dir=Path(directory) / "data"))
            route = next(route for route in app.routes if route.path == "/api/version")
            cache_route = next(
                route
                for route in app.routes
                if route.path == "/api/youtube/cache" and "GET" in route.methods
            )
            clear_route = next(
                route
                for route in app.routes
                if route.path == "/api/youtube/cache" and "DELETE" in route.methods
            )
            local_status_route = next(
                route for route in app.routes if route.path == "/api/video/status"
            )
            local_upload_route = next(
                route
                for route in app.routes
                if route.path == "/api/video-inputs" and "POST" in route.methods
            )
            local_job_route = next(
                route
                for route in app.routes
                if route.path == "/api/video/jobs" and "POST" in route.methods
            )

            response = route.endpoint()
            cache_status = cache_route.endpoint()
            clear_status = clear_route.endpoint()
            local_status = local_status_route.endpoint()

        self.assertEqual(response["version"], __version__)
        self.assertIn("resilient-video-polling-v1", response["features"])
        self.assertIn("youtube-source-cache-v1", response["features"])
        self.assertIn("local-video-upload-v1", response["features"])
        self.assertFalse(cache_status["active"])
        self.assertFalse(clear_status["cleared"])
        self.assertIn("ffmpeg", local_status["checks"])
        self.assertIn("ffprobe", local_status["checks"])
        self.assertIsNotNone(local_upload_route)
        self.assertIsNotNone(local_job_route)


if __name__ == "__main__":
    unittest.main()
