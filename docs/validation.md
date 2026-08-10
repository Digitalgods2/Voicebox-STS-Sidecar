# Privacy-safe validation record

Last updated: 2026-08-10

This repository records functional outcomes without publishing host paths, Windows usernames, hardware fingerprints, cloned-profile names, source-media names, job identifiers, or raw benchmark manifests. Detailed performance measurements remain local runtime data.

## Engine and model integrity

- OpenVoice source and model revisions are pinned in `config/openvoice-v2.provenance.json`.
- The converter checkpoint is loaded with `weights_only=True` and checked for missing or unexpected state keys.
- Model loading, CUDA synchronization, and peak-memory collection complete through the isolated worker.
- Runtime inference uses local files and does not require a cloud API.

## Audio conversion

- A representative authorized recording completed the full local-audio conversion path.
- The published WAV passed FFprobe inspection and a complete FFmpeg error-on-decode pass.
- One model load can process multiple chunks while durable progress state is updated between chunks.
- Pitch and tone processing preserve the intended sample rate and exact frame count.

## Timeline reconstruction and video remux

- Multi-chunk reconstruction produced exactly the extracted source-audio frame count.
- Chunk overlaps are trimmed before placement, preventing cumulative timeline drift.
- The original video stream is copied without re-encoding.
- Converted audio is stored as lossless FLAC in Matroska.
- Completed output passes full audio/video decode validation before a job is marked successful.

## Local-only quality review

Subjective voice-identity, intelligibility, pacing, emphasis, and emotional-performance observations are intentionally kept outside source control because they can reveal local voice-profile and source-media details.
