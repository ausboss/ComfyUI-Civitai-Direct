import json
import unittest
from unittest import mock

from tests import stubs  # noqa: F401

from civitai_direct import civitai_api


class HostTests(unittest.TestCase):
    def test_only_civitai_domains_pass(self):
        good = ["https://civitai.com/x", "https://image.civitai.com/a/b.png", "https://civitai.red/x",
                "http://civitai.com/x"]
        bad = ["https://evilcivitai.red/x", "https://civitai.com.evil.io/x", "http://127.0.0.1:8188/x",
               "https://169.254.169.254/latest", "file:///etc/passwd", "ftp://civitai.com/x", "", None,
               "https://civitai.com@evil.io/x"]
        for url in good:
            with self.subTest(url):
                self.assertTrue(civitai_api.is_civitai_url(url))
        for url in bad:
            with self.subTest(url):
                self.assertFalse(civitai_api.is_civitai_url(url))

    def test_token_is_only_added_for_civitai(self):
        with mock.patch.object(civitai_api, "get_api_key", return_value="k3y"):
            self.assertEqual(civitai_api.add_token_to_url("https://civitai.com/api/download/models/1"),
                             "https://civitai.com/api/download/models/1?token=k3y")
            self.assertEqual(civitai_api.add_token_to_url("https://civitai.com/a?type=Model"),
                             "https://civitai.com/a?type=Model&token=k3y")
            self.assertEqual(civitai_api.add_token_to_url("https://example.com/a"), "https://example.com/a")
            self.assertEqual(civitai_api.add_token_to_url("https://evilcivitai.red/a"), "https://evilcivitai.red/a")

    def test_no_key_no_token(self):
        with mock.patch.object(civitai_api, "get_api_key", return_value=""):
            self.assertEqual(civitai_api.add_token_to_url("https://civitai.com/a"), "https://civitai.com/a")

    def test_user_agent_says_what_it_is(self):
        self.assertTrue(civitai_api.user_agent().startswith("ComfyUI-Civitai-Direct/"))


class RequestAuthTests(unittest.TestCase):
    def sent_headers(self, url, key="k3y"):
        session = mock.Mock()
        with mock.patch.object(civitai_api, "get_api_key", return_value=key):
            civitai_api.get(session, url, headers={"Accept": "application/json"})
        return session.get.call_args.kwargs["headers"]

    def test_the_key_goes_to_civitai_only(self):
        self.assertEqual(self.sent_headers("https://civitai.com/api/v1/models/1")["Authorization"], "Bearer k3y")
        self.assertEqual(self.sent_headers("https://image.civitai.com/x.png")["Accept"], "application/json")
        for url in ("http://127.0.0.1:8188/x", "https://evil.example/x", "https://civitai.com.evil.io/x"):
            with self.subTest(url):
                self.assertNotIn("Authorization", self.sent_headers(url))

    def test_no_key_no_header(self):
        self.assertNotIn("Authorization", self.sent_headers("https://civitai.com/x", key=""))

    def test_the_session_itself_carries_no_credentials(self):
        import asyncio

        async def check():
            with mock.patch.object(civitai_api, "get_api_key", return_value="k3y"):
                async with civitai_api.open_session() as session:
                    return dict(session.headers)

        headers = asyncio.run(check())
        self.assertNotIn("Authorization", headers)
        self.assertTrue(headers["User-Agent"].startswith("ComfyUI-Civitai-Direct/"))


class UrlParsingTests(unittest.TestCase):
    def test_page_url_with_version(self):
        self.assertEqual(civitai_api.parse_ids_from_url("https://civitai.com/models/16014/slug?modelVersionId=19075"),
                         (16014, 19075, "slug"))

    def test_download_url_number_is_a_version_id(self):
        self.assertEqual(civitai_api.parse_ids_from_url("https://civitai.com/api/download/models/28907?type=Model"),
                         (None, 28907, None))

    def test_image_page(self):
        self.assertEqual(civitai_api.parse_image_id_from_url("https://civitai.com/images/143955892?x=1"), 143955892)
        self.assertIsNone(civitai_api.parse_image_id_from_url("https://civitai.com/models/1"))


class GatedErrorTests(unittest.TestCase):
    def message(self, body, key, availability=None):
        with mock.patch.object(civitai_api, "get_api_key_with_source", return_value=(key, "environment" if key else None)):
            return str(civitai_api.gated_error("Civitai returned 403", body, availability))

    def test_early_access_names_the_date_and_says_a_key_will_not_help(self):
        text = self.message(json.dumps({"error": "Early Access", "deadline": "2026-08-22T18:30:00.000Z"}), "k")
        self.assertIn("Early Access", text)
        self.assertIn("2026-08-22", text)
        self.assertIn("API key alone does not unlock it", text)

    def test_early_access_from_the_models_api_flag(self):
        self.assertIn("Early Access", self.message("", "k", availability="EarlyAccess"))

    def test_login_required_without_a_key_says_how_to_add_one(self):
        text = self.message(json.dumps({"error": "Unauthorized"}), "")
        self.assertIn("no API key is set", text)
        self.assertIn("CIVITAI_API_KEY", text)

    def test_login_required_with_a_key_blames_the_key(self):
        text = self.message("", "k3y")
        self.assertIn("refused the API key", text)
        self.assertNotIn("k3y", text)


if __name__ == "__main__":
    unittest.main()
