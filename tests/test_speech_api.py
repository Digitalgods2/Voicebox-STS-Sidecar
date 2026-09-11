from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
import time
from uuid import uuid4
import wave

import pytest
from fastapi.testclient import TestClient

from voicebox_sts_bridge import api
from voicebox_sts_bridge.audio_jobs import AudioJobs
from voicebox_sts_bridge.settings import Settings


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    profile, sample = str(uuid4()), str(uuid4())
    pcm = BytesIO()
    with wave.open(pcm, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x01" * 1600)
    payload = pcm.getvalue()
    monkeypatch.setattr(api.VoiceBoxClient, "profiles", lambda self: [{"id": profile, "name": "Test voice"}])
    monkeypatch.setattr(api.VoiceBoxClient, "samples", lambda self, id: [{"id": sample}] if id == profile else [])
    monkeypatch.setattr(api.VoiceBoxClient, "fetch_reference", lambda *args, **kw: {"wav_path": "reference.wav"})

    def convert(self, source, reference, destination, **options):
        if source.read_bytes() == b"bad audio":
            raise ValueError("Audio decode failed")
        destination.write_bytes(payload)
        return {"ok": True, "audio": {"duration_seconds": 0.1}, "output_path": destination}

    monkeypatch.setattr(api.OpenVoiceEngine, "convert", convert)
    with TestClient(api.create_app(Settings(data_dir=tmp_path))) as client:
        def request(path, body=None, method=None):
            method = method or ("POST" if body is not None else "GET")
            if isinstance(body, dict):
                response = client.request(method, path, json=body)
            else:
                response = client.request(method, path, content=body)
            data = response.json() if "application/json" in response.headers.get("Content-Type", "") else response.content
            return response.status_code, response.headers, data
        yield request, profile, sample, payload


def wait_job(request, url):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, _, job = request(url)
        assert status == 200
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.02)
    pytest.fail("Job did not finish")


def test_upload_convert_poll_download_and_list(endpoint):
    request, profile, sample, payload = endpoint
    assert request("/api/voices")[2][0]["id"] == profile
    assert request(f"/api/voices/{profile}/samples")[2] == [{"id": sample}]
    code, headers, job = request(f"/api/speech?profile_id={profile}&filename=test.wav&authorized=true", payload)
    assert code == 202 and headers["Location"] == job["status_url"]
    assert job["status"] == "queued" and job["sample_id"] == sample
    done = wait_job(request, job["status_url"])
    assert done["status"] == "completed"
    code, headers, audio = request(done["download_url"])
    assert code == 200 and "attachment" in headers["Content-Disposition"]
    with wave.open(BytesIO(audio)) as wav:
        assert wav.getnframes() == 1600
    assert request("/api/audio/jobs?limit=1")[2]["items"][0]["job_id"] == job["job_id"]
    assert request("/api/audio/jobs?offset=1")[2]["items"] == []


def test_reusable_upload_and_failed_job(endpoint):
    request, profile, sample, _ = endpoint
    upload = request("/api/inputs?filename=bad.wav", b"bad audio")[2]
    code, _, job = request("/api/audio/jobs", {"input_id": upload["input_id"], "profile_id": profile,
                                             "sample_id": sample, "authorized": True})
    assert code == 202
    done = wait_job(request, job["status_url"])
    assert done["status"] == "failed" and "decode" in done["error"]["message"]
    assert done["download_url"] is None
    assert request(job["status_url"] + "/audio")[0] == 409


def test_invalid_requests(endpoint):
    request, profile, _, payload = endpoint
    prefix = f"/api/speech?profile_id={profile}&filename=a.wav"
    assert request(prefix, payload)[0] == 422
    assert request(prefix + "&authorized=true&pitch_semitones=999", payload)[0] == 422
    assert request(prefix + f"&authorized=true&sample_id={uuid4()}", payload)[0] == 422
    assert request(prefix + "&authorized=true", b"")[0] == 422
    assert request(f"/api/audio/jobs/{uuid4()}")[0] == 404
    assert request("/api/audio/jobs/not-a-uuid")[0] == 422
    assert request("/api/audio/jobs", {"input_id": str(uuid4()), "profile_id": profile, "authorized": True})[0] == 404
    assert "/api/speech" in request("/openapi.json")[2]["paths"]


def test_restart_marks_abandoned_jobs_failed(tmp_path):
    jobs = AudioJobs(tmp_path, None)
    queued = jobs.create({"input_id": str(uuid4())})
    restarted = AudioJobs(tmp_path, None)
    restarted.recover_interrupted()
    assert restarted.get(queued["job_id"])["error"]["type"] == "Interrupted"


def test_reference_selection_requires_unambiguous_voice(endpoint, monkeypatch):
    request, profile, sample, payload = endpoint
    prefix = f"/api/speech?profile_id={profile}&filename=a.wav&authorized=true"
    monkeypatch.setattr(api.VoiceBoxClient, "samples", lambda *args: [])
    assert "no reference" in request(prefix, payload)[2]["detail"]
    monkeypatch.setattr(api.VoiceBoxClient, "samples", lambda *args: [{"id": sample}, {"id": str(uuid4())}])
    assert "multiple samples" in request(prefix, payload)[2]["detail"]
    assert request(prefix + f"&sample_id={sample}", payload)[0] == 202


def test_queued_output_is_unavailable(endpoint, monkeypatch):
    request, profile, _, payload = endpoint
    monkeypatch.setattr(AudioJobs, "run", lambda *args: None)
    job = request(f"/api/speech?profile_id={profile}&filename=a.wav&authorized=true", payload)[2]
    assert request(job["status_url"])[2]["status"] == "queued"
    assert request(job["status_url"] + "/audio")[0] == 409


def test_status_polling_during_manifest_updates(tmp_path):
    jobs = AudioJobs(tmp_path, None)
    job = jobs.create({"input_id": str(uuid4())})

    def write():
        for index in range(50):
            jobs._save(dict(job, revision=index))

    def read():
        for _ in range(100):
            assert jobs.get(job["job_id"])["job_id"] == job["job_id"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(write), pool.submit(read), pool.submit(read)]
        for future in futures:
            future.result()
