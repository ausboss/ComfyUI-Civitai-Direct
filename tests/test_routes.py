import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests import stubs

from civitai_direct import jobs, routes


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        stubs.folder_paths.models_dir = str(tmp / "models")
        stubs.folder_paths.registered = {"loras": [str(tmp / "models" / "loras")]}
        stubs.folder_paths.get_public_user_directory = lambda user: str(tmp / "user" / user)
        jobs.JOBS.clear()
        app = web.Application()
        app.add_routes(routes.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._tmp.cleanup()

    async def test_health_reports_settings_and_dirs(self):
        resp = await self.client.get("/civitai/health")
        body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["status"], "ok")
        for key in ("version", "has_civitai_api_key", "api_key_source", "sidecars", "previews", "base_folders",
                    "dirs", "images_dir", "workflows_dir"):
            self.assertIn(key, body)
        self.assertTrue(body["dirs"]["loras"].endswith("loras"))

    async def test_download_rejects_bodies_that_are_not_objects(self):
        for payload in ([1, 2], "text", 5, None):
            resp = await self.client.post("/civitai/download", data=json.dumps(payload),
                                          headers={"Content-Type": "application/json"})
            self.assertEqual(resp.status, 400, payload)
        resp = await self.client.post("/civitai/download", data="not json")
        self.assertEqual(resp.status, 400)

    async def test_download_needs_something_to_go_on(self):
        resp = await self.client.post("/civitai/download", json={})
        self.assertEqual(resp.status, 400)
        self.assertIn("error", await resp.json())
        resp = await self.client.post("/civitai/download", json={"model_id": "abc", "url": 5})
        self.assertEqual(resp.status, 400)

    async def test_both_userscript_payload_shapes_are_accepted(self):
        with mock.patch.object(routes, "ModelDownload") as download:
            download.return_value.run = mock.AsyncMock()
            grid = await self.client.post("/civitai/download", json={"model_id": 16014, "model_version_id": 28907})
            page = await self.client.post("/civitai/download", json={
                "url": "https://civitai.com/api/download/models/19075",
                "metadata": {"page_url": "https://civitai.com/models/16014/x?modelVersionId=19075"},
            })
            self.assertEqual((grid.status, page.status), (200, 200))
            calls = [c.args for c in download.call_args_list]
            self.assertEqual(calls[0][1:4], (16014, 28907, None))
            self.assertEqual(calls[1][1:4], (16014, 19075, "x"))

    async def test_status_and_jobs(self):
        job_id = jobs.create_job(kind="x")
        body = await (await self.client.get(f"/civitai/status?id={job_id}")).json()
        self.assertEqual(body["state"], "resolving")
        self.assertEqual((await self.client.get("/civitai/status?id=999")).status, 404)
        self.assertEqual((await self.client.get("/civitai/status?id=abc")).status, 404)
        self.assertIn(str(job_id), await (await self.client.get("/civitai/jobs")).json())

    async def test_exists_validates_and_answers(self):
        self.assertEqual((await self.client.get("/civitai/exists")).status, 400)
        self.assertEqual((await self.client.get("/civitai/exists?model_version_id=zip")).status, 400)
        body = await (await self.client.get("/civitai/exists?model_version_id=5&type=LORA&base_model=SDXL")).json()
        self.assertEqual(body, {"exists": False, "path": None, "model_version_id": 5})

    async def test_image_download_needs_an_id_or_url(self):
        self.assertEqual((await self.client.post("/civitai/download-image", json={})).status, 400)
        self.assertEqual((await self.client.post("/civitai/download-image", data="[]")).status, 400)


if __name__ == "__main__":
    unittest.main()
