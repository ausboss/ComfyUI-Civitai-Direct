import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from tests import stubs  # noqa: F401

from civitai_direct import images, jobs


class JobTests(unittest.TestCase):
    def setUp(self):
        jobs.JOBS.clear()

    def test_finished_jobs_are_pruned_but_running_ones_stay(self):
        running = jobs.create_job(kind="x")
        for _ in range(jobs.MAX_FINISHED_JOBS + 25):
            job_id = jobs.create_job(kind="x")
            jobs.JOBS[job_id]["state"] = "done"
        jobs.create_job(kind="x")  # triggers a prune
        finished = [j for j in jobs.JOBS.values() if j["state"] in jobs.TERMINAL_STATES]
        self.assertLessEqual(len(finished), jobs.MAX_FINISHED_JOBS)
        self.assertIn(running, jobs.JOBS)

    def test_a_destination_can_only_be_claimed_once(self):
        first = jobs.claim_destination("/models/A.safetensors")
        self.assertIsNotNone(first)
        self.assertIsNone(jobs.claim_destination("/models/a.safetensors"))
        jobs.release_destination(first)
        self.assertIsNotNone(jobs.claim_destination("/models/A.safetensors"))

    def test_error_messages_hide_the_token_and_keep_our_wording(self):
        self.assertEqual(jobs.job_error_message(RuntimeError("no key set")), "no key set")
        message = jobs.job_error_message(ValueError("bad https://civitai.com/x?token=SECRET"))
        self.assertTrue(message.startswith("ValueError:"))
        self.assertNotIn("SECRET", message)


def png_with_text(chunks):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    out = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    out += chunk(b"IHDR", ihdr)
    for key, value in chunks.items():
        out += chunk(b"tEXt", key.encode() + b"\x00" + value.encode())
    out += chunk(b"IEND", b"")
    return out


class ImageHelperTests(unittest.TestCase):
    def test_original_url_rewrite(self):
        self.assertEqual(
            images.original_image_url("https://image.civitai.com/key/uuid/width=1152/1.jpeg"),
            "https://image.civitai.com/key/uuid/original=true/1.jpeg",
        )
        self.assertEqual(
            images.original_image_url("https://image.civitai.com/key/uuid/1.jpeg"),
            "https://image.civitai.com/key/uuid/original=true/1.jpeg",
        )
        self.assertEqual(images.original_image_url("https://example.com/a/b.png"), "https://example.com/a/b.png")

    def test_png_text_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            path.write_bytes(png_with_text({"parameters": "a cat", "workflow": json.dumps({"nodes": []})}))
            self.assertEqual(images.extract_png_text_chunks(path), {"parameters": "a cat", "workflow": '{"nodes": []}'})
            (Path(tmp) / "b.png").write_bytes(b"not a png")
            self.assertEqual(images.extract_png_text_chunks(Path(tmp) / "b.png"), {})

    def test_workflow_from_page_data(self):
        wf = {"nodes": [1]}
        data = {"generation_data": {"meta": {"comfy": json.dumps({"prompt": {}, "workflow": wf})}}}
        self.assertEqual(images.comfy_workflow_from_request(data), wf)
        self.assertIsNone(images.comfy_workflow_from_request({}))

    def test_filenames_and_unique_paths(self):
        self.assertEqual(images.image_filename({}, "https://x/a.png", 7), "civitai-image-7.png")
        self.assertEqual(images.image_filename({"filename": "../evil.png"}, "", 7), ".._evil.png")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "a.png"
            base.write_bytes(b"x")
            self.assertEqual(images.unique_image_destination_path(base, False).name, "a (1).png")

    def test_existing_image_lookup_ignores_other_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "civitai-image-12.png").write_bytes(b"x")
            self.assertIsNone(images.existing_image_for_id(Path(tmp), 1))
            self.assertEqual(images.existing_image_for_id(Path(tmp), 12).name, "civitai-image-12.png")


class ImageSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_non_civitai_image_url_is_refused_before_any_request(self):
        # Regression: the URL from the browser was fetched with the API key attached.
        jobs.JOBS.clear()
        job_id = jobs.create_job(kind="image", image_id=None, bytes_done=0, bytes_total=None, path=None, error=None)
        with mock.patch.object(images, "open_session") as open_session:
            open_session.return_value.__aenter__.return_value = mock.Mock()
            await images.run_image_download(job_id, {"image_url": "http://169.254.169.254/latest/meta-data"}, None)
        job = jobs.JOBS[job_id]
        self.assertEqual(job["state"], "error")
        self.assertIn("civitai.com", job["error"])
        open_session.return_value.__aenter__.return_value.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
