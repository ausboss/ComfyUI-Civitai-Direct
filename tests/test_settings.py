import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import stubs  # noqa: F401

from civitai_direct import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.env_file = tmp / ".env"
        self.key_file = tmp / "civitai_api_key.txt"
        patches = [
            mock.patch.object(settings, "ENV_FILE", self.env_file),
            mock.patch.object(settings, "KEY_FILE", self.key_file),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for name in ("CIVITAI_API_KEY", "CIVITAI_SIDECARS", "CIVITAI_PREVIEWS", "CIVITAI_BASE_FOLDERS"):
            os.environ.pop(name, None)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_node_folder_is_the_repo_root(self):
        real = Path(settings.__file__).resolve().parent.parent
        self.assertTrue((real / "pyproject.toml").is_file())

    def test_no_key_anywhere(self):
        self.assertEqual(settings.get_api_key_with_source(), ("", None))

    def test_key_order_is_environment_then_env_file_then_key_file(self):
        self.key_file.write_text("from-key-file\n", encoding="utf-8")
        self.assertEqual(settings.get_api_key_with_source(), ("from-key-file", "civitai_api_key.txt"))
        self.env_file.write_text("# comment\nexport CIVITAI_API_KEY='from-env-file'\n", encoding="utf-8")
        self.assertEqual(settings.get_api_key_with_source(), ("from-env-file", ".env"))
        os.environ["CIVITAI_API_KEY"] = " from-environment "
        self.assertEqual(settings.get_api_key_with_source(), ("from-environment", "environment"))

    def test_files_are_reread_without_a_restart(self):
        self.assertEqual(settings.get_api_key(), "")
        self.env_file.write_text("CIVITAI_API_KEY=added-later", encoding="utf-8")
        self.assertEqual(settings.get_api_key(), "added-later")

    def test_env_file_handles_quotes_blank_lines_and_windows_line_endings(self):
        self.env_file.write_text('\r\nCIVITAI_SIDECARS="full"\r\nCIVITAI_PREVIEWS = off\r\n', encoding="utf-8")
        self.assertEqual(settings.sidecar_mode(), "full")
        self.assertEqual(settings.previews_mode(), "off")

    def test_modes_fall_back_to_their_defaults_on_bad_values(self):
        os.environ.update(CIVITAI_SIDECARS="nonsense", CIVITAI_PREVIEWS="", CIVITAI_BASE_FOLDERS="maybe")
        self.assertEqual(settings.sidecar_mode(), "info")
        self.assertEqual(settings.previews_mode(), "sfw")
        self.assertTrue(settings.base_folders_enabled())

    def test_base_folders_can_be_turned_off(self):
        os.environ["CIVITAI_BASE_FOLDERS"] = "OFF"
        self.assertFalse(settings.base_folders_enabled())


if __name__ == "__main__":
    unittest.main()
