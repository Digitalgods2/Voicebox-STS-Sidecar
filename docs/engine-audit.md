# STS engine audit

Audit date: 2026-08-10

Status: OpenVoice V2 was approved, installed, and validated on 2026-08-09. The pinned source, isolated CUDA runtime, and hash-verified converter model are local. Real single-file, one-load batch, and FFmpeg reconstruction/remux tests have completed successfully.

## Recommendation

Use **OpenVoice V2 as the sidecar's current speech-to-speech engine**. Consider another engine only if formal listening tests show that OpenVoice does not meet the desired voice-identity or performance-preservation threshold.

This choice differs from the original research note because Seed-VC's upstream repository was archived on 2025-11-21. OpenVoice V2 is a much smaller, permissively licensed engine and completed the measured workloads on local CUDA hardware. Objective timing and file-integrity tests have passed; target identity, intelligibility, emphasis, and emotional similarity still require subjective listening across representative source material.

## OpenVoice V2

- Upstream source and model license: MIT.
- Repository: <https://github.com/myshell-ai/OpenVoice>
- Official model: <https://huggingface.co/myshell-ai/OpenVoiceV2>
- Official converter checkpoint: 131,320,490 bytes (about 131 MB decimal), SHA-256 `9652c27e92b6b2a91632590ac9962ef7ae2b712e5c5b7f4c34ec55ee2b37ab9e`.
- The converter API accepts source audio plus source and target speaker embeddings. The bridge can therefore use the original performance directly instead of a transcription-to-TTS path.
- The official Linux instructions use Python 3.9. This project should use an isolated Python 3.10 prefix on Windows; the installed Python 3.13 environment is not a suitable place for the engine.
- The upstream dependency list is old and includes unrelated packages such as the `openai` client, Gradio, Faster Whisper, and Whisper Timestamped. The direct converter prototype does not require a cloud API, MeloTTS, transcription, or a web demo, so the upstream requirements file must not be installed wholesale.
- The application host and ML runtime remain separate. The isolated engine prefix uses CUDA-enabled PyTorch `2.4.1+cu124`; no ML packages were added to the main bridge environment.
- Main uncertainty: OpenVoice is primarily documented as tone-color conversion in a TTS pipeline. Its lower-level converter can process arbitrary source audio, but target similarity and retention of fine emotional detail must be measured rather than assumed.

## Seed-VC

- Upstream source/model license: GPL-3.0.
- Repository: <https://github.com/Plachtaa/seed-vc>
- The owner archived the repository on 2025-11-21; it is read-only.
- Upstream recommends Python 3.10 on Windows.
- The current requirements file is not reproducible as written: it requests nightly CUDA 12.6 PyTorch packages and also pins PyTorch/TorchVision/TorchAudio 2.4.0 in the same file.
- V2 selectively downloads approximately 2.61 GB of model weights before package dependencies and caches:
  - CFM and AR checkpoints: 712 MB total;
  - two ASTRAL light checkpoints: 156.4 MB total;
  - HuBERT Large PyTorch checkpoint: 1.26 GB;
  - BigVGAN generator: 449 MB;
  - CAMPPlus speaker encoder: 28 MB.
- The model files are PyTorch pickle checkpoints loaded through `torch.load`. If tested later, upstream commits and file hashes should be pinned and verified.
- The V2 architecture is the stronger conceptual quality candidate because it is designed for voice/accent conversion and source-speaker suppression, but compatibility and peak VRAM must be established on each target system.
- Do not combine or distribute Seed-VC code with this bridge without a deliberate GPL compatibility decision. A private, separate local process remains the least coupled evaluation arrangement, but this is an engineering note rather than legal advice.

## Approved-install design

The approved OpenVoice V2 installation follows this design:

1. Conda Python 3.10.20 is isolated under `.envs/openvoice-v2`; the base environment was not mutated.
2. CUDA PyTorch 2.4.1+cu124 is pinned and detects a CUDA-capable NVIDIA GPU.
3. OpenVoice source revision `74a1d147b17a8c3092dd5430504bd83ef6c7eb23` is kept under ignored `third_party/OpenVoice`.
4. Only direct-converter dependencies are installed; no cloud SDK, TTS engine, Gradio, or transcription stack is present.
5. Official model revision `f36e7edfe1684461a8343844af60babc2efbb727` is under `data/models/openvoice-v2`; its checkpoint hash matches the audited SHA-256.
6. Keep runtime networking disabled except for the explicit installation/download phase.
7. Real model loading, single conversion, one-load batch conversion, exact-frame reconstruction, lossless remuxing, and full FFmpeg decode validation have passed. The remaining quality gate is a structured listening scorecard across several authorized 30-60 second sources and target profiles.

## Pitch and tone post-processing audit

Profile-to-profile measurements showed variable median pitch offsets and a separate increase in spectral brightness, so a hard-coded global correction was rejected. Version 0.2 adds two bounded, user-controlled post-conversion adjustments instead:

- pitch: -6 to +6 semitones through FFmpeg's Rubber Band filter with tempo fixed at 1.0, formant preservation enabled, and the high-quality pitch mode;
- brightness/tone depth: -6 to +6 dB through a 2.5 kHz high shelf;
- peak safety: a unity-gain limiter that acts only when the adjusted signal would exceed the configured ceiling;
- timing safety: pad/trim to the input frame count, followed by full PCM inspection and exact sample-rate/frame-count comparison before atomic replacement.

The filters run after OpenVoice and, for long video, after exact-frame chunk reconstruction. This avoids repeating nonlinear DSP at overlap boundaries. Neutral settings bypass FFmpeg post-processing entirely. No new model, Python package, cloud dependency, or download was added.

The configured external FFmpeg 7.1 build reports `rubberband`, `highshelf`, and `alimiter` support and was compiled with `librubberband`. FFmpeg/Rubber Band licensing therefore remains a property of the user's installed binary; this repository does not distribute either dependency.

## Primary sources

- Seed-VC README and archive notice: <https://github.com/Plachtaa/seed-vc>
- Seed-VC requirements: <https://github.com/Plachtaa/seed-vc/blob/main/requirements.txt>
- Seed-VC V2 configuration: <https://github.com/Plachtaa/seed-vc/blob/main/configs/v2/vc_wrapper.yaml>
- Seed-VC model files: <https://huggingface.co/Plachta/Seed-VC/tree/main/v2>
- ASTRAL model files: <https://huggingface.co/Plachta/ASTRAL-quantization/tree/main>
- HuBERT Large model files: <https://huggingface.co/facebook/hubert-large-ll60k/tree/main>
- BigVGAN V2 model files: <https://huggingface.co/nvidia/bigvgan_v2_22khz_80band_256x/tree/main>
- CAMPPlus model files: <https://huggingface.co/funasr/campplus/tree/main>
- OpenVoice repository, license, API, and usage: <https://github.com/myshell-ai/OpenVoice>
- Official OpenVoice V2 checkpoint: <https://huggingface.co/myshell-ai/OpenVoiceV2/tree/main/converter>
