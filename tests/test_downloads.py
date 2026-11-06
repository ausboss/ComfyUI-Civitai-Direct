"""The download pipeline against a local stand-in for Civitai."""

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestServer

from tests import stubs

from civitai_direct import civitai_api, downloads, jobs, library, settings

LORA = b"lora-bytes" * 1000
LORA_SHA = hashlib.sha256(LORA).hexdigest()
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


def workflow_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Pack/flow.json", "{}")
    return buf.getvalue()


class FakeCivitai:
    """Serves the few endpoints the node calls. `mode` changes how downloads behave."""

    def __init__(self):
        self.mode = "ok"            # ok | wrong-bytes | login | early-access
        self.download_hits = 0
        self.model_type = "LORA"
        self.file_name = "cool-style.safetensors"
        self.body = LORA
        self.sha = LORA_SHA
        self.base = "SDXL 1.0"
        self.server: TestServer | None = None

    def url(self, path):
        return str(self.server.make_url(path))

    def version(self, version_id=101):
        return {
            "id": version_id, "modelId": 7, "name": "v1", "baseModel": self.base, "availability": "Public",
            "air": "urn:air:sdxl:lora:civitai:7@101", "trainedWords": ["cool"],
            "files": [{"name": self.file_name, "primary": True, "sizeKB": len(self.body) / 1024,
                       "hashes": {"SHA256": self.sha.upper()}, "downloadUrl": self.url(f"/download/{version_id}")}],
            "images": [{"url": self.url("/img/1.jpeg"), "nsfwLevel": 1, "type": "image"}],
        }

    async def model(self, request):
        return web.json_response({"id": 7, "name": "Cool Style", "type": self.model_type, "nsfw": False,
                                  "creator": {"username": "someone"}, "tags": ["style"],
                                  "modelVersions": [self.version()]})

    async def model_version(self, request):
        return web.json_response(self.version(int(request.match_info["id"])))

    async def download(self, request):
        self.download_hits += 1
        if self.mode == "login":
            return web.json_response({"error": "Unauthorized", "message": "login required"}, status=401)
        if self.mode == "early-access":
            return web.json_response({"error": "Early Access", "deadline": "2030-01-02T00:00:00Z"}, status=403)
        body = b"corrupted!" if self.mode == "wrong-bytes" else self.body
        return web.Response(body=body, headers={"Content-Type": "application/octet-stream"})

    async def image(self, request):
        return web.Response(body=PNG, content_type="image/jpeg")

    async def start(self):
        app = web.Application()
        app.router.add_get("/api/v1/models/{id}", self.model)
        app.router.add_get("/api/v1/model-versions/{id}", self.model_version)
        app.router.add_get("/download/{id}", self.download)
        app.router.add_get("/img/{name}", self.image)
        self.server = TestServer(app)
        await self.server.start_server()

    @property
    def root(self):
        return str(self.server.make_url("/")).rstrip("/")


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.models = self.tmp / "models"
        stubs.folder_paths.models_dir = str(self.models)
        stubs.folder_paths.registered = {
            "loras": [str(self.models / "loras")], "checkpoints": [str(self.models / "checkpoints")],
        }
        stubs.folder_paths.get_public_user_directory = lambda user: str(self.tmp / "user" / user)

        self.fake = FakeCivitai()
        await self.fake.start()
        self.addAsyncCleanup(self.fake.server.close)

        patches = [
            mock.patch.object(civitai_api, "CIVITAI_HOSTS", ("127.0.0.1",)),  # so previews are fetched from the stand-in
            mock.patch.object(civitai_api, "CIVITAI_MODEL_API", self.fake.root + "/api/v1/models/{model_id}"),
            mock.patch.object(civitai_api, "CIVITAI_VERSION_API", self.fake.root + "/api/v1/model-versions/{version_id}"),
            mock.patch.object(settings, "ENV_FILE", self.tmp / "no.env"),
            mock.patch.object(settings, "KEY_FILE", self.tmp / "no.key"),
            mock.patch.dict(os.environ, {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for name in ("CIVITAI_API_KEY", "CIVITAI_SIDECARS", "CIVITAI_PREVIEWS", "CIVITAI_BASE_FOLDERS",
                     "CIVITAI_WORKFLOW_DIR"):
            os.environ.pop(name, None)
        jobs.JOBS.clear()
        library.invalidate_listings()
        library._cache.clear()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def run_download(self, model_id=7, version_id=101):
        job_id = jobs.create_job(model_id=model_id, model_version_id=version_id, bytes_done=0, bytes_total=None,
                                 path=None, error=None)
        await downloads.ModelDownload(job_id, model_id, version_id, "cool-style", "default").run()
        return jobs.JOBS[job_id]

    async def test_fresh_download_lands_in_type_and_base_folder_with_sidecars(self):
        job = await self.run_download()
        self.assertEqual(job["state"], "done", job.get("error"))
        model = self.models / "loras" / "sdxl" / "cool-style.safetensors"
        self.assertEqual(Path(job["path"]), model)
        self.assertEqual(model.read_bytes(), LORA)
        info = json.loads(model.with_suffix(".civitai.info").read_text())
        self.assertEqual((info["id"], info["modelId"]), (101, 7))
        self.assertTrue(model.with_suffix(".preview.png").is_file())      # named by its bytes, not the .jpeg URL
        self.assertFalse(list(model.parent.glob("*.part")))
        self.assertFalse(model.with_suffix(".safetensors.json").exists())  # v1 record only with CIVITAI_SIDECARS=full

    async def test_asking_again_reports_already_exists_without_downloading(self):
        await self.run_download()
        hits = self.fake.download_hits
        job = await self.run_download()
        self.assertEqual(job["state"], "already_exists")
        self.assertEqual(self.fake.download_hits, hits)

    async def test_an_identical_file_from_elsewhere_is_adopted_not_copied(self):
        elsewhere = self.models / "loras" / "my own folder" / "cool-style.safetensors"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_bytes(LORA)
        job = await self.run_download()
        self.assertEqual(job["state"], "already_exists")
        self.assertEqual(Path(job["path"]), elsewhere)
        self.assertEqual(self.fake.download_hits, 0)
        self.assertTrue(elsewhere.with_suffix(".civitai.info").is_file())   # sidecar backfilled
        self.assertEqual(len(list(self.models.rglob("*.safetensors"))), 1)

    async def test_same_name_with_different_contents_gets_a_numbered_copy(self):
        clash = self.models / "loras" / "sdxl" / "cool-style.safetensors"
        clash.parent.mkdir(parents=True)
        clash.write_bytes(b"somebody else's file")
        job = await self.run_download()
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(Path(job["path"]).name, "cool-style (1).safetensors")
        self.assertEqual(clash.read_bytes(), b"somebody else's file")

    async def test_a_hash_mismatch_is_quarantined_and_reported(self):
        self.fake.mode = "wrong-bytes"
        job = await self.run_download()
        self.assertEqual(job["state"], "error")
        self.assertIn("SHA256 mismatch", job["error"])
        folder = self.models / "loras" / "sdxl"
        self.assertTrue((folder / "cool-style.safetensors.corrupt").is_file())
        self.assertFalse((folder / "cool-style.safetensors").exists())
        self.assertFalse(list(folder.rglob("*.civitai.info")))

    async def test_a_login_wall_without_a_key_says_how_to_add_one(self):
        self.fake.mode = "login"
        job = await self.run_download()
        self.assertEqual(job["state"], "error")
        self.assertIn("API key", job["error"])
        self.assertFalse(list(self.models.rglob("*.part")))

    async def test_early_access_names_the_date(self):
        self.fake.mode = "early-access"
        job = await self.run_download()
        self.assertEqual(job["state"], "error")
        self.assertIn("Early Access", job["error"])
        self.assertIn("2030-01-02", job["error"])

    async def test_unknown_model_is_a_readable_error(self):
        with mock.patch.object(civitai_api, "CIVITAI_MODEL_API", self.fake.root + "/nothing/{model_id}"):
            job = await self.run_download(model_id=99)
        self.assertEqual(job["state"], "error")
        self.assertIn("404", job["error"])
        self.assertIn("Check the model or version id", job["error"])

    async def test_flat_layout_saves_straight_into_the_type_folder(self):
        os.environ["CIVITAI_BASE_FOLDERS"] = "off"
        job = await self.run_download()
        self.assertEqual(Path(job["path"]), self.models / "loras" / "cool-style.safetensors")

    async def test_full_sidecar_mode_adds_the_json_and_markdown_pair(self):
        os.environ["CIVITAI_SIDECARS"] = "full"
        job = await self.run_download()
        model = Path(job["path"])
        record = json.loads(Path(str(model) + ".json").read_text())
        self.assertEqual(record["schema"], "ausboss-civitai-model-sidecar-v1")
        self.assertEqual(record["local"]["sha256"], LORA_SHA)
        self.assertTrue(Path(str(model) + ".md").is_file())

    async def test_previews_can_be_switched_off(self):
        os.environ["CIVITAI_PREVIEWS"] = "off"
        job = await self.run_download()
        self.assertFalse(list(Path(job["path"]).parent.glob("*.preview.*")))

    async def test_workflow_zip_is_extracted_and_recorded_apart_from_the_workflow(self):
        self.fake.model_type = "Workflows"
        self.fake.file_name = "flow.zip"
        self.fake.body = workflow_zip()
        self.fake.sha = hashlib.sha256(self.fake.body).hexdigest()
        job = await self.run_download()
        self.assertEqual(job["state"], "done", job.get("error"))
        folder = self.tmp / "user" / "default" / "workflows" / "civitai-direct" / "sdxl" / "flow"
        self.assertEqual(Path(job["path"]), folder)
        self.assertTrue((folder / "flow.json").is_file())
        self.assertFalse(list(folder.parent.glob("*.zip")))
        meta = self.tmp / "user" / "default" / ".civitai-direct" / "workflow-metadata" / "sdxl"
        self.assertTrue((meta / "flow.civitai.json").is_file())
        # a second request recognizes it from that record
        again = await self.run_download()
        self.assertEqual(again["state"], "already_exists")


if __name__ == "__main__":
    unittest.main()
