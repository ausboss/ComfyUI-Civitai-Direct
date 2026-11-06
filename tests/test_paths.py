import os
import unittest
from pathlib import Path
from unittest import mock

from tests import stubs

from civitai_direct import paths

M = Path("/comfy/models")


class TypeDirTests(unittest.TestCase):
    def setUp(self):
        stubs.folder_paths.models_dir = str(M)
        stubs.folder_paths.registered = {
            "loras": [str(M / "loras")],
            "checkpoints": [str(M / "checkpoints")],
            "diffusion_models": [str(M / "unet"), str(M / "diffusion_models")],
            "text_encoders": [str(M / "text_encoders"), str(M / "clip")],
        }

    def test_plain_install(self):
        self.assertEqual(paths.resolve_type_dir("loras"), M / "loras")

    def test_comfyuis_legacy_dirs_are_skipped(self):
        self.assertEqual(paths.resolve_type_dir("diffusion_models"), M / "diffusion_models")

    def test_extra_model_paths_is_default_wins_even_with_a_different_folder_name(self):
        # Regression: an A1111-style `models/Lora` marked is_default was ignored
        # because its name is not exactly "loras".
        stubs.folder_paths.registered["loras"] = ["/data/sd/models/Lora", str(M / "loras")]
        self.assertEqual(paths.resolve_type_dir("loras"), Path("/data/sd/models/Lora"))

    def test_extra_path_added_after_comfyuis_own_does_not_take_over(self):
        stubs.folder_paths.registered["loras"] = [str(M / "loras"), "/data/sd/models/Lora"]
        self.assertEqual(paths.resolve_type_dir("loras"), M / "loras")

    def test_a_user_folder_that_happens_to_be_called_unet_is_respected(self):
        stubs.folder_paths.registered["diffusion_models"] = ["/data/models/unet", str(M / "unet"), str(M / "diffusion_models")]
        self.assertEqual(paths.resolve_type_dir("diffusion_models"), Path("/data/models/unet"))

    def test_unregistered_type_falls_back_to_models_dir(self):
        self.assertEqual(paths.resolve_type_dir("hypernetworks"), M / "hypernetworks")

    def test_older_comfyui_that_only_knows_clip(self):
        del stubs.folder_paths.registered["text_encoders"]
        stubs.folder_paths.registered["clip"] = [str(M / "clip")]
        self.assertEqual(paths.resolve_type_dir("text_encoders"), M / "clip")

    def test_target_dir_adds_the_base_model_folder(self):
        self.assertEqual(paths.choose_target_dir("Lora", "sdxl", ""), M / "loras" / "sdxl")
        self.assertEqual(paths.choose_target_dir("Checkpoint", "flux1", ""), M / "diffusion_models" / "flux1")
        self.assertEqual(paths.choose_target_dir("Checkpoint", "sd21", ""), M / "checkpoints" / "sd21")
        self.assertEqual(paths.choose_target_dir("Wildcard", "sdxl", ""), M / "wildcards" / "sdxl")

    def test_flat_layout_when_base_folders_are_off(self):
        with mock.patch.dict(os.environ, {"CIVITAI_BASE_FOLDERS": "off"}):
            self.assertEqual(paths.choose_target_dir("Lora", "sdxl", ""), M / "loras")

    def test_search_covers_every_registered_dir(self):
        stubs.folder_paths.registered["loras"] = ["/data/sd/models/Lora", str(M / "loras")]
        self.assertEqual(paths.search_roots("Lora", "sdxl"), [Path("/data/sd/models/Lora"), M / "loras"])

    def test_checkpoint_search_covers_both_checkpoint_folders(self):
        roots = paths.search_roots("Checkpoint", "sdxl")
        self.assertIn(M / "checkpoints", roots)
        self.assertIn(M / "diffusion_models", roots)
        self.assertIn(M / "unet", roots)
        self.assertEqual(len(roots), len(set(roots)))


class AllRootsTests(unittest.TestCase):
    def test_untyped_search_covers_every_model_folder_once(self):
        stubs.folder_paths.models_dir = str(M)
        stubs.folder_paths.registered = {
            "loras": ["/data/Lora", str(M / "loras")],
            "diffusion_models": [str(M / "unet"), str(M / "diffusion_models")],
        }
        roots = paths.all_model_roots()
        self.assertIn(Path("/data/Lora"), roots)
        self.assertIn(M / "checkpoints", roots)   # unregistered names fall back to models/<name>
        self.assertIn(M / "wildcards", roots)
        self.assertEqual(len(roots), len(set(roots)))


class UserDirTests(unittest.TestCase):
    def test_rejects_path_tricks_in_user_ids(self):
        for bad in ("", "..", "../x", "a/b", "a\\b", ".hidden"):
            with self.subTest(bad):
                with self.assertRaises(RuntimeError):
                    paths.comfyui_user_dir(bad)


class PicturesTests(unittest.TestCase):
    def test_xdg_pictures_dir_is_honored(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "user-dirs.dirs"
            cfg.write_text('XDG_PICTURES_DIR="$HOME/Bilder"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                self.assertEqual(paths._xdg_pictures_dir(Path("/home/u")), Path("/home/u/Bilder"))

    def test_image_dir_setting_wins(self):
        with mock.patch.dict(os.environ, {"CIVITAI_IMAGE_DIR": "/somewhere/else"}):
            self.assertEqual(paths.images_target_dir(), Path("/somewhere/else"))


if __name__ == "__main__":
    unittest.main()
