from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from voicebox_sts_bridge.media_store import MediaStore


async def chunks(*payloads: bytes):
    for payload in payloads:
        yield payload


class MediaStoreTests(unittest.TestCase):
    def test_traversal_filename_keeps_only_the_leaf_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MediaStore(root / "data")

            result = asyncio.run(
                store.store_input(r"..\..\outside\source.WAV", chunks(b"RIFFdata"), "audio/wav")
            )

            stored = Path(result["stored_path"])
            self.assertEqual(result["original_name"], "source.WAV")
            self.assertEqual(stored.suffix, ".wav")
            self.assertTrue(stored.is_relative_to((root / "data" / "inputs").resolve()))
            self.assertFalse((root / "outside" / "source.WAV").exists())

    def test_unsupported_extension_is_rejected_without_creating_input_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            store = MediaStore(data_dir)

            with self.assertRaisesRegex(ValueError, "Unsupported audio extension"):
                asyncio.run(store.store_input("notes.txt", chunks(b"not audio")))

            self.assertFalse((data_dir / "inputs").exists())

    def test_empty_input_is_rejected_and_temporary_file_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            store = MediaStore(data_dir)

            with self.assertRaisesRegex(ValueError, "must not be empty"):
                asyncio.run(store.store_input("empty.flac", chunks(b"", b"")))

            self.assertEqual(list((data_dir / "inputs").iterdir()), [])

    def test_oversize_input_is_rejected_and_all_partial_files_are_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            store = MediaStore(data_dir, max_input_bytes=5)

            with self.assertRaisesRegex(ValueError, "safety limit"):
                asyncio.run(store.store_input("large.mp3", chunks(b"123", b"456")))

            self.assertEqual(list((data_dir / "inputs").iterdir()), [])

    def test_success_atomically_publishes_audio_and_json_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            store = MediaStore(data_dir)

            result = asyncio.run(
                store.store_input("voice.m4a", chunks(b"first", b"", b"second"), "audio/mp4")
            )

            stored = Path(result["stored_path"])
            metadata = data_dir / "inputs" / f"{result['input_id']}.json"
            self.assertEqual(stored.read_bytes(), b"firstsecond")
            self.assertEqual(json.loads(metadata.read_text(encoding="utf-8")), result)
            self.assertEqual(result["size_bytes"], 11)
            self.assertEqual(result["media_url"], f"/api/media/inputs/{result['input_id']}")
            self.assertEqual(
                sorted(path.suffix for path in (data_dir / "inputs").iterdir()), [".json", ".m4a"]
            )

    def test_uuid_path_resolution_for_inputs_outputs_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            store = MediaStore(data_dir)
            uploaded = asyncio.run(store.store_input("source.opus", chunks(b"audio")))

            job_id = str(uuid4())
            output = data_dir / "outputs" / f"{job_id}.wav"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"output")

            profile_id, sample_id = str(uuid4()), str(uuid4())
            reference = data_dir / "references" / profile_id / f"{sample_id}.wav"
            reference.parent.mkdir(parents=True)
            reference.write_bytes(b"reference")

            self.assertEqual(store.resolve_input(uploaded["input_id"]), Path(uploaded["stored_path"]))
            self.assertEqual(store.resolve_output(job_id), output.resolve())
            self.assertEqual(store.resolve_reference(profile_id, sample_id), reference.resolve())

            with self.assertRaisesRegex(ValueError, "input_id must be a UUID"):
                store.resolve_input("../../escape")
            with self.assertRaisesRegex(ValueError, "job_id must be a UUID"):
                store.resolve_output("not-a-uuid")
            with self.assertRaises(FileNotFoundError):
                store.resolve_reference(profile_id, str(uuid4()))


if __name__ == "__main__":
    unittest.main()
