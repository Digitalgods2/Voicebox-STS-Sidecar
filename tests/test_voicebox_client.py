from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
import wave

from voicebox_sts_bridge.voicebox_client import VoiceBoxClient, VoiceBoxError, inspect_wav


class FakeResponse:
    def __init__(self, payload: bytes, content_type: str) -> None:
        self.payload = payload
        self.headers = {"Content-Type": content_type}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]


def wav_bytes() -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 16_000)
    return buffer.getvalue()


class VoiceBoxClientTests(unittest.TestCase):
    def test_inspects_pcm_wave(self) -> None:
        details = inspect_wav(wav_bytes())
        self.assertEqual(details["sample_rate_hz"], 16_000)
        self.assertEqual(details["duration_seconds"], 1.0)

    def test_rejects_non_wave_payload(self) -> None:
        with self.assertRaisesRegex(VoiceBoxError, "WAVE"):
            inspect_wav(b"not audio")

    def test_fetches_selected_reference_atomically_with_metadata(self) -> None:
        profile_id, sample_id = str(uuid4()), str(uuid4())
        sample_list = json.dumps([{"id": sample_id, "profile_id": profile_id, "reference_text": "Authorized sample"}]).encode()
        responses = iter([
            FakeResponse(sample_list, "application/json"),
            FakeResponse(wav_bytes(), "audio/wav"),
        ])
        requested_urls: list[str] = []

        def opener(request: object, **_: object) -> FakeResponse:
            requested_urls.append(request.full_url)  # type: ignore[attr-defined]
            return next(responses)

        client = VoiceBoxClient("http://127.0.0.1:17493", opener=opener)
        with tempfile.TemporaryDirectory() as directory:
            result = client.fetch_reference(profile_id, sample_id, Path(directory))
            self.assertTrue(Path(result["wav_path"]).is_file())
            self.assertTrue(Path(result["metadata_path"]).is_file())
            self.assertFalse(result["cached"])
            self.assertEqual(result["audio"]["duration_seconds"], 1.0)
        self.assertEqual(requested_urls[-1], f"http://127.0.0.1:17493/samples/{sample_id}")

    def test_rejects_sample_from_another_profile(self) -> None:
        profile_id = str(uuid4())
        response = FakeResponse(b"[]", "application/json")
        client = VoiceBoxClient("http://127.0.0.1:17493", opener=lambda *_args, **_kwargs: response)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VoiceBoxError, "does not belong"):
                client.fetch_reference(profile_id, str(uuid4()), Path(directory))

    def test_incomplete_cache_is_replaced(self) -> None:
        profile_id, sample_id = str(uuid4()), str(uuid4())
        sample_list = json.dumps([{"id": sample_id, "profile_id": profile_id}]).encode()
        responses = iter([
            FakeResponse(sample_list, "application/json"),
            FakeResponse(wav_bytes(), "audio/wav"),
        ])
        client = VoiceBoxClient("http://127.0.0.1:17493", opener=lambda *_args, **_kwargs: next(responses))
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "references" / profile_id / f"{sample_id}.wav"
            wav_path.parent.mkdir(parents=True)
            wav_path.write_bytes(wav_bytes())
            result = client.fetch_reference(profile_id, sample_id, Path(directory))
            self.assertFalse(result["cached"])
            self.assertTrue(Path(result["metadata_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
