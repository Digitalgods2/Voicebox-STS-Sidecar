from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
import wave

from voicebox_sts_bridge.openvoice_engine import OpenVoiceEngine, OpenVoiceError
from voicebox_sts_bridge import openvoice_worker


def write_wav(path: Path, *, seconds: float = 0.25, sample_rate: int = 16_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames)


def scaffold_project(root: Path) -> None:
    python = root / ".envs" / "openvoice-v2" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (root / "third_party" / "OpenVoice").mkdir(parents=True)
    converter = root / "data" / "models" / "openvoice-v2" / "converter"
    converter.mkdir(parents=True)
    (converter / "config.json").write_text("{}", encoding="utf-8")
    (converter / "checkpoint.pth").write_bytes(b"local checkpoint placeholder")


class OpenVoiceEngineTests(unittest.TestCase):
    def test_status_uses_isolated_python_without_loading_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            calls: list[list[str]] = []

            def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"ok": True, "operation": "status", "ready": True, "model_loaded": False}),
                    "",
                )

            result = OpenVoiceEngine(root, runner=runner).status()

            self.assertTrue(result["ready"])
            self.assertFalse(result["model_loaded"])
            self.assertEqual(Path(calls[0][0]), root / ".envs" / "openvoice-v2" / "python.exe")
            self.assertIn("status", calls[0])

    def test_worker_environment_does_not_inherit_bridge_python_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            captured_environment: dict[str, str] = {}

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                captured_environment.update(kwargs["env"])  # type: ignore[arg-type]
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"ok": True, "operation": "status", "ready": True, "model_loaded": False}),
                    "",
                )

            with patch.dict("os.environ", {"PYTHONPATH": "bridge-src", "PYTHONHOME": "base-python"}):
                OpenVoiceEngine(root, runner=runner).status()

            self.assertNotIn("PYTHONPATH", captured_environment)
            self.assertNotIn("PYTHONHOME", captured_environment)

    def test_status_reports_a_missing_environment_without_starting_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = Mock()
            result = OpenVoiceEngine(directory, runner=runner).status()
            self.assertFalse(result["ready"])
            self.assertFalse(result["checks"]["python"])
            runner.assert_not_called()

    def test_probe_requests_a_real_model_load_in_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            calls: list[list[str]] = []

            def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"ok": True, "operation": "probe", "ready": True, "model_loaded": True}),
                    "checkpoint diagnostics",
                )

            result = OpenVoiceEngine(root, runner=runner).probe(device="cpu")
            self.assertTrue(result["model_loaded"])
            self.assertIn("probe", calls[0])
            self.assertEqual(calls[0][calls[0].index("--device") + 1], "cpu")

    def test_convert_accepts_non_uuid_paths_and_validates_the_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            source = root / "input files" / "performance take 01.flac"
            target = root / "references" / "Jamie's approved voice.wav"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b"mock source audio")
            target.write_bytes(b"mock target audio")
            destination = root / "outputs" / "converted take.wav"
            calls: list[list[str]] = []

            def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                worker_output = Path(command[command.index("--output") + 1])
                write_wav(worker_output)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"ok": True, "operation": "convert", "output_path": str(worker_output)}),
                    "",
                )

            result = OpenVoiceEngine(root, runner=runner).convert(source, target, destination, device="cpu")

            self.assertTrue(destination.is_file())
            self.assertEqual(result["output_path"], str(destination.resolve()))
            self.assertEqual(result["audio"]["duration_seconds"], 0.25)
            self.assertGreater(result["audio"]["size_bytes"], 44)
            command = calls[0]
            self.assertEqual(command[command.index("--source") + 1], str(source.resolve()))
            self.assertEqual(command[command.index("--target-reference") + 1], str(target.resolve()))
            self.assertFalse(command[command.index("--output") + 1] == str(destination.resolve()))

    def test_convert_batch_uses_one_worker_invocation_and_publishes_all_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            source_a = root / "chunks" / "a.wav"
            source_b = root / "chunks" / "b.wav"
            target = root / "reference.wav"
            write_wav(source_a)
            write_wav(source_b)
            write_wav(target)
            output_a = root / "converted" / "a.wav"
            output_b = root / "converted" / "b.wav"
            progress = root / "progress.json"
            calls: list[list[str]] = []

            def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                batch_path = Path(command[command.index("--batch-file") + 1])
                batch = json.loads(batch_path.read_text(encoding="utf-8"))
                self.assertEqual(batch["target_reference"], str(target.resolve()))
                self.assertEqual(batch["progress_path"], str(progress.resolve()))
                for item in batch["items"]:
                    write_wav(Path(item["output"]))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"ok": True, "operation": "convert-batch"}),
                    "",
                )

            result = OpenVoiceEngine(root, runner=runner).convert_batch(
                [(source_a, output_a), (source_b, output_b)],
                target,
                device="cpu",
                progress_path=progress,
            )

            self.assertEqual(len(calls), 1)
            self.assertIn("convert-batch", calls[0])
            self.assertEqual(result["chunks_completed"], 2)
            self.assertTrue(output_a.is_file())
            self.assertTrue(output_b.is_file())
            self.assertEqual([item["output_path"] for item in result["items"]], [str(output_a.resolve()), str(output_b.resolve())])
            self.assertEqual(list((root / "data").glob(".openvoice-batch.*.json")), [])

    def test_convert_refuses_source_as_output_and_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            source = root / "source.wav"
            target = root / "target.wav"
            write_wav(source)
            write_wav(target)
            engine = OpenVoiceEngine(root, runner=Mock())

            with self.assertRaisesRegex(ValueError, "different from source"):
                engine.convert(source, target, source)
            existing = root / "existing.wav"
            write_wav(existing)
            with self.assertRaises(FileExistsError):
                engine.convert(source, target, existing)

    def test_convert_rejects_a_truncated_wave_after_reading_its_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            source = root / "source.wav"
            target = root / "target.wav"
            destination = root / "result.wav"
            write_wav(source)
            write_wav(target)

            def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                worker_output = Path(command[command.index("--output") + 1])
                write_wav(worker_output)
                worker_output.write_bytes(worker_output.read_bytes()[:-2])
                payload = {"ok": True, "operation": "convert", "output_path": str(worker_output)}
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

            with self.assertRaisesRegex(OpenVoiceError, "truncated"):
                OpenVoiceEngine(root, runner=runner).convert(source, target, destination, device="cpu")
            self.assertFalse(destination.exists())

    def test_overwrite_is_atomic_and_timeout_removes_only_the_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            source = root / "source.wav"
            target = root / "target.wav"
            output = root / "result.wav"
            write_wav(source)
            write_wav(target)
            original = b"existing output must survive"
            output.write_bytes(original)

            def timeout_runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                raise subprocess.TimeoutExpired(command, 1)

            engine = OpenVoiceEngine(root, timeout_seconds=1, runner=timeout_runner)
            with self.assertRaisesRegex(OpenVoiceError, "timed out"):
                engine.convert(source, target, output, overwrite=True)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(root.glob(".result.*.wav")), [])

    def test_worker_errors_are_converted_to_openvoice_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)

            def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                payload = {"ok": False, "error": {"type": "RuntimeError", "message": "CUDA unavailable"}}
                return subprocess.CompletedProcess(command, 1, json.dumps(payload), "diagnostics")

            with self.assertRaisesRegex(OpenVoiceError, "CUDA unavailable"):
                OpenVoiceEngine(root, runner=runner).probe()


class OpenVoiceWorkerTests(unittest.TestCase):
    def test_invalid_worker_arguments_still_emit_json(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = openvoice_worker.main(["convert"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(return_code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("arguments", payload["error"]["message"])
        self.assertEqual(stderr.getvalue(), "")

    def test_batch_worker_loads_converter_and_target_embedding_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            source_a = root / "a.wav"
            source_b = root / "b.wav"
            target = root / "target.wav"
            output_a = root / "out-a.wav"
            output_b = root / "out-b.wav"
            progress = root / "progress.json"
            batch = root / "batch.json"
            for path in (source_a, source_b, target):
                write_wav(path)
            batch.write_text(
                json.dumps(
                    {
                        "target_reference": str(target),
                        "progress_path": str(progress),
                        "items": [
                            {"source": str(source_a), "output": str(output_a)},
                            {"source": str(source_b), "output": str(output_b)},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            class FakeConverter:
                def __init__(self) -> None:
                    self.extracted: list[str] = []

                def extract_se(self, path: str) -> str:
                    self.extracted.append(path)
                    return f"embedding:{path}"

                def convert(self, *, audio_src_path, src_se, tgt_se, output_path, tau) -> None:
                    self.assertions.append((audio_src_path, src_se, tgt_se, tau))
                    write_wav(Path(output_path))

                assertions: list[tuple[object, ...]] = []

            converter = FakeConverter()
            with patch.object(openvoice_worker, "_reset_cuda_metrics", return_value=None), patch.object(
                openvoice_worker, "_load_converter", return_value=converter
            ) as load_converter:
                result = openvoice_worker.run_batch_conversion(root, batch, device="cpu", tau=0.3)

            load_converter.assert_called_once_with(root.resolve(), "cpu")
            self.assertEqual(
                converter.extracted,
                [str(target.resolve()), str(source_a.resolve()), str(source_b.resolve())],
            )
            self.assertEqual(result["chunks_completed"], 2)
            self.assertTrue(output_a.is_file())
            self.assertTrue(output_b.is_file())
            self.assertEqual(json.loads(progress.read_text(encoding="utf-8"))["status"], "completed")

    def test_status_only_checks_files_and_module_specs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            dependencies: list[str] = []

            def available(name: str) -> bool:
                dependencies.append(name)
                return True

            with patch.object(openvoice_worker, "_dependency_available", side_effect=available), patch.object(
                openvoice_worker, "_load_converter"
            ) as load_converter:
                result = openvoice_worker.status_payload(root)
            self.assertTrue(result["ready"])
            self.assertFalse(result["model_loaded"])
            self.assertEqual(
                dependencies,
                [
                    "torch",
                    "openvoice",
                    "numpy",
                    "librosa",
                    "soundfile",
                    "inflect",
                    "unidecode",
                    "eng_to_ipa",
                    "pypinyin",
                    "jieba",
                    "cn2an",
                ],
            )
            load_converter.assert_not_called()

    def test_worker_directly_extracts_both_embeddings_and_keeps_stdout_json_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            source = root / "source performance.wav"
            target = root / "target reference.wav"
            output = root / "result.wav"
            write_wav(source)
            write_wav(target)

            instances: list[object] = []

            class FakeModel:
                def __init__(self) -> None:
                    self.moves: list[str] = []
                    self.state_loads: list[tuple[dict[str, object], bool]] = []
                    self.eval_calls = 0

                def load_state_dict(self, state: dict[str, object], *, strict: bool) -> tuple[list[str], list[str]]:
                    self.state_loads.append((state, strict))
                    return [], []

                def to(self, device: str) -> "FakeModel":
                    self.moves.append(device)
                    return self

                def eval(self) -> "FakeModel":
                    self.eval_calls += 1
                    return self

            class FakeHparams:
                _version_ = "v2"

            class FakeBase:
                def __init__(self, config: str, *, device: str) -> None:
                    self.config = config
                    self.base_device = device
                    self.device = device
                    self.model = FakeModel()
                    self.hps = FakeHparams()
                    self.checkpoint: str | None = None
                    self.extracted: list[str] = []
                    self.convert_kwargs: dict[str, object] = {}
                    instances.append(self)
                    print("model constructor diagnostic")

            class FakeConverter:
                def load_ckpt(self, checkpoint: str) -> None:
                    raise AssertionError(f"unsafe upstream load_ckpt called for {checkpoint}")

                def extract_se(self, audio_path: str) -> str:
                    self.extracted.append(audio_path)
                    return f"embedding:{audio_path}"

                def convert(self, **kwargs: object) -> None:
                    self.convert_kwargs = kwargs
                    write_wav(Path(str(kwargs["output_path"])), seconds=0.5)

            package = ModuleType("openvoice")
            package.__path__ = []  # type: ignore[attr-defined]
            api_module = ModuleType("openvoice.api")
            api_module.OpenVoiceBaseClass = FakeBase  # type: ignore[attr-defined]
            api_module.ToneColorConverter = FakeConverter  # type: ignore[attr-defined]
            torch_module = ModuleType("torch")
            torch_loads: list[tuple[str, dict[str, object]]] = []

            def torch_load(path: str, **kwargs: object) -> dict[str, object]:
                torch_loads.append((path, kwargs))
                return {"model": {"converter.weight": "safe tensor placeholder"}}

            torch_module.load = torch_load  # type: ignore[attr-defined]
            stdout, stderr = StringIO(), StringIO()
            argv = [
                "convert",
                "--project-root",
                str(root),
                "--source",
                str(source),
                "--target-reference",
                str(target),
                "--output",
                str(output),
                "--device",
                "cpu",
            ]

            with patch.dict(
                sys.modules,
                {"torch": torch_module, "openvoice": package, "openvoice.api": api_module},
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = openvoice_worker.main(argv)

            payload = json.loads(stdout.getvalue())
            self.assertEqual(return_code, 0)
            self.assertTrue(payload["ok"])
            self.assertNotIn("diagnostic", stdout.getvalue())
            self.assertIn("model constructor diagnostic", stderr.getvalue())
            converter = instances[0]
            self.assertIsNone(converter.watermark_model)  # type: ignore[attr-defined]
            self.assertEqual(converter.base_device, "cpu")  # type: ignore[attr-defined]
            self.assertEqual(converter.version, "v2")  # type: ignore[attr-defined]
            self.assertEqual(
                torch_loads,
                [
                    (
                        str(root / "data" / "models" / "openvoice-v2" / "converter" / "checkpoint.pth"),
                        {"map_location": "cpu", "weights_only": True},
                    )
                ],
            )
            self.assertEqual(converter.model.state_loads, [({"converter.weight": "safe tensor placeholder"}, False)])  # type: ignore[attr-defined]
            self.assertEqual(converter.model.moves, ["cpu"])  # type: ignore[attr-defined]
            self.assertEqual(converter.model.eval_calls, 1)  # type: ignore[attr-defined]
            self.assertEqual(converter.extracted, [str(source.resolve()), str(target.resolve())])  # type: ignore[attr-defined]
            convert_kwargs = converter.convert_kwargs  # type: ignore[attr-defined]
            self.assertEqual(convert_kwargs["audio_src_path"], str(source.resolve()))
            self.assertEqual(convert_kwargs["src_se"], f"embedding:{source.resolve()}")
            self.assertEqual(convert_kwargs["tgt_se"], f"embedding:{target.resolve()}")
            self.assertNotIn("text", convert_kwargs)
            self.assertEqual(payload["audio"]["duration_seconds"], 0.5)
            self.assertIsNone(payload["cuda_device_name"])
            self.assertIsNone(payload["peak_memory_allocated_mb"])
            self.assertIsNone(payload["peak_memory_reserved_mb"])

    def test_checkpoint_state_mismatch_fails_before_device_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)

            class MismatchedModel:
                def __init__(self) -> None:
                    self.moves: list[str] = []

                def load_state_dict(self, _state: dict[str, object], *, strict: bool) -> tuple[list[str], list[str]]:
                    self.strict = strict
                    return ["missing.weight"], ["unexpected.weight"]

                def to(self, device: str) -> "MismatchedModel":
                    self.moves.append(device)
                    return self

            class FakeBase:
                def __init__(self, _config: str, *, device: str) -> None:
                    self.model = MismatchedModel()
                    self.hps = object()
                    self.device = device

            class FakeConverter:
                pass

            package = ModuleType("openvoice")
            package.__path__ = []  # type: ignore[attr-defined]
            api_module = ModuleType("openvoice.api")
            api_module.OpenVoiceBaseClass = FakeBase  # type: ignore[attr-defined]
            api_module.ToneColorConverter = FakeConverter  # type: ignore[attr-defined]
            torch_module = ModuleType("torch")
            torch_module.load = lambda *_args, **_kwargs: {"model": {"weight": object()}}  # type: ignore[attr-defined]

            with patch.dict(
                sys.modules,
                {"torch": torch_module, "openvoice": package, "openvoice.api": api_module},
            ):
                with self.assertRaisesRegex(RuntimeError, "missing keys.*unexpected keys"):
                    openvoice_worker._load_converter(root, "cuda:0")

    def test_probe_reports_cuda_name_and_peak_memory_after_synchronizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            events: list[str] = []

            class FakeCuda:
                def init(self) -> None:
                    events.append("init")

                def reset_peak_memory_stats(self, device: str) -> None:
                    events.append(f"reset:{device}")

                def synchronize(self, device: str) -> None:
                    events.append(f"synchronize:{device}")

                def get_device_name(self, device: str) -> str:
                    events.append(f"name:{device}")
                    return "Mock RTX"

                def max_memory_allocated(self, device: str) -> int:
                    events.append(f"allocated:{device}")
                    return 5 * 1024 * 1024

                def max_memory_reserved(self, device: str) -> int:
                    events.append(f"reserved:{device}")
                    return 7 * 1024 * 1024

            torch_module = ModuleType("torch")
            torch_module.cuda = FakeCuda()  # type: ignore[attr-defined]
            torch_module.device = lambda value: value  # type: ignore[attr-defined]

            def load_converter(_root: Path, device: str) -> object:
                events.append(f"load:{device}")
                return object()

            with patch.dict(sys.modules, {"torch": torch_module}), patch.object(
                openvoice_worker, "_load_converter", side_effect=load_converter
            ):
                result = openvoice_worker.probe_model(root, device="cuda:0")

            self.assertLess(events.index("reset:cuda:0"), events.index("load:cuda:0"))
            self.assertLess(events.index("load:cuda:0"), events.index("synchronize:cuda:0"))
            self.assertEqual(result["cuda_device_name"], "Mock RTX")
            self.assertEqual(result["peak_memory_allocated_mb"], 5.0)
            self.assertEqual(result["peak_memory_reserved_mb"], 7.0)

    def test_worker_wave_inspection_rejects_truncated_frame_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "truncated.wav"
            write_wav(audio_path)
            audio_path.write_bytes(audio_path.read_bytes()[:-2])
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                openvoice_worker._inspect_wav(audio_path)

    def test_worker_rejects_source_as_output_before_model_loading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scaffold_project(root)
            source = root / "source.wav"
            target = root / "target.wav"
            write_wav(source)
            write_wav(target)
            with patch.object(openvoice_worker, "_load_converter") as load_converter:
                with self.assertRaisesRegex(ValueError, "different from source"):
                    openvoice_worker.run_conversion(root, source, target, source, device="cpu")
            load_converter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
