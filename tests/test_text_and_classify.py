import unittest

from tests import stubs  # noqa: F401

from civitai_direct import classify, text


class TextTests(unittest.TestCase):
    def test_safe_filename_strips_path_and_reserved_characters(self):
        self.assertEqual(text.safe_filename('a/b\\c:d*e?"f<g>h|i.safetensors'), "a_b_c_d_e__f_g_h_i.safetensors")

    def test_safe_filename_avoids_windows_device_names(self):
        self.assertEqual(text.safe_filename("CON.safetensors"), "_CON.safetensors")
        self.assertEqual(text.safe_filename("nul"), "_nul")
        self.assertEqual(text.safe_filename("console.safetensors"), "console.safetensors")

    def test_safe_filename_trims_trailing_dots_and_falls_back(self):
        self.assertEqual(text.safe_filename("name. ."), "name")
        self.assertEqual(text.safe_filename("   ", fallback="model"), "model")

    def test_console_safe_escapes_non_ascii(self):
        self.assertEqual(text.console_safe("a线b"), "a\\u7ebfb")
        self.assertEqual(text.console_safe(None), "")

    def test_redact_civitai_token(self):
        self.assertEqual(
            text.redact_civitai_token("https://civitai.com/api/download/models/1?type=Model&token=abc123&x=1"),
            "https://civitai.com/api/download/models/1?type=Model&token=REDACTED&x=1",
        )

    def test_strip_html(self):
        self.assertEqual(text.strip_html("<p>Hello&nbsp;<b>you</b></p><p>bye</p>"), "Hello\xa0you\n\nbye")


class ClassifyTests(unittest.TestCase):
    def test_base_model_slugs(self):
        cases = {
            "SD 1.5": "sd15", "SDXL 1.0": "sdxl", "SDXL Lightning": "sdxl", "Pony": "pony",
            "Illustrious": "illustrious", "Flux.1 D": "flux1", "Flux.1 S": "flux1",
            "SD 3.5 Large": "sd35-large", "SD 3.5 Medium": "sd35-medium", "SD 2.1": "sd21",
            "SD 1.4": "sd14", "ZImageTurbo": "zimage-turbo", "Flux.2 Klein 9B": "flux2-klein-9b",
            "Wan Video 2.2 I2V-A14B": "wan-video-22-i2v-a14b", "": "unknown",
        }
        for name, slug in cases.items():
            with self.subTest(name):
                self.assertEqual(classify.normalize_base_model(name), slug)

    def test_unknown_base_model_uses_the_air_ecosystem(self):
        self.assertEqual(classify.normalize_base_model("Qwen 2.1", "urn:air:qwen21:lora:civitai:1@2"), "qwen21")
        self.assertEqual(classify.normalize_base_model("Brand New", "urn:air:other:lora:civitai:1@2"), "brand-new")

    def test_model_types(self):
        for raw, expected in {
            "LORA": "Lora", "LoCon": "Lora", "DoRA": "Lora", "Checkpoint": "Checkpoint",
            "TextualInversion": "Embedding", "VAE": "VAE", "Upscaler": "Upscaler",
            "Workflows": "Workflow", "CLIP": "TextEncoder", "MotionModule": "MotionModule",
        }.items():
            with self.subTest(raw):
                self.assertEqual(classify.normalize_model_type(raw), expected)

    def test_older_stable_diffusion_checkpoints_go_to_checkpoints(self):
        # Regression: sd14 and sd21 were sent to diffusion_models/, where
        # Load Checkpoint cannot see them.
        for base in ("sd14", "sd15", "sd21", "sdxl", "pony", "illustrious", "sd3", "sd35-large"):
            with self.subTest(base):
                self.assertEqual(classify.folder_name_for("Checkpoint", base), "checkpoints")
        for base in ("flux1", "wan-video-22-t2v-a14b", "krea2"):
            with self.subTest(base):
                self.assertEqual(classify.folder_name_for("Checkpoint", base), "diffusion_models")
        self.assertEqual(classify.folder_name_for("Checkpoint", "flux1", "Some AIO merge"), "checkpoints")

    def test_type_folders(self):
        self.assertEqual(classify.folder_name_for("Lora", "sdxl"), "loras")
        self.assertEqual(classify.folder_name_for("TextEncoder", "flux1"), "text_encoders")
        self.assertIsNone(classify.folder_name_for("Wildcard", "sdxl"))
        self.assertEqual(classify.fallback_dirname("Wildcard"), "wildcards")

    def test_nsfw_bitmask_and_permissions(self):
        self.assertEqual(classify.decode_nsfw_level(3), (["PG", "PG13"], "PG13"))
        self.assertEqual(classify.decode_nsfw_level("nope"), ([], None))
        self.assertEqual(classify.parse_permission_list("{Image,RentCivit,Rent}"), ["Image", "RentCivit", "Rent"])
        self.assertEqual(classify.parse_permission_list(["Image"]), ["Image"])

    def test_wan_detection_and_filename(self):
        meta = {"base_model": "wan-video-22-t2v-a14b", "model_slug": "my-lora", "version_name": "v1 high",
                "model_version_id": 42}
        self.assertTrue(classify.is_wan_model(meta))
        self.assertEqual(classify.slug_aware_filename(meta), "my-lora__v1_high-v42.safetensors")
        self.assertFalse(classify.is_wan_model({"base_model": "sdxl", "model_name": "Swan Lake"}))


if __name__ == "__main__":
    unittest.main()
