from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest

from voicebox_sts_bridge.youtube_cache import YouTubeCacheError, YouTubeSourceCache


def probe() -> dict:
    return {
        "format": {"duration": "12.0", "size": "128"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        ],
    }


def publish(cache: YouTubeSourceCache, job_id: str, video_id: str, payload: bytes) -> dict:
    staging = cache.prepare_staging(job_id)
    staging.mkdir(parents=True)
    source = staging / "source.mkv"
    source.write_bytes(payload)
    return cache.publish(
        staging,
        source,
        video_id=video_id,
        source_url=f"https://youtu.be/{video_id}",
        video={"id": video_id, "title": f"Video {video_id}", "channel": "Owner"},
        source_probe=probe(),
        yt_dlp_version="2026.07.04",
    )


class YouTubeSourceCacheTests(unittest.TestCase):
    def test_publish_lookup_hit_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = YouTubeSourceCache(directory, max_media_bytes=1024)

            published = publish(cache, "job-one", "abc123", b"authorized video")
            found = cache.lookup("abc123")
            recorded = cache.record_hit("abc123")

            self.assertEqual(Path(published["source_path"]).read_bytes(), b"authorized video")
            self.assertEqual(found["video_id"], "abc123")
            self.assertEqual(recorded["hit_count"], 1)
            self.assertTrue(cache.status()["active"])
            self.assertIsNone(cache.lookup("different"))
            self.assertTrue(cache.status()["active"])
            cleared = cache.clear()
            self.assertTrue(cleared["cleared"])
            self.assertFalse(cache.status()["active"])

    def test_new_video_replaces_the_only_active_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = YouTubeSourceCache(directory, max_media_bytes=1024)
            first = publish(cache, "job-one", "first", b"first video")
            first_path = Path(first["source_path"])

            second = publish(cache, "job-two", "second", b"second video")

            self.assertEqual(first_path, Path(second["source_path"]))
            self.assertNotEqual(first_path.read_bytes(), b"first video")
            self.assertEqual(Path(second["source_path"]).read_bytes(), b"second video")
            self.assertEqual(cache.status()["video_id"], "second")
            self.assertEqual(
                [path.name for path in cache.root.iterdir() if path.is_dir()],
                ["current"],
            )

    def test_failed_replacement_preserves_the_previous_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = YouTubeSourceCache(directory, max_media_bytes=1024)
            publish(cache, "job-one", "first", b"first video")
            staging = cache.prepare_staging("job-two")
            staging.mkdir(parents=True)
            empty = staging / "source.mkv"
            empty.write_bytes(b"")

            with self.assertRaisesRegex(YouTubeCacheError, "cache limit"):
                cache.publish(
                    staging,
                    empty,
                    video_id="second",
                    source_url="https://youtu.be/second",
                    video={"id": "second"},
                    source_probe=probe(),
                    yt_dlp_version=None,
                )

            self.assertEqual(cache.status()["video_id"], "first")
            cache.discard_staging(staging)

    def test_corrupt_media_is_invalidated_on_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = YouTubeSourceCache(directory, max_media_bytes=1024)
            entry = publish(cache, "job-one", "abc123", b"original")
            Path(entry["source_path"]).write_bytes(b"changed size")

            self.assertIsNone(cache.lookup("abc123"))
            self.assertFalse(cache.status()["active"])

    def test_startup_recovers_a_retired_entry_and_removes_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = YouTubeSourceCache(directory, max_media_bytes=1024)
            publish(cache, "job-one", "abc123", b"original")
            retired = cache.root / ".retired-interrupted"
            os.replace(cache.current_dir, retired)
            orphan = cache.root / ".staging-abandoned"
            orphan.mkdir()
            (orphan / "partial.tmp").write_bytes(b"partial")

            recovered = YouTubeSourceCache(directory, max_media_bytes=1024)

            self.assertEqual(recovered.status()["video_id"], "abc123")
            self.assertFalse(retired.exists())
            self.assertFalse(orphan.exists())


if __name__ == "__main__":
    unittest.main()
