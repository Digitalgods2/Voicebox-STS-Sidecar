from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

from .audio_effects import AudioEffectsError
from .conversion_service import ConversionService
from .openvoice_engine import OpenVoiceEngine, OpenVoiceError
from .settings import Settings
from .voicebox_client import VoiceBoxClient, VoiceBoxError


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local companion bridge for VoiceBox STS workflows")
    parser.add_argument("--voicebox-url", help="Loopback VoiceBox API URL")
    parser.add_argument("--data-dir", type=Path, help="Project-local runtime data directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="Check the local VoiceBox backend")
    commands.add_parser("profiles", help="List VoiceBox profiles")
    commands.add_parser("engine-status", help="Check the isolated OpenVoice installation without loading its model")
    commands.add_parser("engine-probe", help="Load the OpenVoice model on CUDA and report readiness")
    samples = commands.add_parser("samples", help="List reference samples for a profile")
    samples.add_argument("profile_id")
    fetch = commands.add_parser("fetch-reference", help="Cache one explicitly selected reference WAV")
    fetch.add_argument("profile_id")
    fetch.add_argument("sample_id")
    fetch.add_argument("--overwrite", action="store_true")
    convert = commands.add_parser("convert", help="Convert one source recording to a selected VoiceBox reference")
    convert.add_argument("source_audio", type=Path)
    convert.add_argument("profile_id")
    convert.add_argument("sample_id")
    convert.add_argument("--output", type=Path)
    convert.add_argument("--tau", type=float, default=0.3)
    convert.add_argument(
        "--pitch-semitones",
        type=float,
        default=0.0,
        help="Duration-preserving pitch correction from -6 to +6 semitones",
    )
    convert.add_argument(
        "--brightness-db",
        type=float,
        default=0.0,
        help="Tone-depth/high-shelf adjustment from -6 dB (deeper) to +6 dB (brighter)",
    )
    convert.add_argument("--overwrite", action="store_true")
    serve = commands.add_parser("serve", help="Run the loopback-only web UI")
    serve.add_argument("--host", default=None, help="Loopback bind address")
    serve.add_argument("--port", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        overrides: dict[str, Any] = {}
        if args.voicebox_url:
            overrides["voicebox_base_url"] = args.voicebox_url
        if args.data_dir:
            overrides["data_dir"] = args.data_dir
        if args.command == "serve":
            if args.host:
                overrides["bridge_host"] = args.host
            if args.port:
                overrides["bridge_port"] = args.port
        settings = replace(settings, **overrides)

        if args.command == "serve":
            try:
                import uvicorn
                from .api import create_app
            except ImportError as exc:
                raise RuntimeError('Install bridge dependencies with: python -m pip install -e ".[dev]"') from exc
            uvicorn.run(
                create_app(settings),
                host=settings.bridge_host,
                port=settings.bridge_port,
                reload=False,
            )
            return 0

        if args.command in {"engine-status", "engine-probe"}:
            engine = OpenVoiceEngine()
            _print(engine.status() if args.command == "engine-status" else engine.probe())
            return 0

        client = VoiceBoxClient(
            settings.voicebox_base_url,
            timeout_seconds=settings.request_timeout_seconds,
            max_reference_bytes=settings.max_reference_bytes,
        )
        if args.command == "health":
            _print(client.health())
        elif args.command == "profiles":
            _print(client.profiles())
        elif args.command == "samples":
            _print(client.samples(args.profile_id))
        elif args.command == "fetch-reference":
            _print(client.fetch_reference(args.profile_id, args.sample_id, settings.data_dir, overwrite=args.overwrite))
        elif args.command == "convert":
            conversions = ConversionService(settings.data_dir, client, OpenVoiceEngine())
            _print(
                conversions.convert(
                    args.source_audio,
                    args.profile_id,
                    args.sample_id,
                    tau=args.tau,
                    pitch_semitones=args.pitch_semitones,
                    brightness_db=args.brightness_db,
                    overwrite=args.overwrite,
                    output_audio=args.output,
                )
            )
        return 0
    except (ValueError, VoiceBoxError, OpenVoiceError, AudioEffectsError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
