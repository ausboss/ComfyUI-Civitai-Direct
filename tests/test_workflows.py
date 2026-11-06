import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests import stubs  # noqa: F401

from civitai_direct import workflows


def make_zip(path: Path, entries: dict) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


class ZipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_and_hoists_a_single_wrapper_folder(self):
        z = make_zip(self.tmp / "w.zip", {"Pack/flow.json": "{}", "Pack/notes.txt": "hi"})
        out = self.tmp / "out"
        workflows.extract_workflow_zip(z, out)
        self.assertTrue((out / "flow.json").is_file())
        self.assertTrue((out / "notes.txt").is_file())

    def test_rejects_entries_that_escape_the_folder(self):
        z = make_zip(self.tmp / "evil.zip", {"../escape.txt": "x"})
        with self.assertRaisesRegex(RuntimeError, "unsafe ZIP entry"):
            workflows.extract_workflow_zip(z, self.tmp / "out")
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_rejects_absolute_entry_paths(self):
        z = make_zip(self.tmp / "abs.zip", {"/tmp/civitai-direct-should-not-exist.txt": "x"})
        with self.assertRaisesRegex(RuntimeError, "unsafe ZIP entry"):
            workflows.extract_workflow_zip(z, self.tmp / "out")

    def test_rejects_an_oversized_archive(self):
        z = make_zip(self.tmp / "big.zip", {"a.bin": b"x"})
        original = workflows.MAX_ZIP_TOTAL_BYTES
        workflows.MAX_ZIP_TOTAL_BYTES = 0
        try:
            with self.assertRaisesRegex(RuntimeError, "more than 4 GiB"):
                workflows.extract_workflow_zip(z, self.tmp / "out")
        finally:
            workflows.MAX_ZIP_TOTAL_BYTES = original

    def test_rejects_too_many_entries(self):
        z = make_zip(self.tmp / "many.zip", {f"f{i}.txt": "x" for i in range(5)})
        original = workflows.MAX_ZIP_ENTRIES
        workflows.MAX_ZIP_ENTRIES = 3
        try:
            with self.assertRaisesRegex(RuntimeError, "too many entries"):
                workflows.extract_workflow_zip(z, self.tmp / "out")
        finally:
            workflows.MAX_ZIP_ENTRIES = original

    def test_nested_zip_is_unpacked_in_place(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("deep.json", "{}")
        z = make_zip(self.tmp / "outer.zip", {"top.json": "{}", "inner.zip": inner.getvalue()})
        out = self.tmp / "out"
        workflows.extract_workflow_zip(z, out)
        self.assertTrue((out / "top.json").is_file())
        self.assertTrue((out / "inner" / "deep.json").is_file())
        self.assertFalse((out / "inner.zip").exists())

    def test_nesting_depth_is_limited(self):
        data = b"{}"
        for _ in range(workflows.MAX_NESTED_ZIP_DEPTH + 2):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("n.zip" if data != b"{}" else "leaf.json", data)
            data = buf.getvalue()
        z = self.tmp / "deep.zip"
        z.write_bytes(data)
        with self.assertRaisesRegex(RuntimeError, "depth limit"):
            workflows.extract_workflow_zip(z, self.tmp / "out")


class DestinationTests(unittest.TestCase):
    def test_zip_extracts_into_a_folder_named_after_it(self):
        self.assertEqual(workflows.workflow_artifact_path(Path("/w/pack.zip")), Path("/w/pack"))
        self.assertEqual(workflows.workflow_artifact_path(Path("/w/flow.json")), Path("/w/flow.json"))

    def test_unique_destination_counts_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "pack").mkdir()
            download, artifact = workflows.unique_workflow_destination(base / "pack.zip")
            self.assertEqual((download.name, artifact.name), ("pack (1).zip", "pack (1)"))

    def test_sidecars_live_apart_from_the_workflow(self):
        meta = Path("/meta")
        self.assertEqual(workflows.workflow_sidecar_json_path(meta, Path("/w/pack")), meta / "pack.civitai.json")
        self.assertEqual(workflows.workflow_sidecar_json_path(meta, Path("/w/flow.json")), meta / "flow.civitai.json")


if __name__ == "__main__":
    unittest.main()
