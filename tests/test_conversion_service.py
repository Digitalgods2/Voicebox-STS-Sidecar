from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from voicebox_sts_bridge.conversion_service import ConversionService


class FakeVoiceBoxClient:
    def __init__(self, reference: Path) -> None:
        self.reference = reference
        self.calls: list[tuple[str, str, Path, bool]] = []

    def fetch_reference(
        self,
        profile_id: str,
        sample_id: str,
        data_dir: Path,
        *,
        overwrite: bool = False,
    ) -> dict[str, object]:
        self.calls.append((profile_id, sample_id, data_dir, overwrite))
        return {
            "profile_id": profile_id,
            "sample_id": sample_id,
            "wav_path": self.reference,
            "cached": True,
        }


class FakeEngine:
    def __init__(self, *, failure: Exception | None = None, delay: float = 0.0) -> None:
        self.failure = failure
        self.delay = delay
        self.calls: list[tuple[object, object, object, float, bool]] = []
        self._activity_lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def convert(
        self,
        source_audio: object,
        target_reference: object,
        output_audio: object,
        *,
        tau: float,
        overwrite: bool,
    ) -> dict[str, object]:
        self.calls.append((source_audio, target_reference, output_audio, tau, overwrite))
        with self._activity_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.failure is not None:
                raise self.failure
            destination = Path(output_audio)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"mock wav")
            return {"ok": True, "output_path": destination, "audio": {"duration_seconds": 1.25}}
        finally:
            with self._activity_lock:
                self.active -= 1


class FakeAudioProcessor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, float, float, bool]] = []

    def apply(
        self,
        input_audio,
        output_audio,
        *,
        pitch_semitones,
        brightness_db,
        overwrite,
    ):
        self.calls.append(
            (input_audio, output_audio, pitch_semitones, brightness_db, overwrite)
        )
        return {
            "ok": True,
            "applied": True,
            "output_path": str(output_audio),
            "pitch_semitones": pitch_semitones,
            "pitch_scale": 0.890898718,
            "brightness_db": brightness_db,
            "tempo_preserved": True,
            "formants_preserved": True,
            "exact_frame_match": True,
            "audio": {"duration_seconds": 1.25, "frames": 27_562},
        }


class ConversionServiceTests(unittest.TestCase):
    def test_completed_job_uses_default_output_and_durable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            source = Path(directory) / "source.wav"
            reference = Path(directory) / "reference.wav"
            source.write_bytes(b"source")
            reference.write_bytes(b"reference")
            client = FakeVoiceBoxClient(reference)
            engine = FakeEngine()
            service = ConversionService(data_dir, client, engine)

            manifest = service.convert(source, "profile-1", "sample-1")

            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(Path(manifest["output_audio"]).parent, data_dir.resolve() / "outputs")
            self.assertTrue(Path(manifest["output_audio"]).is_file())
            self.assertEqual(manifest["reference"]["wav_path"], str(reference))
            self.assertEqual(manifest["result"]["audio"]["duration_seconds"], 1.25)
            self.assertIn("completed_at", manifest)
            persisted_path = data_dir / "jobs" / f"{manifest['job_id']}.json"
            self.assertEqual(json.loads(persisted_path.read_text(encoding="utf-8")), manifest)
            json.dumps(manifest)
            self.assertEqual(client.calls, [("profile-1", "sample-1", data_dir.resolve(), False)])
            self.assertEqual(list((data_dir / "jobs").glob("*.tmp")), [])

    def test_explicit_output_and_options_are_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            reference = root / "reference.wav"
            output = root / "custom" / "result.wav"
            source.write_bytes(b"source")
            reference.write_bytes(b"reference")
            client = FakeVoiceBoxClient(reference)
            engine = FakeEngine()

            manifest = ConversionService(root / "data", client, engine).convert(
                source,
                "profile-2",
                "sample-2",
                tau=0.65,
                overwrite=True,
                output_audio=output,
            )

            self.assertEqual(manifest["output_audio"], str(output))
            self.assertEqual(engine.calls, [(source, reference, output, 0.65, True)])
            self.assertTrue(client.calls[0][3])

    def test_pitch_and_brightness_are_applied_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            reference = root / "reference.wav"
            source.write_bytes(b"source")
            reference.write_bytes(b"reference")
            processor = FakeAudioProcessor()
            service = ConversionService(
                root / "data",
                FakeVoiceBoxClient(reference),
                FakeEngine(),
                audio_processor=processor,
            )

            manifest = service.convert(
                source,
                "profile",
                "sample",
                pitch_semitones=-2,
                brightness_db=-3.5,
            )

            self.assertEqual(manifest["pitch_semitones"], -2.0)
            self.assertEqual(manifest["brightness_db"], -3.5)
            self.assertTrue(manifest["result"]["post_processing"]["applied"])
            self.assertEqual(manifest["result"]["audio"]["frames"], 27_562)
            self.assertEqual(len(processor.calls), 1)
            self.assertEqual(processor.calls[0][2:], (-2.0, -3.5, True))

    def test_failure_is_persisted_and_original_exception_is_reraised(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            reference = root / "reference.wav"
            source.write_bytes(b"source remains")
            reference.write_bytes(b"reference remains")
            failure = RuntimeError("CUDA ran out of memory")
            service = ConversionService(root / "data", FakeVoiceBoxClient(reference), FakeEngine(failure=failure))

            with self.assertRaises(RuntimeError) as raised:
                service.convert(source, "profile-3", "sample-3")

            self.assertIs(raised.exception, failure)
            manifests = list((root / "data" / "jobs").glob("*.json"))
            self.assertEqual(len(manifests), 1)
            persisted = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "failed")
            self.assertEqual(persisted["error"], {"type": "RuntimeError", "message": "CUDA ran out of memory"})
            self.assertIn("failed_at", persisted)
            self.assertEqual(source.read_bytes(), b"source remains")
            self.assertEqual(reference.read_bytes(), b"reference remains")

    def test_engine_calls_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            reference = root / "reference.wav"
            source.write_bytes(b"source")
            reference.write_bytes(b"reference")
            engine = FakeEngine(delay=0.05)
            service = ConversionService(root / "data", FakeVoiceBoxClient(reference), engine)

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(service.convert, source, "profile", f"sample-{index}")
                    for index in range(2)
                ]
                manifests = [future.result() for future in futures]

            self.assertEqual(engine.max_active, 1)
            self.assertEqual({item["status"] for item in manifests}, {"completed"})
            self.assertEqual(len({item["job_id"] for item in manifests}), 2)

    def test_non_finite_tau_still_produces_valid_failure_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            reference = root / "reference.wav"
            source.write_bytes(b"source")
            reference.write_bytes(b"reference")
            service = ConversionService(
                root / "data",
                FakeVoiceBoxClient(reference),
                FakeEngine(failure=ValueError("tau must be finite")),
            )

            with self.assertRaises(ValueError):
                service.convert(source, "profile", "sample", tau=float("nan"))

            manifest_path = next((root / "data" / "jobs").glob("*.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["tau"], "nan")


if __name__ == "__main__":
    unittest.main()
