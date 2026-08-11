# VoiceBox STS Bridge - Project Memory

## Objective

Build a fully local, offline speech-to-speech companion application for Jamie Pine's VoiceBox. The application should convert an existing speech or video recording into a selected VoiceBox voice profile while preserving the source timing, pacing, emphasis, and emotional performance as closely as the selected conversion engine permits.

This is intended primarily for long local and YouTube video conversions. There must be no cloud inference, paid API, subscription, or per-minute service dependency. A one-time local model download is acceptable only after the user approves its size and license.

## User constraints

- Run inference entirely on the user's computer.
- Use existing VoiceBox profiles as target voices.
- Do not modify the installed VoiceBox application or the Applio project for the initial prototype.
- Keep all services bound to loopback unless the user explicitly requests otherwise.
- Optimize eventually for long files, resumable jobs, and batch processing.
- Use only voice profiles and source media the user owns or has permission to use.
- Never download a multi-gigabyte model without first telling the user its approximate size and receiving approval.

## Runtime assumptions

- Target operating system: Windows.
- A CUDA-capable NVIDIA GPU is recommended; actual host hardware is intentionally not recorded in the repository.
- The launcher derives the default VoiceBox executable path from `%ProgramFiles%` and permits local configuration.
- VoiceBox is accessed only through its loopback API at `http://127.0.0.1:17493`; host-specific VoiceBox data paths are not tracked.
- Available VoiceBox models and profiles are queried dynamically and are not recorded in source control.
- Keep this add-on in its own repository rather than placing it inside VoiceBox or another voice project.

## VoiceBox architecture already researched

Repository: https://github.com/jamiepine/voicebox

VoiceBox is a Tauri/React desktop app backed by a local FastAPI server. It exposes profile and audio endpoints on port 17493.

Useful endpoints:

- `GET /health`
- `GET /profiles`
- `GET /profiles/{profile_id}`
- `GET /profiles/{profile_id}/samples`
- `GET /samples/{sample_id}`
- `GET /profiles/{profile_id}/export`

A cloned VoiceBox profile is not an Applio/RVC `.pth` model. It consists of database metadata plus one or more reference WAV files and their transcripts. The shared Qwen, Chatterbox, LuxTTS, or TADA weights are engine-level models. Exported profiles use a `.voicebox.zip` containing `manifest.json`, `samples.json`, and `samples/*.wav`.

Existing VoiceBox feature requests describe this exact goal:

- https://github.com/jamiepine/voicebox/issues/347
- https://github.com/jamiepine/voicebox/issues/407

The VoiceBox maintainers currently classify voice-to-voice/RVC support as a high-effort new modality with a different architecture from `TTSBackend`.

## Recommended architecture

Start with a separate local companion service/UI rather than forking or patching VoiceBox:

1. Accept a source audio or video file.
2. Query VoiceBox for available profiles.
3. Retrieve the selected cloned profile's reference WAV through the local API.
4. Run a local zero-shot speech-to-speech engine using the source audio and target reference WAV.
5. Save a verified WAV result locally.
6. Later add FFmpeg video extraction/remuxing, VAD-based segmentation, crossfades, exact timeline reconstruction, resumable job manifests, batch queues, and optional separation/recombination of background music and effects.

Do not use Whisper -> text -> TTS for the primary conversion path because that loses timing, delivery, emotion, and synchronization. Whisper can still be useful for diagnostics, captions, or segment metadata.

For preset or designed VoiceBox profiles that do not contain reference samples, generate and cache a neutral reference utterance through the existing VoiceBox TTS API before STS conversion.

## Candidate STS engines

### Seed-VC

- Repository: https://github.com/Plachtaa/seed-vc
- True zero-shot voice conversion using roughly 1-30 seconds of target reference audio.
- Supports offline, singing, and real-time variants.
- Strong conceptual match for VoiceBox reference-WAV profiles.
- GPL-3.0 license, while VoiceBox is MIT. Keep the initial experiment private/local and treat bundling or distribution as a deliberate licensing decision.
- Target hardware should be evaluated first with a short offline conversion. Real-time performance is not guaranteed.

### OpenVoice V2

- Repository: https://github.com/myshell-ai/OpenVoice
- MIT licensed and easier to distribute.
- Supports tone-color conversion from source audio to a reference voice.
- Likely easier to package but may provide weaker identity/prosody quality than Seed-VC. Benchmark both before committing to an engine.

### RVC

- Good speech/voice conversion when a trained target `.pth`/`.index` exists.
- Not directly compatible with VoiceBox profiles because VoiceBox profiles contain reference samples rather than per-speaker trained RVC weights.
- Do not choose RVC for the profile-compatible MVP.

## First milestone

Build a narrow offline proof of concept before any polished UI:

1. Scaffold a local backend and minimal UI/CLI in this repository.
2. Confirm VoiceBox health and list its profiles through the REST API.
3. Select a cloned profile and download one reference sample through the API.
4. Perform a dependency and license audit of the selected STS engine before installing it.
5. Ask permission before downloading model weights.
6. Convert a user-authorized 30-60 second test clip.
7. Verify the output file is valid, audible, durationally sensible, and uses the selected target voice.
8. Record conversion speed, peak VRAM, and quality observations locally without committing host-identifying measurements.

Only after the proof of concept succeeds should the project add long-file chunking, job recovery, video remuxing, and batch processing.

## Development principles

- Prefer a small local FastAPI backend plus a simple web UI for the prototype; package as a desktop application only after the inference path works.
- Treat VoiceBox as an external local service and use its public REST API instead of reading or editing its SQLite database directly.
- Keep generated files and caches inside explicit project/data directories.
- Preserve clean intermediate WAV files for debugging and later remuxing.
- Add structured job manifests so long conversions can resume after interruption.
- Process speech segments independently and reconstruct them at original timestamps.
- Never claim conversion success until the completed audio file has been decoded or otherwise validated.
- Benchmark a short clip before processing hours of material.

## Current status

The initial proof of concept and its long-video extension are implemented and validated locally as of 2026-08-11.

- OpenVoice V2 is installed in an isolated Python 3.10/CUDA environment from pinned source. The worker enforces security-fixed PyTorch 2.13.0 or newer and verifies the converter checkpoint against tracked provenance before deserialization.
- A representative source was converted successfully on local CUDA hardware and fully decoded; host-specific measurements remain local.
- The FastAPI service can discover VoiceBox profiles, cache reference WAV files, accept browser uploads, convert local audio, and serve source/reference/output media with byte-range support.
- The web UI automatically selects a sole reference sample, provides native file pickers, and includes persistent source, reference, output-audio, and output-video players. Its production-console design color-codes all seven stages and displays live service, dependency, cache, and video-pipeline states without inventing metrics.
- The UI provides per-profile pitch correction and brightness/tone-depth controls. Pitch uses high-quality, formant-preserving Rubber Band processing; tone uses a high shelf; both paths enforce the original converted frame count before publishing output.
- The YouTube workflow validates authorized URLs, downloads one video locally, extracts full-quality PCM, converts aligned overlapping chunks with one model load, reconstructs the exact source frame count, copies the original video stream, writes lossless FLAC audio to MKV, and fully decodes the result before reporting success.
- Local videos can be imported through a native browser file picker into a separate, UUID-addressed, 12 GiB-capped store. They are previewable in the UI and join the same exact-timing chunk conversion, lossless remux, and full-validation pipeline without invoking yt-dlp or changing the YouTube cache.
- YouTube downloads use a validated single-entry source cache keyed by canonical video ID. Matching reruns make no yt-dlp/YouTube request; a different successfully validated download atomically replaces the prior cache without deleting completed job outputs.
- Durable manifests and progress files survive page refreshes. Automatic continuation after the bridge process itself exits remains future work.
- Runtime environments, upstream source, model weights, reference caches, source media, and generated outputs remain ignored local data and are not part of the repository.

See `README.md`, `docs/engine-audit.md`, and `docs/validation.md` for installation, operation, provenance, and privacy-safe validation results.
