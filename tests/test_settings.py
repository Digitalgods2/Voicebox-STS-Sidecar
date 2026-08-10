from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from voicebox_sts_bridge.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_are_loopback_only(self) -> None:
        settings = Settings()
        self.assertEqual(settings.voicebox_base_url, "http://127.0.0.1:17493")
        self.assertEqual(settings.bridge_host, "127.0.0.1")
        self.assertTrue(settings.data_dir.is_absolute())

    def test_rejects_non_loopback_voicebox_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            Settings(voicebox_base_url="https://example.com")

    def test_rejects_public_bind(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            Settings(bridge_host="0.0.0.0")

    def test_resolves_explicit_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(Settings(data_dir=Path(directory)).data_dir, Path(directory).resolve())

    def test_from_env_uses_real_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.bridge_port, 8765)
        self.assertEqual(settings.voicebox_base_url, "http://127.0.0.1:17493")


if __name__ == "__main__":
    unittest.main()
