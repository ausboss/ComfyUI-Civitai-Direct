"""Civitai Direct -- one-click Civitai downloads straight into the right
ComfyUI model folders.

Runs entirely inside ComfyUI: routes are registered on ComfyUI's own web
server and destination folders come from ComfyUI's folder_paths, so there is
no separate process, port, config file, or Python environment to set up.
Pair it with the userscripts in userscripts/ to get a download button on
civitai.com.

Routes (this package defines no graph nodes):

    GET  /civitai/health          server info + resolved model dirs
    POST /civitai/download        {"url": ...} or {"model_id": ..., "model_version_id": ...}
    POST /civitai/download-image  {"image_id": ...} and/or {"image_url": ..., "page_url": ...}
    GET  /civitai/status?id=N     state/progress of one download job
    GET  /civitai/jobs            every job this session

Settings come from the environment or a .env file next to this __init__.py
(NAME=value lines), re-read on every download so changes apply without a
ComfyUI restart:

    CIVITAI_API_KEY       API key for login-gated models (also accepted as a
                          bare civitai_api_key.txt file containing just the key)
    CIVITAI_IMAGE_DIR     where /civitai/download-image saves images.
                          Default: <your Pictures folder>/Civitai
    CIVITAI_WORKFLOW_DIR  where Workflow-type models are saved.
                          Default: ComfyUI's own workflow library
                          (user/<user>/workflows/civitai-direct/<base model>) Note: an API key does NOT unlock Early Access
versions -- those need the early-access purchase/subscription on the account.

MIT License. Sibling project (standalone server + image/workflow tools):
https://github.com/ausboss/easy-civitai-downloader
"""

import asyncio
import hashlib
import html
import itertools
import json
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import aiohttp
from aiohttp import web

import folder_paths
from server import PromptServer

NODE_VERSION = "0.1.0"
SIDECAR_SCHEMA = "ausboss-civitai-model-sidecar-v1"  # same schema as easy-civitai-downloader

routes = PromptServer.instance.routes
log = logging.getLogger("civitai-direct")

CIVITAI_MODEL_API = "https://civitai.com/api/v1/models/{model_id}"
CIVITAI_VERSION_API = "https://civitai.com/api/v1/model-versions/{version_id}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CHUNK = 1 << 20  # 1 MiB per event-loop turn keeps the server responsive
MAX_NESTED_ZIP_DEPTH = 8
MAX_NESTED_ZIP_BYTES = 512 * 1024 * 1024

# Civitai's nsfwLevel is a bitmask, not a scale: a model or version carries the
# OR of every browsing level its images fall under, so 3 is "PG and PG13" and
# 60 is "R, X, XXX and Blocked".
NSFW_LEVELS: tuple[tuple[int, str], ...] = (
    (1, "PG"), (2, "PG13"), (4, "R"), (8, "X"), (16, "XXX"), (32, "Blocked"),
)

SAVE_MARKDOWN_NOTES = True
SAVE_RAW_API_JSON = True

_NODE_DIR = Path(__file__).resolve().parent
_ENV_FILE = _NODE_DIR / ".env"
_KEY_FILE = _NODE_DIR / "civitai_api_key.txt"


def _env_file_value(path: Path, name: str) -> str:
    """Parse one NAME out of a dotenv-style file, no dependencies."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def get_setting(name: str) -> str:
    """Environment variable first, then the .env file next to this file.
    Re-read on every call so edits apply without a ComfyUI restart."""
    value = os.getenv(name, "").strip()
    if value:
        return value
    if _ENV_FILE.is_file():
        return _env_file_value(_ENV_FILE, name)
    return ""


def get_api_key_with_source() -> tuple[str, Optional[str]]:
    """(key, where-it-came-from). Files are re-read on every call so a key
    added while ComfyUI is running works on the next download."""
    key = os.getenv("CIVITAI_API_KEY", "").strip()
    if key:
        return key, "environment"
    if _ENV_FILE.is_file():
        key = _env_file_value(_ENV_FILE, "CIVITAI_API_KEY")
        if key:
            return key, ".env"
    if _KEY_FILE.is_file():
        try:
            key = _KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if key:
            return key, "civitai_api_key.txt"
    return "", None


def get_api_key() -> str:
    return get_api_key_with_source()[0]


def auth_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    key = get_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def add_token_to_url(url: str) -> str:
    """Append ?token= for Civitai hosts. The download endpoint has
    historically honored the query token more reliably than the
    Authorization header, so downloads send both."""
    key = get_api_key()
    if not key:
        return url
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return url
    if not (host == "civitai.com" or host.endswith((".civitai.com", "civitai.red"))):
        return url
    return f"{url}{'&' if '?' in url else '?'}token={quote(key)}"


# ============================================================
# Naming / classification (ported from easy-civitai-downloader)
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(value: str, fallback: str = "model") -> str:
    value = html.unescape(value or "").strip()
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.rstrip(". ")
    return value or fallback


def safe_folder(value: Optional[str], fallback: str = "Unknown") -> str:
    return safe_filename(value or fallback, fallback=fallback)


def sanitize_filename_component(value: Optional[str], fallback: str = "model", max_length: int = 120) -> str:
    value = html.unescape(value or fallback)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"[^\w.-]+", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip("_")
    value = value[:max_length].strip("_")
    return value or fallback


def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    value = re.sub(r"</p\s*>", "\n\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def decode_nsfw_level(value: Any) -> tuple[list[str], Optional[str]]:
    """(every browsing level in the bitmask, the highest one)."""
    try:
        bits = int(value)
    except (TypeError, ValueError):
        return [], None
    names = [name for bit, name in NSFW_LEVELS if bits & bit]
    return names, (names[-1] if names else None)


def parse_permission_list(value: Any) -> list[str]:
    """allowCommercialUse comes back as a JSON list on some endpoints and as a
    raw Postgres array string ("{Image,RentCivit,Rent}") on others."""
    if isinstance(value, list):
        return [str(item) for item in value]
    text = str(value or "").strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    return [part.strip().strip('"') for part in text.split(",") if part.strip()]


def format_size(size_bytes: Optional[float]) -> str:
    if not size_bytes:
        return ""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def normalize_model_type(value: Optional[str]) -> str:
    value = (value or "").strip().lower()
    compact = re.sub(r"[\s._-]+", "", value)

    if value == "lora":
        return "Lora"
    # LyCORIS variants load through ComfyUI's regular LoRA loader.
    if compact in {"lycoris", "locon", "loha", "lokr", "dora"}:
        return "Lora"
    if value == "vae":
        return "VAE"
    # "Checkpoint Merge" is still a full checkpoint.
    if compact in {"checkpoint", "checkpointmerge"}:
        return "Checkpoint"
    if value in {"textualinversion", "textual inversion", "embedding", "embeddings"}:
        return "Embedding"
    if compact in {"textencoder", "textencoders"}:
        return "TextEncoder"
    if value in {"hypernetwork", "hypernet"}:
        return "Hypernetwork"
    if value in {"controlnet", "control net"}:
        return "ControlNet"
    if value in {"upscaler", "upscalers", "esrgan"}:
        return "Upscaler"
    if value in {"poses", "pose"}:
        return "Pose"
    if value in {"wildcards", "wildcard"}:
        return "Wildcard"
    if value in {"workflow", "workflows"}:
        return "Workflow"
    return safe_folder(value or "Other", "Other")


def air_ecosystem(air: Optional[str]) -> Optional[str]:
    """Ecosystem token out of an AIR urn (urn:air:krea2:lora:civitai:1@2 ->
    "krea2") -- Civitai's own canonical, lowercase, space-free family slug."""
    if not air:
        return None
    match = re.match(r"^(?:urn:)?(?:air:)?([a-z0-9][a-z0-9.-]*):", str(air).strip().lower())
    return match.group(1) if match else None


def slugify_folder(value: str) -> str:
    """Lowercase, space-free folder slug: "Wan Video 2.2 I2V-A14B" ->
    "wan-video-22-i2v-a14b". Dots vanish first so version numbers read like
    the community's folder names (2.2 -> 22, Flux.1 -> flux1)."""
    value = html.unescape(value or "").strip().lower().replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def normalize_base_model(value: Optional[str], air: Optional[str] = None) -> str:
    """Folder slug for a Civitai base model: lowercase, no spaces, AIR-style.
    Known families map explicitly (keeping distinctions AIR drops, like
    flux1-dev vs flux1-schnell); anything else uses the version's own AIR
    ecosystem token, then a generic slug of the name."""
    value = (value or "").strip()
    compact = re.sub(r"[\s._-]+", "", value.lower())

    if "zimageturbo" in compact:
        return "zimage-turbo"
    if "zimage" in compact:
        return "zimage"
    if "flux2klein9b" in compact:
        return "flux2-klein-9b"
    # One flux1 family: Civitai's API says "Flux.1 D" / "Flux.1 S", AIR says
    # flux1 for all of them, and dev/schnell LoRAs interchange anyway.
    if "flux1" in compact or "flux.1" in value.lower():
        return "flux1"
    if "illustrious" in compact:
        return "illustrious"
    if "anima" in compact:
        return "anima"
    if "sdxl" in compact:
        return "sdxl"
    if "sd15" in compact or "sd1.5" in value.lower():
        return "sd15"
    if "pony" in compact:
        return "pony"
    if "sd35large" in compact or "sd3.5large" in value.lower():
        return "sd35-large"
    if "sd35medium" in compact or "sd3.5medium" in value.lower():
        return "sd35-medium"
    if "sd3" in compact:
        return "sd3"
    if "sd21" in compact or "sd2.1" in value.lower():
        return "sd21"
    if "sd14" in compact or "sd1.4" in value.lower():
        return "sd14"

    ecosystem = air_ecosystem(air)
    if ecosystem and ecosystem != "other":
        return ecosystem
    return slugify_folder(value) or "unknown"


# SD-family checkpoints load through CheckpointLoaderSimple and belong in
# checkpoints/; newer diffusion models (Flux, Wan, LTX, ...) load through
# "load diffusion model" and belong in diffusion_models/.
_STABLE_DIFFUSION_BASES = {"base-model", "illustrious", "anima", "sdxl", "sd15", "pony"}

# Civitai model type -> folder_paths folder name
_TYPE_TO_FOLDER_NAME: dict[str, str] = {
    "Lora": "loras",
    "VAE": "vae",
    "Embedding": "embeddings",
    "ControlNet": "controlnet",
    "Hypernetwork": "hypernetworks",
    "Upscaler": "upscale_models",
    "TextEncoder": "text_encoders",
}

# Types folder_paths has no entry for; they live under models/<dirname>/.
_TYPE_FALLBACK_DIRNAME: dict[str, str] = {
    "Wildcard": "wildcards",
    "Pose": "poses",
}


def resolve_type_dir(folder_name: str) -> Path:
    """Destination dir for a folder_paths name, honoring extra_model_paths.yaml.

    Prefer the registered path whose basename matches the folder name --
    ComfyUI lists legacy dirs first for some types (diffusion_models ->
    [models/unet, models/diffusion_models]), and new downloads belong in the
    modern folder. Fall back to the first registered path, then models_dir.
    """
    try:
        paths = folder_paths.get_folder_paths(folder_name)
    except KeyError:
        paths = []
    if paths:
        for p in paths:
            if Path(p).name.lower() == folder_name.lower():
                return Path(p)
        return Path(paths[0])
    return Path(folder_paths.models_dir) / folder_name


def choose_target_dir(model_type: str, base_model: str, name_haystack: str) -> Path:
    if model_type == "Checkpoint":
        is_aio = "AIO" in name_haystack.upper()
        use_sd = is_aio or base_model in _STABLE_DIFFUSION_BASES
        folder_name = "checkpoints" if use_sd else "diffusion_models"
        return resolve_type_dir(folder_name) / base_model

    folder_name = _TYPE_TO_FOLDER_NAME.get(model_type)
    if folder_name:
        return resolve_type_dir(folder_name) / base_model

    dirname = _TYPE_FALLBACK_DIRNAME.get(model_type, safe_folder(model_type, "Other"))
    return Path(folder_paths.models_dir) / dirname / base_model


def is_wan_model(meta: dict[str, Any]) -> bool:
    base_model = (meta.get("base_model") or "").strip().lower()
    compact_base = re.sub(r"[\s._-]+", "", base_model)
    if base_model.startswith("wan") or compact_base.startswith("wan"):
        return True

    haystack = " ".join(
        value
        for value in [meta.get("model_slug"), meta.get("model_name"), meta.get("version_name")]
        if value
    ).lower()
    compact = re.sub(r"[\s._-]+", "", haystack)
    return bool(
        re.search(r"(^|[^a-z0-9])wan([^a-z0-9]|$)", haystack)
        or compact.startswith("wan")
        or re.search(r"wan(?:video|i2v|t2v|2\d|1\d)", compact)
    )


def slug_aware_filename(meta: dict[str, Any]) -> str:
    slug = meta.get("model_slug") or (str(meta["model_id"]) if meta.get("model_id") else "model")
    slug = sanitize_filename_component(slug, "model")
    version = sanitize_filename_component(meta.get("version_name") or "", "")
    base = f"{slug}__{version}" if version else slug
    suffix = f"-v{meta['model_version_id']}" if meta.get("model_version_id") else ""
    return f"{base}{suffix}.safetensors"


def unique_destination_path(path: Path) -> Path:
    candidate = path
    index = 1
    while candidate.exists():
        candidate = path.parent / f"{path.stem} ({index}){path.suffix}"
        index += 1
    return candidate


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
    """Ask the shell for the real Pictures folder -- users relocate it
    (OneDrive does this by default), so %USERPROFILE%\\Pictures is a guess."""
    try:
        import ctypes
        from ctypes import wintypes

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
    except Exception:
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


def workflow_target_dir(user_id: str, base_model: str) -> Path:
    """Workflow destination: CIVITAI_WORKFLOW_DIR if set, else the requesting
    user's ComfyUI workflow library."""
    override = get_setting("CIVITAI_WORKFLOW_DIR")
    base = (
        Path(override).expanduser()
        if override
        else comfyui_user_dir(user_id) / "workflows" / "civitai-direct"
    )
    return base / safe_folder(base_model, "UnknownBaseModel")


def workflow_metadata_dir(user_id: str, base_model: str) -> Path:
    override = get_setting("CIVITAI_WORKFLOW_DIR")
    base = (
        Path(override).expanduser()
        if override
        else comfyui_user_dir(user_id)
    )
    return (
        base
        / ".civitai-direct"
        / "workflow-metadata"
        / safe_folder(base_model, "UnknownBaseModel")
    )


def workflow_artifact_path(download_path: Path) -> Path:
    if download_path.suffix.lower() == ".zip":
        return download_path.parent / safe_folder(download_path.stem, "workflow")
    return download_path


def workflow_sidecar_json_path(metadata_dir: Path, artifact_path: Path) -> Path:
    stem = artifact_path.stem if artifact_path.suffix else artifact_path.name
    return metadata_dir / f"{safe_filename(stem, 'workflow')}.civitai.json"


def workflow_sidecar_markdown_path(metadata_dir: Path, artifact_path: Path) -> Path:
    stem = artifact_path.stem if artifact_path.suffix else artifact_path.name
    return metadata_dir / f"{safe_filename(stem, 'workflow')}.civitai.md"


def unique_workflow_destination(download_path: Path) -> tuple[Path, Path]:
    candidate = download_path
    index = 1
    while True:
        artifact = workflow_artifact_path(candidate)
        part = candidate.with_suffix(candidate.suffix + ".part")
        artifact_part = artifact.with_name(artifact.name + ".part")
        if (
            not candidate.exists()
            and not artifact.exists()
            and not part.exists()
            and not artifact_part.exists()
        ):
            return candidate, artifact
        candidate = download_path.with_name(
            f"{download_path.stem} ({index}){download_path.suffix}"
        )
        index += 1


def _safe_archive_members(
    archive: zipfile.ZipFile,
    extract_dir: Path,
) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    resolved_extract_dir = extract_dir.resolve()
    for member in members:
        member_path = (extract_dir / member.filename).resolve()
        if member_path != resolved_extract_dir and resolved_extract_dir not in member_path.parents:
            raise RuntimeError(f"unsafe ZIP entry path: {member.filename}")
    return members


def _hoist_single_subdirectory(extract_dir: Path) -> None:
    children = list(extract_dir.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        return
    inner_dir = children[0]
    for item in inner_dir.iterdir():
        shutil.move(str(item), str(extract_dir / item.name))
    inner_dir.rmdir()


def _extract_workflow_zip(zip_path: Path, extract_dir: Path, depth: int = 0) -> None:
    if depth > MAX_NESTED_ZIP_DEPTH:
        raise RuntimeError("nested ZIP depth limit exceeded")

    with zipfile.ZipFile(zip_path) as archive:
        _safe_archive_members(archive, extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        archive.extractall(extract_dir)

    nested_zips = sorted(
        (
            path
            for path in extract_dir.rglob("*")
            if path.is_file() and path.suffix.lower() == ".zip"
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for nested_zip in nested_zips:
        if nested_zip.stat().st_size > MAX_NESTED_ZIP_BYTES:
            raise RuntimeError(f"nested ZIP is too large to extract safely: {nested_zip.name}")
        nested_extract_dir = nested_zip.parent / safe_folder(nested_zip.stem, "workflow")
        _extract_workflow_zip(nested_zip, nested_extract_dir, depth + 1)
        nested_zip.unlink()
        _hoist_single_subdirectory(nested_extract_dir)

    _hoist_single_subdirectory(extract_dir)


# ============================================================
# URL parsing
# ============================================================

def _query_int(url: str, key: str) -> Optional[int]:
    match = re.search(rf"[?&]{re.escape(key)}=(\d+)", url or "")
    return int(match.group(1)) if match else None


def parse_ids_from_url(url: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Return (model_id, model_version_id, model_slug) parsed from any Civitai URL."""
    url = url or ""
    version_id = _query_int(url, "modelVersionId")
    if version_id is None:
        match = re.search(r"/api/download/models/(\d+)", url)
        version_id = int(match.group(1)) if match else None

    model_id = None
    slug = None
    # The number in /api/download/models/N is a VERSION id, not a model id --
    # only page-style /models/N URLs carry the model id.
    match = re.search(r"(?<!/api/download)/models/(\d+)(?:/([^/?#]+))?", url)
    if match:
        model_id = int(match.group(1))
        slug = match.group(2)
    return model_id, version_id, slug


# ============================================================
# Civitai API
# ============================================================

def pick_strongest_hash(hashes: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    for key in ("SHA256", "sha256", "AutoV3", "AutoV2", "BLAKE3", "CRC32"):
        value = hashes.get(key)
        if value:
            return key, str(value)
    return None, None


def pick_primary_model_file(version_json: dict[str, Any]) -> Optional[dict[str, Any]]:
    files = version_json.get("files") or []
    primary = next((f for f in files if f.get("primary")), None)
    return primary or (files[0] if files else None)


def build_classification(
    model_json: dict[str, Any],
    version_json: dict[str, Any],
    file: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Sort/filter/identify signals, flattened out of the two API payloads.

    The point is that a librarian script can decide where a file belongs --
    or that it should never have been downloaded -- without re-querying
    Civitai or digging through api_raw.
    """
    file = file or {}
    file_meta = file.get("metadata") or {}

    version_levels, version_top = decode_nsfw_level(version_json.get("nsfwLevel"))
    model_levels, model_top = decode_nsfw_level(model_json.get("nsfwLevel"))

    return {
        # The uploader's own NSFW checkbox. Unreliable on its own -- plenty of
        # models sit at nsfw=false with XXX images -- so sort on the levels.
        "nsfw": model_json.get("nsfw"),
        # Rates THIS version, i.e. the file being saved.
        "nsfw_level": version_json.get("nsfwLevel"),
        "nsfw_levels": version_levels,
        "nsfw_level_top": version_top,
        # Union across every version of the model, so it reads higher than the
        # downloaded file often deserves. Useful as a "what is this creator
        # posting" signal, not as the file's own rating.
        "model_nsfw_level": model_json.get("nsfwLevel"),
        "model_nsfw_levels": model_levels,
        "model_nsfw_level_top": model_top,
        "sfw_only": model_json.get("sfwOnly"),
        "minor": model_json.get("minor"),
        "poi": model_json.get("poi"),  # trained on a real, identifiable person
        "availability": version_json.get("availability") or model_json.get("availability"),
        "status": version_json.get("status"),
        "supports_generation": model_json.get("supportsGeneration"),
        "usage_control": version_json.get("usageControl"),
        "upload_type": version_json.get("uploadType"),
        "base_model_type": version_json.get("baseModelType"),
        "model_base_models": model_json.get("baseModels") or [],
        "version_index": version_json.get("index"),  # 0 is the newest version
        "version_stats": version_json.get("stats") or {},
        "dates": {
            "created": version_json.get("createdAt"),
            "published": version_json.get("publishedAt"),
            "updated": version_json.get("updatedAt"),
        },
        "license": {
            "allow_no_credit": model_json.get("allowNoCredit"),
            "allow_commercial_use": parse_permission_list(model_json.get("allowCommercialUse")),
            "allow_derivatives": model_json.get("allowDerivatives"),
            "allow_different_license": model_json.get("allowDifferentLicense"),
        },
        "file": {
            "name": file.get("name"),
            "size_kb": file.get("sizeKB"),
            "format": file_meta.get("format"),      # SafeTensor / PickleTensor
            "precision": file_meta.get("fp"),       # fp16 / bf16 / fp32
            "size_type": file_meta.get("size"),     # full / pruned
            "pickle_scan": file.get("pickleScanResult"),
            "virus_scan": file.get("virusScanResult"),
            "scanned_at": file.get("scannedAt"),
        },
    }


async def probe_gate_reason(session: aiohttp.ClientSession, url: str) -> str:
    """Ask the download endpoint WHY it refused, as JSON.

    Sent a browser-like User-Agent, Civitai answers a gated download with a
    redirect to an HTML page; sent a plain client UA it explains itself:
    403 {"error": "Early Access", "deadline": ...}. Returns '' on anything
    unexpected -- this only ever decorates an error we already have."""
    try:
        async with session.get(
            url,
            headers={"User-Agent": "ComfyUI-Civitai-Direct", "Accept": "application/json"},
            allow_redirects=False,
        ) as resp:
            if resp.status in (401, 403):
                return (await resp.text())[:2000]
    except Exception:  # noqa: BLE001 -- best-effort diagnostics
        pass
    return ""


async def fetch_json(session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    async with session.get(url) as resp:
        if resp.status in (401, 403):
            raise RuntimeError(
                f"Civitai returned {resp.status} for {url} -- the model may be gated; "
                "set an API key (CIVITAI_API_KEY or civitai_api_key.txt)."
            )
        resp.raise_for_status()
        return await resp.json()


async def resolve_model(
    session: aiohttp.ClientSession,
    model_id: Optional[int],
    version_id: Optional[int],
    slug: Optional[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return (model_json, version_json, meta) from whatever ids we have."""
    detail_json: Optional[dict[str, Any]] = None
    if model_id is None and version_id is not None:
        detail_json = await fetch_json(session, CIVITAI_VERSION_API.format(version_id=version_id))
        model_id = detail_json.get("modelId")
        if model_id is None:
            raise RuntimeError(f"model-versions/{version_id} has no modelId")

    if model_id is None:
        raise RuntimeError("could not determine a model id")

    model_json = await fetch_json(session, CIVITAI_MODEL_API.format(model_id=model_id))
    versions = model_json.get("modelVersions") or []
    if not versions:
        raise RuntimeError(f"model {model_id} has no versions")

    if version_id is not None:
        version_json = next((v for v in versions if v.get("id") == version_id), None)
        if version_json is None:
            raise RuntimeError(f"version {version_id} not found on model {model_id}")
    else:
        version_json = versions[0]

    # The model endpoint's embedded version objects drop fields only the
    # dedicated version endpoint carries (air, usageControl, uploadType,
    # updatedAt, trainingDetails), so fetch that too. The embedded copy wins
    # every shared key -- availability, index and covered exist nowhere else,
    # and availability is what drives the Early Access error message.
    if detail_json is None or detail_json.get("id") != version_json.get("id"):
        detail_json = None
        try:
            detail_json = await fetch_json(
                session, CIVITAI_VERSION_API.format(version_id=version_json.get("id"))
            )
        except Exception as exc:  # noqa: BLE001 -- extra detail, never a blocker
            log.info("civitai-direct: version %s detail unavailable (%s)",
                     version_json.get("id"), console_safe(redact_civitai_token(exc)))
    if detail_json:
        version_json = {**detail_json, **version_json}

    trained_words = version_json.get("trainedWords") or []
    description_html = model_json.get("description")
    file = pick_primary_model_file(version_json)
    hash_type, hash_value = pick_strongest_hash((file or {}).get("hashes") or {})

    page_url = f"https://civitai.com/models/{model_id}"
    if slug:
        page_url = f"{page_url}/{slug}"

    meta = {
        "page_url": page_url,
        "source_domain": "civitai.com",
        "model_id": model_id,
        "model_version_id": version_json.get("id"),
        "model_name": model_json.get("name"),
        "model_slug": slug,
        "version_name": version_json.get("name"),
        "creator": (model_json.get("creator") or {}).get("username"),
        "type": model_json.get("type"),
        "base_model": normalize_base_model(version_json.get("baseModel"), version_json.get("air")),
        "published": version_json.get("publishedAt"),
        "hash_type": hash_type,
        "hash_value": hash_value,
        "air": version_json.get("air"),
        "trigger_words": trained_words,
        "trained_words": trained_words,
        "tags": model_json.get("tags") or [],
        "description_html": description_html,
        "description_text": strip_html(description_html or ""),
        "stats": model_json.get("stats") or {},
        "api_hashes": (file or {}).get("hashes") or {},
        "classification": build_classification(model_json, version_json, file),
    }
    return model_json, version_json, meta


# ============================================================
# Sidecars (schema-compatible with easy-civitai-downloader)
# ============================================================

def sidecar_mode() -> str:
    """Sidecar style for model downloads, from CIVITAI_SIDECARS (env/.env):
    "info" (default) writes the ecosystem-standard <stem>.civitai.info that
    Civitai Helper, ComfyUI-Lora-Manager & co read; "full" additionally writes
    this tool's own <file>.json + <file>.md notes."""
    value = get_setting("CIVITAI_SIDECARS").strip().lower()
    return value if value in {"info", "full"} else "info"


def civitai_info_path(model_path: Path) -> Path:
    """<stem>.civitai.info next to the model file (model.safetensors ->
    model.civitai.info) -- the sidecar name the wider ecosystem shares."""
    return model_path.with_suffix(".civitai.info")


def build_civitai_info(
    model_json: dict[str, Any], version_json: dict[str, Any], meta: dict[str, Any]
) -> dict[str, Any]:
    """The .civitai.info payload: the model-version API response verbatim,
    with the keys the model endpoint's embedded copy can drop backfilled so
    the file matches what /api/v1/model-versions/{id} itself serves."""
    info = dict(version_json)
    info.setdefault("modelId", meta.get("model_id"))
    info.setdefault("model", {
        "name": model_json.get("name"),
        "type": model_json.get("type"),
        "nsfw": model_json.get("nsfw"),
        "poi": model_json.get("poi"),
    })
    return info


def previews_mode() -> str:
    """Preview-image style, from CIVITAI_PREVIEWS (env/.env): "sfw" (default)
    saves the tamest image and skips models with nothing PG/PG13, "any"
    trusts Civitai's cover choice, "off" disables preview downloads."""
    value = get_setting("CIVITAI_PREVIEWS").strip().lower()
    return value if value in {"sfw", "any", "off"} else "sfw"


def pick_preview_image(version_json: dict[str, Any]) -> Optional[dict[str, Any]]:
    if previews_mode() == "off":
        return None
    images = [
        entry for entry in (version_json.get("images") or [])
        if isinstance(entry, dict) and (entry.get("type") or "image") == "image" and entry.get("url")
    ]
    if not images:
        return None
    if previews_mode() == "any":
        return images[0]
    # Per-image nsfwLevel is a single browsing level: 1 PG, 2 PG13, 4 R, ...
    tamest = min(images, key=lambda entry: entry.get("nsfwLevel") or 99)
    return tamest if (tamest.get("nsfwLevel") or 99) <= 2 else None


def preview_path_for(model_path: Path, image_url: str) -> Path:
    """<stem>.preview.<ext> -- the writer convention A1111/Forge, Civitai
    Helper and Lora Manager all read, leaving <stem>.<ext> free for a
    preview the user makes themselves (which those tools prefer)."""
    ext = Path(urlparse(image_url).path).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        ext = ".jpeg"
    return model_path.with_suffix(".preview" + ext)


async def save_preview_image(
    session: aiohttp.ClientSession,
    version_json: dict[str, Any],
    model_path: Path,
    job_id: int,
) -> bool:
    """Best-effort preview download next to the model. Never fails the job."""
    entry = pick_preview_image(version_json)
    if not entry:
        return False
    dest = preview_path_for(model_path, entry["url"])
    if dest.exists():
        return True
    part = dest.parent / (dest.name + ".part")
    try:
        async with session.get(entry["url"]) as resp:
            resp.raise_for_status()
            with part.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    fh.write(chunk)
        part.replace(dest)
        return True
    except Exception as exc:  # noqa: BLE001 -- previews are decoration, not data
        log.info("civitai-direct: job %s: preview image failed (%s)",
                 job_id, console_safe(redact_civitai_token(exc)))
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def redact_civitai_token(text: Any) -> str:
    return re.sub(r"([?&]token=)[^&\s]+", r"\1REDACTED", str(text or ""))


def console_safe(value: Any) -> str:
    """ASCII-only rendering for console output. Civitai model names routinely
    carry CJK/emoji that crash a cp1252 Windows console when printed raw;
    this keeps ASCII text readable and escapes the rest."""
    return str(value if value is not None else "").encode("ascii", "backslashreplace").decode("ascii")


def build_sidecar_json(
    meta: dict[str, Any],
    api_raw: dict[str, Any],
    save_path: Path,
    download_url: str,
    file_sha256: Optional[str],
    file_size: Optional[int],
    status: str,
    json_file_path: Optional[Path] = None,
    markdown_file_path: Optional[Path] = None,
) -> dict[str, Any]:
    json_file_path = json_file_path or save_path.with_suffix(save_path.suffix + ".json")
    markdown_file_path = markdown_file_path or save_path.with_suffix(save_path.suffix + ".md")

    return {
        "schema": SIDECAR_SCHEMA,
        "status": status,
        "captured_at": now_iso(),
        "downloaded_at": now_iso() if status in {"downloaded", "already_exists"} else None,
        "local": {
            "model_file": str(save_path),
            "model_file_name": save_path.name,
            "json_file": str(json_file_path),
            "markdown_file": str(markdown_file_path),
            "size_bytes": file_size,
            "sha256": file_sha256,
        },
        "preview": None,
        "source": {
            "page_url": meta.get("page_url"),
            "download_url": redact_civitai_token(download_url),
            "source_domain": meta.get("source_domain"),
        },
        "model": {
            "id": meta.get("model_id"),
            "version_id": meta.get("model_version_id"),
            "name": meta.get("model_name"),
            "slug": meta.get("model_slug"),
            "version_name": meta.get("version_name"),
            "creator": meta.get("creator"),
            "type": meta.get("type"),
            "base_model": meta.get("base_model"),
            "published": meta.get("published"),
            "air": meta.get("air"),
        },
        "usage": {
            "trigger_words": meta.get("trigger_words") or [],
            "trained_words": meta.get("trained_words") or [],
            "suggested_tags": [],
            "suggested_strength": None,
            "suggested_usage": None,
            "notes": None,
            "description_text": meta.get("description_text"),
            "description_html": meta.get("description_html"),
        },
        "classification": {
            "tags": meta.get("tags") or [],
            "stats": meta.get("stats") or {},
            # Additive only: tags and stats keep their names and position, the
            # rest of the sort/filter signals merge in behind them.
            **(meta.get("classification") or {}),
        },
        "hashes": {
            "page_hash_type": meta.get("hash_type"),
            "page_hash_value": meta.get("hash_value"),
            "downloaded_sha256": file_sha256,
            # AutoV2 is what A1111 shows, AutoV3 what Civitai's UI shows --
            # both let other tools match this file without rehashing it.
            "api_hashes": meta.get("api_hashes") or {},
        },
        "api_raw": api_raw if SAVE_RAW_API_JSON else None,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def classification_lines(payload: dict[str, Any]) -> list[str]:
    """The '## Classification' block: stable `- **Key:** value` bullets, so the
    note greps as easily as the JSON sidecar parses."""
    cls = payload.get("classification") or {}
    lic = cls.get("license") or {}
    fil = cls.get("file") or {}
    stats = cls.get("stats") or {}
    version_stats = cls.get("version_stats") or {}
    tags = cls.get("tags") or []
    no_credit = lic.get("allow_no_credit")

    def yes_no(value: Any) -> str:
        return "" if value is None else ("Yes" if value else "No")

    def rating(level: Any, names: list[str]) -> str:
        text = ", ".join(names) or "unrated"
        return f"{text} (level {level})" if level is not None else text

    file_format = " / ".join(
        filter(None, [fil.get("format"), fil.get("precision"), fil.get("size_type")])
    )

    return [
        "",
        "## Classification",
        "",
        f"- **Rating:** {rating(cls.get('nsfw_level'), cls.get('nsfw_levels') or [])}",
        f"- **Rating across all versions:** "
        f"{rating(cls.get('model_nsfw_level'), cls.get('model_nsfw_levels') or [])}",
        f"- **NSFW flag:** {yes_no(cls.get('nsfw'))}",
        f"- **Real person (PoI):** {yes_no(cls.get('poi'))}",
        f"- **Depicts a minor:** {yes_no(cls.get('minor'))}",
        f"- **SFW only:** {yes_no(cls.get('sfw_only'))}",
        f"- **Tags:** {', '.join(f'`{tag}`' for tag in tags) or 'None captured'}",
        f"- **Availability:** {cls.get('availability') or ''}",
        f"- **Usage control:** {cls.get('usage_control') or ''}",
        f"- **Base model type:** {cls.get('base_model_type') or ''}",
        f"- **Upload type:** {cls.get('upload_type') or ''}",
        f"- **Format:** {file_format}",
        f"- **API file name:** `{fil.get('name') or ''}`",
        f"- **Size:** {format_size((fil.get('size_kb') or 0) * 1024)}",
        f"- **Scans:** pickle {fil.get('pickle_scan') or '?'}, virus {fil.get('virus_scan') or '?'}",
        f"- **Downloads:** {stats.get('downloadCount', '')} "
        f"(this version: {version_stats.get('downloadCount', '')})",
        f"- **Thumbs:** {stats.get('thumbsUpCount', '')} up / {stats.get('thumbsDownCount', '')} down",
        f"- **Commercial use:** {', '.join(lic.get('allow_commercial_use') or []) or 'None'}",
        f"- **Derivatives allowed:** {yes_no(lic.get('allow_derivatives'))}",
        f"- **Attribution required:** {'' if no_credit is None else yes_no(not no_credit)}",
    ]


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    model = payload["model"]
    usage = payload["usage"]
    local = payload["local"]
    source = payload["source"]
    hashes = payload["hashes"]

    trigger_words = usage.get("trigger_words") or []

    lines = [
        f"# {model.get('name') or 'Civitai Model'}",
        "",
        f"- **Version:** {model.get('version_name') or ''}",
        f"- **Type:** {model.get('type') or ''}",
        f"- **Base model:** {model.get('base_model') or ''}",
        f"- **Creator:** {model.get('creator') or ''}",
        f"- **Published:** {model.get('published') or ''}",
        f"- **Civitai model ID:** {model.get('id') or ''}",
        f"- **Civitai version ID:** {model.get('version_id') or ''}",
        f"- **AIR:** `{model.get('air') or ''}`",
        f"- **File:** `{local.get('model_file') or ''}`",
        f"- **SHA256:** `{hashes.get('downloaded_sha256') or ''}`",
        "",
        "## Trigger words",
        "",
    ]
    if trigger_words:
        lines.extend(f"- `{word}`" for word in trigger_words)
    else:
        lines.append("- None captured")

    lines.extend(classification_lines(payload))

    lines.extend([
        "",
        "## Description",
        "",
        usage.get("description_text") or "No description captured.",
        "",
        "## Source",
        "",
        f"- Page: {source.get('page_url') or ''}",
        f"- Download: {source.get('download_url') or ''}",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


# Extensions a .civitai.info sidecar's model file can carry, most likely first.
_MODEL_FILE_SUFFIXES = (
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".zip",
)

# Sidecar scans cover whole type folders, which can hold arbitrary JSON from
# other tools -- don't slurp anything that clearly isn't a sidecar.
_MAX_SIDECAR_BYTES = 8 * 1024 * 1024


def matching_downloaded_version_sidecar(target_dir: Path, meta: dict[str, Any]) -> Optional[Path]:
    """Existing model file for this exact version, found via its sidecar.

    Reads both sidecar dialects: .civitai.info (ours and any other tool's --
    a file fetched by Civitai Helper or Lora Manager counts as already
    downloaded) and this tool's <file>.json records."""
    if not meta.get("model_version_id") or not target_dir.exists():
        return None

    requested_version = str(meta["model_version_id"])
    requested_model = str(meta["model_id"]) if meta.get("model_id") else None

    for info_path in target_dir.rglob("*.civitai.info"):
        try:
            if info_path.stat().st_size > _MAX_SIDECAR_BYTES:
                continue
            payload = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("id")) != requested_version:
            continue
        info_model_id = payload.get("modelId")
        if requested_model and info_model_id and str(info_model_id) != requested_model:
            continue
        # The model file shares the sidecar's stem; probe extensions instead
        # of globbing -- model names can contain glob metacharacters.
        stem = info_path.name[: -len(".civitai.info")]
        for suffix in _MODEL_FILE_SUFFIXES:
            candidate = info_path.parent / (stem + suffix)
            if candidate.exists():
                return candidate

    for sidecar_path in target_dir.rglob("*.json"):
        if sidecar_path.name.endswith((".failed.json", ".failed.civitai.json")):
            continue
        try:
            if sidecar_path.stat().st_size > _MAX_SIDECAR_BYTES:
                continue
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("status") == "failed":
            continue

        model = payload.get("model") or {}
        if str(model.get("version_id")) != requested_version:
            continue
        model_id = model.get("id")
        if requested_model and model_id and str(model_id) != requested_model:
            continue

        local = payload.get("local") or {}
        local_model_file = local.get("model_file")
        if local_model_file and Path(str(local_model_file)).exists():
            return Path(str(local_model_file))

        # The file may have been moved (with its sidecars) to another folder,
        # leaving the recorded absolute path stale -- trust a copy sitting
        # next to the sidecar that recorded it.
        moved_name = local.get("model_file_name") or (
            Path(str(local_model_file)).name if local_model_file else None
        )
        if moved_name:
            moved = sidecar_path.parent / str(moved_name)
            if moved.exists():
                return moved
    return None


def find_same_named_files(root: Path, filename: str, limit: int = 5) -> list[Path]:
    """Files named `filename` anywhere under root, subfolders included, so a
    model the user reorganized into a subfolder is still recognized. Bounded
    because every match may get hashed."""
    if not filename or not root.is_dir():
        return []
    matches: list[Path] = []
    try:
        for path in root.rglob(filename):
            if path.is_file():
                matches.append(path)
                if len(matches) >= limit:
                    break
    except OSError:
        pass
    return matches


async def calculate_sha256(path: Path) -> str:
    def _hash() -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    return await asyncio.to_thread(_hash)


# ============================================================
# Images (ported from easy-civitai-downloader; sidecar schema shared)
# ============================================================

IMAGE_SIDECAR_SCHEMA = "ausboss-civitai-image-sidecar-v1"
CIVITAI_IMAGE_API = "https://civitai.com/api/v1/images"
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}


def extension_from_url(url: str, fallback: str = ".jpg") -> str:
    try:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in _IMAGE_EXTENSIONS:
            return suffix
    except Exception:
        pass
    return fallback


def original_image_url(url: str) -> str:
    """Rewrite an image.civitai.com delivery URL to fetch the original upload.

    Civitai serves width-optimized renders (.../width=1152/name.jpeg).
    Replacing the transform segment with original=true returns the unmodified
    file, which for PNGs keeps embedded generation/ComfyUI metadata.
    """
    try:
        parsed = urlparse(url)
        if "image.civitai.com" not in parsed.netloc:
            return url

        parts = parsed.path.split("/")
        if len(parts) >= 2 and "=" in parts[-2]:
            parts[-2] = "original=true"
        elif len(parts) >= 2:
            parts.insert(-1, "original=true")
        else:
            return url

        return parsed._replace(path="/".join(parts)).geturl()
    except Exception:
        return url


def extension_from_content_type(content_type: str, fallback: str = ".jpg") -> str:
    content_type = (content_type or "").lower()
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    if "avif" in content_type:
        return ".avif"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    return fallback


def extract_png_text_chunks(path: Path) -> dict[str, str]:
    """Read tEXt/zTXt/iTXt chunks from a PNG (ComfyUI stores its prompt and
    workflow there; A1111 stores 'parameters'). Returns {keyword: text}."""
    import struct
    import zlib

    result: dict[str, str] = {}
    try:
        with path.open("rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return {}

            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                length, chunk_type = struct.unpack(">I4s", header)
                if chunk_type in {b"tEXt", b"zTXt", b"iTXt"} and length < 64 * 1024 * 1024:
                    data = f.read(length)
                    f.seek(4, 1)  # skip CRC
                    try:
                        if chunk_type == b"tEXt":
                            key, _, value = data.partition(b"\x00")
                            result[key.decode("latin-1")] = value.decode("latin-1")
                        elif chunk_type == b"zTXt":
                            key, _, rest = data.partition(b"\x00")
                            result[key.decode("latin-1")] = zlib.decompress(rest[1:]).decode("latin-1")
                        else:  # iTXt
                            key, _, rest = data.partition(b"\x00")
                            compressed = rest[0:1] == b"\x01"
                            rest = rest[2:]
                            _, _, rest = rest.partition(b"\x00")  # language tag
                            _, _, text = rest.partition(b"\x00")  # translated keyword
                            if compressed:
                                text = zlib.decompress(text)
                            result[key.decode("latin-1")] = text.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                else:
                    f.seek(length + 4, 1)
                if chunk_type == b"IEND":
                    break
    except Exception as exc:
        log.warning("civitai-direct: failed to read PNG text chunks from %s: %s",
                    console_safe(path.name), console_safe(exc))

    return result


def _parse_json_maybe(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def image_generation_meta(data: dict[str, Any]) -> dict[str, Any]:
    gen = data.get("generation_data") or {}
    meta = gen.get("meta") if isinstance(gen, dict) else None
    return meta if isinstance(meta, dict) else {}


def comfy_workflow_from_request(data: dict[str, Any]) -> Any:
    """ComfyUI graph from the userscript's generation data, if present.

    Civitai stores it as {"prompt": ..., "workflow": ...}; the workflow half
    is what ComfyUI loads/pastes. Returns a dict/list or None."""
    comfy = image_generation_meta(data).get("comfy")
    if isinstance(comfy, str):
        comfy = _parse_json_maybe(comfy)
    if not isinstance(comfy, dict):
        return None

    workflow = comfy.get("workflow")
    if isinstance(workflow, (dict, list)) and workflow:
        return workflow
    return comfy


def build_image_sidecar(
    data: dict[str, Any],
    image_id: Optional[int],
    image_url: str,
    prompt: Optional[str],
    api_image: Optional[dict[str, Any]],
    save_path: Path,
    embedded: dict[str, str],
) -> dict[str, Any]:
    api_meta = (api_image or {}).get("meta") or {}
    client_data = data.get("generation_data") if isinstance(data.get("generation_data"), dict) else {}
    client_meta = image_generation_meta(data)

    # The userscript's data comes from the frontend's generation-data
    # endpoint and is usually richer than the public API; let it win.
    meta = {**api_meta, **{k: v for k, v in client_meta.items() if v is not None}}

    client_resources = client_data.get("resources")

    return {
        "schema": IMAGE_SIDECAR_SCHEMA,
        "captured_at": now_iso(),
        "image": {
            "id": image_id,
            "page_url": data.get("page_url"),
            "source_url": image_url,
            "width": (api_image or {}).get("width"),
            "height": (api_image or {}).get("height"),
            "base_model": (api_image or {}).get("baseModel"),
            "model_version_ids": (api_image or {}).get("modelVersionIds"),
            "username": (api_image or {}).get("username"),
        },
        "generation": {
            "prompt": prompt or meta.get("prompt"),
            "negative_prompt": meta.get("negativePrompt"),
            "cfg_scale": meta.get("cfgScale"),
            "steps": meta.get("steps"),
            "sampler": meta.get("sampler"),
            "scheduler": meta.get("scheduler"),
            "seed": meta.get("seed"),
            "clip_skip": meta.get("clipSkip"),
            "size": meta.get("Size"),
            "model": meta.get("Model"),
            "process": client_data.get("process"),
            "on_site": client_data.get("onSite"),
            # The frontend resources carry model/version names; the meta
            # variants only carry ids.
            "resources": client_resources or meta.get("resources") or meta.get("civitaiResources"),
        },
        "embedded_metadata": {
            # A1111-style generation parameters, if present in the file
            "parameters": embedded.get("parameters"),
            # ComfyUI prompt graph / workflow, if present in the file
            "comfyui_prompt": _parse_json_maybe(embedded.get("prompt")),
            "has_comfyui_workflow": bool(embedded.get("workflow")),
            "has_civitai_comfy_workflow": comfy_workflow_from_request(data) is not None,
        },
        "local": {
            "image_file": str(save_path),
            "image_file_name": save_path.name,
        },
        "api_meta_raw": api_meta if SAVE_RAW_API_JSON else None,
        "generation_data_raw": (client_data or None) if SAVE_RAW_API_JSON else None,
    }


def image_filename(data: dict[str, Any], image_url: str, image_id: Optional[int]) -> str:
    requested = data.get("filename")
    if requested:
        return safe_filename(str(requested))

    stem = f"civitai-image-{image_id}" if image_id else "civitai-image"
    return f"{stem}{extension_from_url(image_url)}"


def unique_image_destination_path(path: Path, write_prompt: bool) -> Path:
    candidate = path
    index = 1

    while True:
        temp_path = candidate.with_suffix(candidate.suffix + ".part")
        prompt_path = candidate.with_suffix(".txt")
        sidecar_path = candidate.with_suffix(".json")
        prompt_clear = not write_prompt or not prompt_path.exists()

        if not candidate.exists() and not temp_path.exists() and prompt_clear and not sidecar_path.exists():
            return candidate

        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        index += 1


def parse_image_id_from_url(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    try:
        match = re.search(r"/images/(\d+)", urlparse(url).path)
        return int(match.group(1)) if match else None
    except Exception:
        return None


def existing_image_for_id(images_dir: Path, image_id: Optional[int]) -> Optional[Path]:
    """An already-downloaded copy of this exact image, by its default stem."""
    if not image_id or not images_dir.is_dir():
        return None
    for candidate in images_dir.glob(f"civitai-image-{image_id}.*"):
        if candidate.suffix.lower() in _IMAGE_EXTENSIONS:
            return candidate
    return None


async def fetch_civitai_image(
    session: aiohttp.ClientSession, image_id: int
) -> Optional[dict[str, Any]]:
    try:
        async with session.get(
            CIVITAI_IMAGE_API, params={"imageId": str(image_id)}
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001 -- enrichment only, never a blocker
        log.info("civitai-direct: image API fetch failed for %s: %s",
                 image_id, console_safe(redact_civitai_token(exc)))
        return None

    items = data.get("items") or []
    return items[0] if items else None


def prompt_from_image_api(image_json: Optional[dict[str, Any]]) -> Optional[str]:
    if not image_json:
        return None
    meta = image_json.get("meta") or {}
    prompt = meta.get("prompt") or meta.get("Prompt")
    return str(prompt).strip() if prompt else None


# ============================================================
# Jobs + routes
# ============================================================

JOBS: dict[int, dict[str, Any]] = {}
_job_ids = itertools.count(1)
_ACTIVE_DESTS: set[str] = set()


@routes.get("/civitai/health")
async def civitai_health(request):
    dirs = {
        name: str(resolve_type_dir(name))
        for name in (
            "checkpoints", "diffusion_models", "loras", "vae", "embeddings",
            "controlnet", "upscale_models", "text_encoders", "hypernetworks",
        )
    }
    key, source = get_api_key_with_source()
    try:
        workflows_dir = str(workflow_target_dir("default", "UnknownBaseModel").parent)
    except Exception:  # noqa: BLE001 -- diagnostics only
        workflows_dir = None
    return web.json_response({
        "status": "ok",
        "node": "ComfyUI-Civitai-Direct",
        "version": NODE_VERSION,
        "has_civitai_api_key": bool(key),
        "api_key_source": source,
        "sidecars": sidecar_mode(),
        "previews": previews_mode(),
        "dirs": dirs,
        "images_dir": str(images_target_dir()),
        "workflows_dir": workflows_dir,
    })


@routes.get("/civitai/jobs")
async def civitai_jobs(request):
    return web.json_response(JOBS)


@routes.get("/civitai/status")
async def civitai_status(request):
    try:
        job = JOBS[int(request.query["id"])]
    except (KeyError, ValueError):
        return web.json_response({"error": "unknown job id"}, status=404)
    return web.json_response(job)


def request_user_id(request) -> Optional[str]:
    """ComfyUI user for this request; None if the request names an unknown user."""
    user_manager = getattr(PromptServer.instance, "user_manager", None)
    if user_manager is None:
        return "default"
    try:
        return user_manager.get_request_user_id(request)
    except KeyError:
        return None


@routes.get("/civitai/exists")
async def civitai_exists(request):
    """Read-only library check for the userscripts: is this version already on
    disk? Sidecar lookup only -- no hashing, no Civitai API calls -- so the
    model page can label its button the moment it renders."""
    try:
        version_id = int(request.query["model_version_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "need a numeric model_version_id"}, status=400)

    model_id_raw = (request.query.get("model_id") or "").strip()
    meta = {
        "model_version_id": version_id,
        "model_id": int(model_id_raw) if model_id_raw.isdigit() else None,
    }

    model_type = normalize_model_type(request.query.get("type"))
    base_model = normalize_base_model(request.query.get("base_model"), request.query.get("air"))
    if model_type == "Workflow":
        # A wrong user id just means "not found" on a read-only check.
        search_dir = workflow_metadata_dir(request_user_id(request) or "default", base_model)
    else:
        # Whole type folder, not just <type>/<base>: older downloads live in
        # pre-slug folder names, and moved files should count regardless.
        search_dir = choose_target_dir(model_type, base_model, "").parent

    existing = await asyncio.to_thread(matching_downloaded_version_sidecar, search_dir, meta)
    return web.json_response({
        "exists": existing is not None,
        "path": str(existing) if existing else None,
        "model_version_id": version_id,
    })


async def _handle_download(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    model_id = data.get("model_id")
    version_id = data.get("model_version_id")
    slug = data.get("model_slug")
    url = data.get("url")

    if model_id is None and url:
        model_id, url_version_id, url_slug = parse_ids_from_url(url)
        version_id = version_id or url_version_id
        slug = slug or url_slug
        # The main userscript also sends the page URL inside metadata.
        page_url = ((data.get("metadata") or {}).get("page_url")) or ""
        if model_id is None and page_url:
            model_id, page_version_id, page_slug = parse_ids_from_url(page_url)
            version_id = version_id or page_version_id
            slug = slug or page_slug

    if model_id is None and version_id is None:
        return web.json_response(
            {"error": "need model_id, model_version_id, or a recognizable Civitai url"},
            status=400,
        )

    user_id = request_user_id(request)
    if user_id is None:
        return web.json_response({"error": "unknown ComfyUI user"}, status=403)

    job_id = next(_job_ids)
    JOBS[job_id] = {
        "state": "resolving",
        "model_id": model_id,
        "model_version_id": version_id,
        "model_name": None,
        "version_name": None,
        "type": None,
        "base_model": None,
        "bytes_done": 0,
        "bytes_total": None,
        "path": None,
        "error": None,
        "user_id": user_id,
    }
    asyncio.create_task(_run_download(job_id, model_id, version_id, slug, user_id))
    return web.json_response({"status": "queued", "job_id": job_id})


# Same handler on both paths: /civitai/download matches the main userscript's
# payload ({url, metadata}), and the grid userscript's ({model_id, ...}).
routes.post("/civitai/download")(_handle_download)
routes.post("/civitai/download-by-id")(_handle_download)


@routes.post("/civitai/download-image")
async def civitai_download_image(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "invalid JSON body"}, status=400)

    image_id = data.get("image_id") or parse_image_id_from_url(data.get("page_url"))
    if not image_id and not data.get("image_url"):
        return web.json_response(
            {"error": "need image_id, image_url, or a Civitai /images/N page_url"},
            status=400,
        )

    job_id = next(_job_ids)
    JOBS[job_id] = {
        "kind": "image",
        "state": "resolving",
        "image_id": image_id,
        "bytes_done": 0,
        "bytes_total": None,
        "path": None,
        "error": None,
    }
    asyncio.create_task(_run_image_download(job_id, data, image_id))
    return web.json_response({"status": "queued", "job_id": job_id})


async def _run_image_download(job_id: int, data: dict[str, Any], image_id: Optional[int]) -> None:
    job = JOBS[job_id]
    part: Optional[Path] = None
    dest_key: Optional[str] = None
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
        async with aiohttp.ClientSession(headers=auth_headers(), timeout=timeout) as session:
            api_image = await fetch_civitai_image(session, image_id) if image_id else None

            image_url = (api_image or {}).get("url") or data.get("image_url")
            if not image_url:
                raise RuntimeError("no image URL found")

            # Fetch the original upload rather than a width-optimized render;
            # for PNGs this preserves embedded generation/ComfyUI metadata.
            image_url = original_image_url(image_url)

            client_prompt = str(image_generation_meta(data).get("prompt") or "").strip() or None
            prompt = (
                str(data.get("prompt") or "").strip()
                or client_prompt
                or prompt_from_image_api(api_image)
            )

            images_dir = images_target_dir()
            existing = existing_image_for_id(images_dir, image_id)
            if existing:
                job.update(state="already_exists", path=str(existing))
                log.info("civitai-direct: job %s: image %s already downloaded -> %s",
                         job_id, image_id, console_safe(existing))
                return

            images_dir.mkdir(parents=True, exist_ok=True)
            requested_name = image_filename(data, image_url, image_id)
            save_path = unique_image_destination_path(
                images_dir / requested_name, write_prompt=bool(prompt)
            )

            job["state"] = "downloading"
            async with session.get(image_url) as resp:
                if resp.status in (401, 403):
                    raise RuntimeError(
                        f"Civitai returned {resp.status} for the image -- it may "
                        "require a logged-in account (see /civitai/health for API key state)."
                    )
                resp.raise_for_status()

                # The original file's real type often differs from the URL
                # extension (Civitai serves originals from ".jpeg" URLs that
                # are actually PNGs), so trust the content-type.
                content_type = resp.headers.get("Content-Type", "")
                actual_ext = extension_from_content_type(content_type, fallback="")
                current_ext = ".jpg" if save_path.suffix.lower() == ".jpeg" else save_path.suffix.lower()
                if actual_ext and actual_ext != current_ext:
                    save_path = unique_image_destination_path(
                        save_path.with_suffix(actual_ext), write_prompt=bool(prompt)
                    )

                dest_key = str(save_path).lower()
                if dest_key in _ACTIVE_DESTS:
                    dest_key = None
                    raise RuntimeError("this image is already being downloaded")
                _ACTIVE_DESTS.add(dest_key)

                job["path"] = str(save_path)
                total = resp.headers.get("Content-Length")
                job["bytes_total"] = int(total) if total else None
                log.info("civitai-direct: job %s: downloading image %s (%s) -> %s",
                         job_id, image_id or "?",
                         format_size(job["bytes_total"]) or "unknown size",
                         console_safe(save_path))

                part = save_path.with_suffix(save_path.suffix + ".part")
                with part.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        fh.write(chunk)
                        job["bytes_done"] += len(chunk)

            part.replace(save_path)
            part = None

            job["state"] = "writing_metadata"
            embedded = (
                await asyncio.to_thread(extract_png_text_chunks, save_path)
                if save_path.suffix.lower() == ".png"
                else {}
            )

            extras = [".json sidecar"]
            if prompt:
                save_path.with_suffix(".txt").write_text(
                    prompt.strip() + "\n", encoding="utf-8"
                )
                extras.append(".txt prompt")

            sidecar = build_image_sidecar(
                data, image_id, image_url, prompt, api_image, save_path, embedded
            )
            write_json(save_path.with_suffix(".json"), sidecar)

            # Save the ComfyUI graph beside the image whenever we have one:
            # from the PNG itself, or (for JPEG/WebP originals with no embedded
            # metadata) from the graph the userscript captured off the page.
            workflow_json = _parse_json_maybe(embedded.get("workflow"))
            if not isinstance(workflow_json, (dict, list)):
                workflow_json = comfy_workflow_from_request(data)
            if isinstance(workflow_json, (dict, list)):
                write_json(save_path.with_suffix(".workflow.json"), workflow_json)
                extras.append(".workflow.json ComfyUI graph")

            job["state"] = "done"
            log.info("civitai-direct: job %s: done -> %s (+ %s)",
                     job_id, console_safe(save_path), ", ".join(extras))
    except Exception as exc:  # noqa: BLE001 -- the job record is the error channel
        message = redact_civitai_token(f"{type(exc).__name__}: {exc}")
        job.update(state="error", error=message)
        log.warning("civitai-direct: job %s: image download failed: %s",
                    job_id, console_safe(message))
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        if dest_key is not None:
            _ACTIVE_DESTS.discard(dest_key)


async def _run_download(job_id: int, model_id, version_id, slug, user_id: str) -> None:
    job = JOBS[job_id]
    part: Optional[Path] = None
    extraction_part: Optional[Path] = None
    dest_key: Optional[str] = None
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
        async with aiohttp.ClientSession(headers=auth_headers(), timeout=timeout) as session:
            model_json, version_json, meta = await resolve_model(session, model_id, version_id, slug)
            job.update(
                model_id=meta["model_id"],
                model_version_id=meta["model_version_id"],
                model_name=meta["model_name"],
                version_name=meta["version_name"],
                type=meta["type"],
                base_model=meta["base_model"],
            )

            model_type = normalize_model_type(meta["type"])
            workflow_download = model_type == "Workflow"

            log.info(
                "civitai-direct: job %s: %s (%s) -- %s / %s [model %s, version %s]",
                job_id,
                console_safe(meta["model_name"]),
                console_safe(meta["version_name"] or "?"),
                model_type,
                console_safe(meta["base_model"]),
                meta["model_id"],
                meta["model_version_id"],
            )

            file = pick_primary_model_file(version_json)
            if file is None:
                raise RuntimeError("this version has no downloadable files")
            download_url = file.get("downloadUrl") or version_json.get("downloadUrl")
            if not download_url:
                raise RuntimeError("this version has no download URL")

            name_haystack = " ".join(
                filter(None, [meta.get("model_name"), meta.get("version_name"), file.get("name")])
            )
            target_dir = (
                workflow_target_dir(user_id, meta["base_model"])
                if workflow_download
                else choose_target_dir(model_type, meta["base_model"], name_haystack)
            )
            metadata_dir = (
                workflow_metadata_dir(user_id, meta["base_model"])
                if workflow_download
                else None
            )

            if workflow_download:
                existing = await asyncio.to_thread(
                    matching_downloaded_version_sidecar, metadata_dir or target_dir, meta)
                if not existing:
                    # Compatibility with the sibling downloader's older layout.
                    existing = await asyncio.to_thread(
                        matching_downloaded_version_sidecar, target_dir, meta)
            else:
                # Whole type folder (loras/, checkpoints/, ...), not just the
                # target dir: older downloads live in pre-slug folder names,
                # and files the user reorganized should count regardless.
                existing = await asyncio.to_thread(
                    matching_downloaded_version_sidecar, target_dir.parent, meta)
            if existing:
                job.update(state="already_exists", path=str(existing))
                # Models found via a legacy .json sidecar gain the ecosystem's
                # .civitai.info (and a preview) too, so old libraries converge
                # on the format the loader panels read.
                if not workflow_download and existing.is_file():
                    info_path = civitai_info_path(existing)
                    if not info_path.exists():
                        write_json(info_path, build_civitai_info(model_json, version_json, meta))
                    await save_preview_image(session, version_json, existing, job_id)
                log.info("civitai-direct: job %s: already downloaded -> %s",
                         job_id, console_safe(existing))
                return

            # Second duplicate layer, for files this tool didn't download (no
            # sidecar): same name on disk + SHA256 matching Civitai's record
            # means the file is already here -- adopt it (backfilling the
            # missing sidecars) instead of saving a "name (1)" copy. A name
            # match with a DIFFERENT hash is a genuine collision (versions
            # often reuse filenames) and still gets the unique suffix.
            api_sha256 = ((file.get("hashes") or {}).get("SHA256") or "").strip().lower()
            sha_cache: dict[str, str] = {}

            async def same_bytes_already_here(path: Path) -> bool:
                if not api_sha256 or not path.is_file():
                    return False
                sha = sha_cache.get(str(path))
                if sha is None:
                    job["state"] = "hashing_existing"
                    sha = await calculate_sha256(path)
                    sha_cache[str(path)] = sha
                return sha.lower() == api_sha256

            def adopt_existing(path: Path) -> None:
                job.update(state="already_exists", path=str(path))
                info_path = civitai_info_path(path)
                if not info_path.exists():
                    write_json(info_path, build_civitai_info(model_json, version_json, meta))
                if sidecar_mode() == "full":
                    json_path = path.with_suffix(path.suffix + ".json")
                    if not json_path.exists():
                        sidecar = build_sidecar_json(
                            meta, {"model": model_json, "model_version": version_json},
                            path, download_url, sha_cache.get(str(path)),
                            path.stat().st_size, "already_exists",
                        )
                        write_json(json_path, sidecar)
                        if SAVE_MARKDOWN_NOTES:
                            write_markdown(path.with_suffix(path.suffix + ".md"), sidecar)
                log.info("civitai-direct: job %s: identical file already on disk, adopted -> %s",
                         job_id, console_safe(path))

            expected_name = (
                slug_aware_filename(meta) if not workflow_download and is_wan_model(meta)
                else safe_filename(file.get("name") or "") or slug_aware_filename(meta)
            )
            # Search the whole type folder (loras/, checkpoints/, ...), not just
            # the target dir: a model the user moved into a subfolder -- or
            # another base-model folder -- still counts as already here.
            if not workflow_download:
                for candidate in find_same_named_files(target_dir.parent, expected_name):
                    if await same_bytes_already_here(candidate):
                        adopt_existing(candidate)
                        return

            job["state"] = "downloading"
            key, key_source = get_api_key_with_source()
            key_desc = f"API key loaded from {key_source}" if key else "no API key configured"
            availability = version_json.get("availability")

            def _gated_error(detail: str, body_text: str = "") -> RuntimeError:
                # The download endpoint is the one honest source of Early
                # Access state: the models API reports these versions as
                # "Public", but a 403 comes back as JSON like {"error":
                # "Early Access", "deadline": "2026-08-22T18:30:00.000Z"}.
                body: dict[str, Any] = {}
                try:
                    parsed = json.loads(body_text) if body_text else None
                    if isinstance(parsed, dict):
                        body = parsed
                except ValueError:
                    pass
                api_error = str(body.get("error") or "")
                if availability == "EarlyAccess" or "early access" in api_error.lower():
                    deadline = str(body.get("deadline") or "")[:10]
                    when = (
                        f"It unlocks for everyone on {deadline}."
                        if deadline
                        else "It becomes downloadable when the early-access window ends."
                    )
                    return RuntimeError(
                        f"{detail} -- this version is in Early Access, which needs the "
                        "early-access purchase/subscription on your Civitai account. "
                        f"An API key alone does not unlock it ({key_desc}). {when}"
                    )
                return RuntimeError(
                    f"{detail} -- the model requires a logged-in account "
                    f"({key_desc}; see /civitai/health). If a key is loaded, check it "
                    "at civitai.com/user/account and make sure your account can "
                    "download this model in the browser."
                )

            async with session.get(add_token_to_url(download_url)) as resp:
                if resp.status in (401, 403):
                    try:
                        body_text = (await resp.text())[:2000]
                    except Exception:  # noqa: BLE001 -- the status alone still errors fine
                        body_text = ""
                    if not body_text.lstrip().startswith("{"):
                        body_text = await probe_gate_reason(session, download_url) or body_text
                    raise _gated_error(f"Civitai returned {resp.status}", body_text)
                resp.raise_for_status()
                if "text/html" in resp.headers.get("Content-Type", ""):
                    raise _gated_error(
                        "Civitai returned HTML instead of a model file",
                        await probe_gate_reason(session, download_url),
                    )

                served_name = None
                if resp.content_disposition and resp.content_disposition.filename:
                    served_name = safe_filename(resp.content_disposition.filename)
                filename = served_name or safe_filename(file.get("name") or "") or slug_aware_filename(meta)
                if not workflow_download and is_wan_model(meta):
                    filename = slug_aware_filename(meta)

                target_dir.mkdir(parents=True, exist_ok=True)
                workflow_artifact: Optional[Path] = None
                if workflow_download:
                    dest, workflow_artifact = unique_workflow_destination(target_dir / filename)
                else:
                    dest = target_dir / filename
                if not workflow_download and dest.exists():
                    # Served filename can differ from the API name, so this
                    # can be a path the pre-download check never looked at.
                    if await same_bytes_already_here(dest):
                        adopt_existing(dest)
                        return
                    dest = unique_destination_path(dest)
                job["state"] = "downloading"
                dest_key = str(workflow_artifact or dest).lower()
                if dest_key in _ACTIVE_DESTS:
                    dest_key = None
                    raise RuntimeError("this file is already being downloaded")
                _ACTIVE_DESTS.add(dest_key)

                part = dest.with_suffix(dest.suffix + ".part")
                job["path"] = str(workflow_artifact or dest)
                total = resp.headers.get("Content-Length")
                job["bytes_total"] = int(total) if total else None
                log.info("civitai-direct: job %s: downloading %s -> %s",
                         job_id, format_size(job["bytes_total"]) or "unknown size",
                         console_safe(dest))

                with part.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        fh.write(chunk)
                        job["bytes_done"] += len(chunk)

            part.replace(dest)
            part = None

            job["state"] = "hashing"
            file_sha256 = await calculate_sha256(dest)
            file_size = dest.stat().st_size

            artifact_path = dest
            if workflow_download and dest.suffix.lower() == ".zip":
                if workflow_artifact is None:
                    raise RuntimeError("could not determine workflow extraction directory")
                job["state"] = "extracting"
                extraction_part = workflow_artifact.with_name(workflow_artifact.name + ".part")
                await asyncio.to_thread(_extract_workflow_zip, dest, extraction_part)
                extraction_part.replace(workflow_artifact)
                extraction_part = None
                dest.unlink()
                artifact_path = workflow_artifact
                job["path"] = str(artifact_path)

            job["state"] = "writing_metadata"
            api_raw = {"model": model_json, "model_version": version_json}
            json_path = (
                workflow_sidecar_json_path(metadata_dir, artifact_path)
                if workflow_download and metadata_dir is not None
                else dest.with_suffix(dest.suffix + ".json")
            )
            markdown_path = (
                workflow_sidecar_markdown_path(metadata_dir, artifact_path)
                if workflow_download and metadata_dir is not None
                else dest.with_suffix(dest.suffix + ".md")
            )
            sidecars = []
            if not workflow_download:
                write_json(civitai_info_path(artifact_path),
                           build_civitai_info(model_json, version_json, meta))
                sidecars.append(".civitai.info")
                if await save_preview_image(session, version_json, artifact_path, job_id):
                    sidecars.append(".preview")
            # Workflows always keep the v1 json+md sidecars: they are the
            # workflow dedupe record, and .civitai.info is a model-file
            # convention other tools would not look for on a workflow.
            if workflow_download or sidecar_mode() == "full":
                sidecar = build_sidecar_json(
                    meta, api_raw, artifact_path, download_url, file_sha256, file_size,
                    "downloaded",
                    json_file_path=json_path,
                    markdown_file_path=markdown_path,
                )
                write_json(json_path, sidecar)
                sidecars.append(".json")
                if SAVE_MARKDOWN_NOTES:
                    write_markdown(markdown_path, sidecar)
                    sidecars.append(".md")

            job["state"] = "done"
            log.info("civitai-direct: job %s: done -> %s (+ %s)",
                     job_id, console_safe(artifact_path), "/".join(sidecars) or "no sidecars")
    except Exception as exc:  # noqa: BLE001 -- the job record is the error channel
        # Redact before storing: aiohttp errors embed the request URL, which
        # now carries ?token=, and the job record reaches the browser console.
        message = redact_civitai_token(f"{type(exc).__name__}: {exc}")
        job.update(state="error", error=message)
        log.warning("civitai-direct: job %s: failed: %s", job_id, console_safe(message))
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        if extraction_part is not None:
            try:
                shutil.rmtree(extraction_part)
            except OSError:
                pass
    finally:
        if dest_key is not None:
            _ACTIVE_DESTS.discard(dest_key)


NODE_CLASS_MAPPINGS: dict[str, Any] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, Any] = {}
