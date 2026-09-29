"""Where things go on disk.

Model folders always come from ComfyUI's `folder_paths`, which already honors
extra_model_paths.yaml. Images and workflows have no folder_paths entry, so
they use CIVITAI_IMAGE_DIR / CIVITAI_WORKFLOW_DIR or a sensible default.
"""

import os
import re
from pathlib import Path
from typing import Optional

import folder_paths

from .classify import TYPE_FALLBACK_DIRNAME, TYPE_TO_FOLDER_NAME, fallback_dirname, folder_name_for
from .settings import base_folders_enabled, get_setting
from .text import safe_folder

# Folder names older ComfyUI versions know a type by.
_FOLDER_ALIASES = {
    "text_encoders": ("text_encoders", "clip"),
    "diffusion_models": ("diffusion_models", "unet"),
}
# The legacy directories ComfyUI itself lists first for those types.
_LEGACY_DIRNAMES = ("unet", "clip")


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def registered_dirs(folder_name: str) -> list[Path]:
    """Every directory ComfyUI has registered for a folder name, in ComfyUI's
    order (extra_model_paths.yaml entries included)."""
    for name in _FOLDER_ALIASES.get(folder_name, (folder_name,)):
        try:
            paths = folder_paths.get_folder_paths(name)
        except KeyError:
            continue
        if paths:
            return [Path(p) for p in paths]
    return []


def resolve_type_dir(folder_name: str) -> Path:
    """Where new downloads of this type go: the first directory ComfyUI lists,
    which is the user's own choice when extra_model_paths.yaml marks a path
    `is_default`. The exception is ComfyUI's legacy models/unet and models/clip,
    which it lists first for some types and which new files should skip."""
    dirs = registered_dirs(folder_name)
    if not dirs:
        return Path(folder_paths.models_dir) / folder_name
    legacy = [Path(folder_paths.models_dir) / name for name in _LEGACY_DIRNAMES]
    for directory in dirs:
        if not any(_same_path(directory, old) for old in legacy):
            return directory
    return dirs[0]


def choose_target_dir(model_type: str, base_model: str, name_haystack: str) -> Path:
    folder_name = folder_name_for(model_type, base_model, name_haystack)
    root = (
        resolve_type_dir(folder_name)
        if folder_name
        else Path(folder_paths.models_dir) / fallback_dirname(model_type)
    )
    return root / base_model if base_folders_enabled() else root


def search_roots(model_type: str, base_model: str, name_haystack: str = "") -> list[Path]:
    """Every folder that might already hold this kind of model: all the
    directories registered for its type, not only the one new files go to.
    Checkpoints also cover diffusion_models, where older releases put some."""
    folder_name = folder_name_for(model_type, base_model, name_haystack)
    if folder_name is None:
        return [Path(folder_paths.models_dir) / fallback_dirname(model_type)]

    names = ["checkpoints", "diffusion_models"] if model_type == "Checkpoint" else [folder_name]
    roots: list[Path] = []
    for name in names:
        for directory in registered_dirs(name) or [Path(folder_paths.models_dir) / name]:
            if not any(_same_path(directory, existing) for existing in roots):
                roots.append(directory)
    return roots


def all_model_roots() -> list[Path]:
    """Every model folder there is, for a check that doesn't know the type."""
    names = sorted({*TYPE_TO_FOLDER_NAME.values(), "checkpoints", "diffusion_models"})
    roots: list[Path] = []
    candidates = [d for name in names for d in (registered_dirs(name) or [Path(folder_paths.models_dir) / name])]
    candidates += [Path(folder_paths.models_dir) / dirname for dirname in TYPE_FALLBACK_DIRNAME.values()]
    for directory in candidates:
        if not any(_same_path(directory, existing) for existing in roots):
            roots.append(directory)
    return roots


def comfyui_user_dir(user_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", user_id or ""):
        raise RuntimeError("invalid ComfyUI user id")

    get_public_dir = getattr(folder_paths, "get_public_user_directory", None)
    if callable(get_public_dir):
        user_dir = get_public_dir(user_id)
        if user_dir is None:
            raise RuntimeError("invalid ComfyUI user directory")
    else:
        user_dir = Path(folder_paths.get_user_directory()) / user_id

    return Path(user_dir)


def _windows_pictures_dir() -> Optional[Path]:
    """Ask the shell for the real Pictures folder. Users relocate it (OneDrive
    does by default), so %USERPROFILE%\\Pictures is only a guess."""
    try:
        import ctypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Pictures {33E28130-4E1E-4676-835A-98395C3BC3BB}
        folder_id = _GUID(
            0x33E28130, 0x4E1E, 0x4676,
            (ctypes.c_ubyte * 8)(0x83, 0x5A, 0x98, 0x39, 0x5C, 0x3B, 0xC3, 0xBB),
        )
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(out)
        ) != 0:
            return None
        path = Path(out.value) if out.value else None
        ctypes.windll.ole32.CoTaskMemFree(out)
        return path
    except Exception:  # noqa: BLE001 -- any failure just means "use the guess"
        return None


def _xdg_pictures_dir(home: Path) -> Optional[Path]:
    config = Path(os.getenv("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("XDG_PICTURES_DIR"):
                value = line.partition("=")[2].strip().strip('"')
                value = value.replace("$HOME", str(home))
                if value:
                    return Path(value)
    except OSError:
        pass
    return None


def default_pictures_dir() -> Path:
    home = Path.home()
    if os.name == "nt":
        return _windows_pictures_dir() or home / "Pictures"
    return _xdg_pictures_dir(home) or home / "Pictures"


def images_target_dir() -> Path:
    override = get_setting("CIVITAI_IMAGE_DIR")
    if override:
        return Path(override).expanduser()
    return default_pictures_dir() / "Civitai"


def workflow_root(user_id: str) -> Path:
    """CIVITAI_WORKFLOW_DIR if set, else the requesting user's workflow library."""
    override = get_setting("CIVITAI_WORKFLOW_DIR")
    if override:
        return Path(override).expanduser()
    return comfyui_user_dir(user_id) / "workflows" / "civitai-direct"


def workflow_metadata_root(user_id: str) -> Path:
    """Where workflow sidecars go. Kept out of the workflow folder so they
    don't show up in ComfyUI's workflow picker."""
    override = get_setting("CIVITAI_WORKFLOW_DIR")
    base = Path(override).expanduser() if override else comfyui_user_dir(user_id)
    return base / ".civitai-direct" / "workflow-metadata"


def workflow_target_dir(user_id: str, base_model: str) -> Path:
    return workflow_root(user_id) / safe_folder(base_model, "UnknownBaseModel")


def workflow_metadata_dir(user_id: str, base_model: str) -> Path:
    return workflow_metadata_root(user_id) / safe_folder(base_model, "UnknownBaseModel")
