# Speech API

Start VoiceBox and the bridge (`python -m voicebox_sts_bridge serve`). The API
is available at `http://127.0.0.1:8765`. Interactive documentation is at `/docs`,
with the machine-readable schema at `/openapi.json`.

## Upload, select a voice, download

| Method and path | Purpose |
| --- | --- |
| `GET /api/voices` | List VoiceBox profiles; use a profile's `id` as `profile_id` |
| `GET /api/voices/{profile_id}` | Retrieve profile details |
| `GET /api/voices/{profile_id}/samples` | List reference samples and their IDs |
| `POST /api/speech` | Upload raw speech bytes and start a background conversion |
| `GET /api/audio/jobs/{job_id}` | Read status, selected voice, options, and errors |
| `GET /api/audio/jobs/{job_id}/audio` | Download the completed WAV |
| `GET /api/audio/jobs?limit=50&offset=0` | List audio API jobs, newest first |
| `GET /api/engine/status` | Inspect local engine availability |
| `GET /api/voicebox/health` | Check VoiceBox connectivity |

`POST /api/speech` takes query parameters `filename`, `profile_id`, and
`authorized=true` (confirmation that you have permission to use both media and
voice). Send the file itself as the request body, **not multipart/form-data**.
Use `Content-Type: application/octet-stream`. Audio uploads are capped at 1 GiB;
accepted extensions are WAV, MP3, FLAC, M4A, AAC, OGG, OPUS, and WMA.
The conversion engine must be able to decode the contents.

Optional parameters:

- `sample_id`: automatically selected only when the profile has exactly one
  sample. Multiple samples require an explicit choice. Profiles without samples
  cannot currently be used by this API.
- `tau`: conversion parameter, 0–1, default 0.3.
- `pitch_semitones` and `brightness_db`: default 0; see `/docs` for allowed ranges.

The response is HTTP **202**, with `Location` pointing to `status_url` and
`Retry-After: 2`. Poll every two seconds until `status` is `completed` or `failed`.
`download_url` is null until completion. Failed jobs include an `error` object.
Downloading an unfinished or failed job returns **409**; unknown IDs return
**404**; invalid parameters return **422**. VoiceBox connection failures during
submission return **502**. Failures after acceptance appear in the job record.

## Runnable Python client

The example uses only Python's standard library and streams uploads/downloads:

```powershell
python examples/speech_client.py
python examples/speech_client.py --input speech.wav --voice PROFILE_UUID --authorized --output converted.wav
```

To send bytes directly with curl on Windows:

```powershell
curl.exe "http://127.0.0.1:8765/api/voices"
curl.exe -X POST "http://127.0.0.1:8765/api/speech?filename=speech.wav&profile_id=PROFILE_UUID&authorized=true" -H "Content-Type: application/octet-stream" --data-binary "@speech.wav"
```

## Reuse uploads and other workflows

Upload once with `POST /api/inputs?filename=speech.wav` (raw bytes), then submit
`POST /api/audio/jobs` with JSON:

```json
{"input_id":"UPLOAD_UUID","profile_id":"PROFILE_UUID","authorized":true,"pitch_semitones":0,"brightness_db":0}
```

Repeat with different voices using the same input ID. The existing synchronous
`POST /api/conversions` and UI endpoints remain available. Local-video uploads
(`/api/video-inputs`, `/api/video/jobs`) and YouTube jobs (`/api/youtube/jobs`)
are also described in `/docs`.

## Execution and storage

Inference stays local and uses the existing shared conversion lock. Run one
bridge server process, with one Uvicorn worker per data directory. Jobs and
outputs survive restart; queued/running audio API jobs become failed with an
`Interrupted` error on startup and must be submitted again. Submission is not
idempotent: retrying a POST creates another job. There is no cancellation or
automatic retry. Job history covers the new audio API, not older UI conversions.

Keep the unauthenticated API on loopback. Files and manifests are retained under
the configured data directory; this API does not automatically delete them.
Benchmark short speech before large inputs; the audio endpoint uses the existing
single-file engine path, while the video workflow provides chunked conversion.
