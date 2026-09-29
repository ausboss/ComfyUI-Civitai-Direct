import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests import stubs  # noqa: F401

from civitai_direct import library


def write(path: Path, content="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        library._cache.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def test_civitai_info_from_any_tool_counts_and_survives_moves(self):
        model = write(self.root / "moved" / "into" / "sub" / "style.safetensors")
        write(model.with_suffix(".civitai.info"), {"id": 28907, "modelId": 16014})
        self.assertEqual(library.find_downloaded_version([self.root], 28907, 16014), model)
        self.assertEqual(library.find_downloaded_version([self.root], "28907"), model)

    def test_other_version_or_model_does_not_match(self):
        model = write(self.root / "style.safetensors")
        write(model.with_suffix(".civitai.info"), {"id": 28907, "modelId": 16014})
        self.assertIsNone(library.find_downloaded_version([self.root], 19075))
        self.assertIsNone(library.find_downloaded_version([self.root], 28907, 999))

    def test_sidecar_without_its_model_file_is_ignored(self):
        write(self.root / "gone.civitai.info", {"id": 1, "modelId": 2})
        self.assertIsNone(library.find_downloaded_version([self.root], 1))

    def test_own_json_record_and_moved_file(self):
        model = write(self.root / "b" / "thing.safetensors")
        write(self.root / "b" / "thing.safetensors.json", {
            "status": "downloaded",
            "model": {"id": 5, "version_id": 6},
            "local": {"model_file": "/old/place/thing.safetensors", "model_file_name": "thing.safetensors"},
        })
        self.assertEqual(library.find_downloaded_version([self.root], 6, 5), model)

    def test_failed_records_are_ignored(self):
        model = write(self.root / "thing.safetensors")
        write(self.root / "thing.safetensors.json", {"status": "failed", "model": {"id": 5, "version_id": 6}})
        self.assertIsNone(library.find_downloaded_version([self.root], 6))
        del model

    def test_unrelated_json_is_never_read(self):
        # A config.json in a model folder must not be parsed as a sidecar.
        write(self.root / "config.json", {"model": {"id": 5, "version_id": 6}})
        write(self.root / "thing.safetensors")
        self.assertIsNone(library.find_downloaded_version([self.root], 6))
        self.assertEqual(library._cache, {})

    def test_changed_sidecar_is_reread(self):
        model = write(self.root / "a.safetensors")
        info = write(model.with_suffix(".civitai.info"), {"id": 1, "modelId": 2})
        self.assertEqual(library.find_downloaded_version([self.root], 1), model)
        info.write_text(json.dumps({"id": 9, "modelId": 2}), encoding="utf-8")
        os.utime(info, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        self.assertIsNone(library.find_downloaded_version([self.root], 1))
        self.assertEqual(library.find_downloaded_version([self.root], 9), model)

    def test_same_name_search_handles_glob_characters(self):
        # Regression: names with [ ] were treated as glob patterns and never matched.
        target = write(self.root / "sub" / "My LoRA [v1.0] (final).safetensors")
        self.assertEqual(library.find_same_named_files([self.root], "My LoRA [v1.0] (final).safetensors"), [target])

    def test_same_name_search_is_case_insensitive_and_bounded(self):
        for i in range(8):
            write(self.root / f"d{i}" / "Model.safetensors")
        self.assertEqual(len(library.find_same_named_files([self.root], "model.SAFETENSORS", limit=5)), 5)

    def test_symlinked_folders_are_followed_and_loops_end(self):
        real = self.root / "real"
        model = write(real / "a.safetensors")
        write(model.with_suffix(".civitai.info"), {"id": 3, "modelId": 4})
        library_root = self.root / "lib"
        library_root.mkdir()
        try:
            (library_root / "linked").symlink_to(real, target_is_directory=True)
            (real / "loop").symlink_to(library_root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        found = library.find_downloaded_version([library_root], 3)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "a.safetensors")

    def test_search_covers_every_root(self):
        second = self.root / "second"
        model = write(second / "x.safetensors")
        write(model.with_suffix(".civitai.info"), {"id": 7, "modelId": 8})
        missing = self.root / "does-not-exist"
        self.assertEqual(library.find_downloaded_version([missing, self.root / "empty", second], 7), model)


class HashTests(unittest.IsolatedAsyncioTestCase):
    async def test_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp) / "f.bin", "abc")
            self.assertEqual(
                await library.calculate_sha256(path),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )


if __name__ == "__main__":
    unittest.main()
