"""Decide what a Civitai model is and where it belongs.

Pure functions over Civitai's API payloads. No ComfyUI imports.
"""

import html
import re
from typing import Any, Optional

from .text import safe_folder, sanitize_filename_component

# Civitai's nsfwLevel is a bitmask, not a scale: a model or version carries the
# OR of every browsing level its images fall under, so 3 is "PG and PG13" and
# 60 is "R, X, XXX and Blocked".
NSFW_LEVELS: tuple[tuple[int, str], ...] = (
    (1, "PG"), (2, "PG13"), (4, "R"), (8, "X"), (16, "XXX"), (32, "Blocked"),
)


def decode_nsfw_level(value: Any) -> tuple[list[str], Optional[str]]:
    """(every browsing level in the bitmask, the highest one)."""
    try:
        bits = int(value)
    except (TypeError, ValueError):
        return [], None
    names = [name for bit, name in NSFW_LEVELS if bits & bit]
    return names, (names[-1] if names else None)


def parse_permission_list(value: Any) -> list[str]:
    """allowCommercialUse is a JSON list on some endpoints and a raw Postgres
    array string ("{Image,RentCivit,Rent}") on others."""
    if isinstance(value, list):
        return [str(item) for item in value]
    text = str(value or "").strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    return [part.strip().strip('"') for part in text.split(",") if part.strip()]


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
    # Civitai's "CLIP" type is CLIP-L/G text encoders, not vision towers.
    if compact == "clip":
        return "TextEncoder"
    if compact == "clipvision":
        return "CLIPVision"
    if compact == "unet":
        return "Unet"
    if compact in {"motionmodule", "motionmodules"}:
        return "MotionModule"
    if compact == "detection":
        return "Detection"
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
    "krea2"), Civitai's own lowercase, space-free family slug."""
    if not air:
        return None
    match = re.match(r"^(?:urn:)?(?:air:)?([a-z0-9][a-z0-9.-]*):", str(air).strip().lower())
    return match.group(1) if match else None


def slugify_folder(value: str) -> str:
    """Lowercase, space-free folder slug: "Wan Video 2.2 I2V-A14B" becomes
    "wan-video-22-i2v-a14b". Dots vanish first so version numbers read like
    the community's folder names (2.2 -> 22, Flux.1 -> flux1)."""
    value = html.unescape(value or "").strip().lower().replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def normalize_base_model(value: Optional[str], air: Optional[str] = None) -> str:
    """Folder slug for a Civitai base model: lowercase, no spaces, AIR-style.
    Known families map explicitly (keeping distinctions AIR drops); anything
    else uses the version's own AIR ecosystem token, then a slug of the name."""
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


# Base models whose checkpoints load through ComfyUI's "Load Checkpoint" node
# and belong in checkpoints/. Newer diffusion models (Flux, Wan, LTX, ...) load
# through "Load Diffusion Model" and belong in diffusion_models/.
CHECKPOINT_BASES = {
    "base-model", "sd14", "sd15", "sd21", "sdxl", "pony", "illustrious", "anima",
    "sd3", "sd35-large", "sd35-medium",
}

# Civitai model type -> folder_paths folder name. Names that only a custom
# node pack registers (animatediff_models, ultralytics) resolve through that
# pack's registration when installed, and fall back to models/<name>/.
TYPE_TO_FOLDER_NAME: dict[str, str] = {
    "Lora": "loras",
    "VAE": "vae",
    "Embedding": "embeddings",
    "ControlNet": "controlnet",
    "Hypernetwork": "hypernetworks",
    "Upscaler": "upscale_models",
    "TextEncoder": "text_encoders",
    "CLIPVision": "clip_vision",
    "Unet": "diffusion_models",
    "MotionModule": "animatediff_models",
    "Detection": "ultralytics",
}

# Types folder_paths has no entry for; they live under models/<dirname>/.
TYPE_FALLBACK_DIRNAME: dict[str, str] = {
    "Wildcard": "wildcards",
    "Pose": "poses",
}


def folder_name_for(model_type: str, base_model: str, name_haystack: str = "") -> Optional[str]:
    """The folder_paths name a model type belongs in, or None when
    folder_paths has no entry for it."""
    if model_type == "Checkpoint":
        is_aio = "AIO" in name_haystack.upper()
        return "checkpoints" if is_aio or base_model in CHECKPOINT_BASES else "diffusion_models"
    return TYPE_TO_FOLDER_NAME.get(model_type)


def fallback_dirname(model_type: str) -> str:
    return TYPE_FALLBACK_DIRNAME.get(model_type) or safe_folder(model_type, "Other")


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
    """<slug>__<version>-v<id>.safetensors. Wan files from different versions
    share generic names, so they get a name that says which they are."""
    slug = meta.get("model_slug") or (str(meta["model_id"]) if meta.get("model_id") else "model")
    slug = sanitize_filename_component(slug, "model")
    version = sanitize_filename_component(meta.get("version_name") or "", "")
    base = f"{slug}__{version}" if version else slug
    suffix = f"-v{meta['model_version_id']}" if meta.get("model_version_id") else ""
    return f"{base}{suffix}.safetensors"


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
    """Sort, filter and identify signals, flattened out of the two API
    payloads, so a script can decide where a file belongs without asking
    Civitai again."""
    file = file or {}
    file_meta = file.get("metadata") or {}

    version_levels, version_top = decode_nsfw_level(version_json.get("nsfwLevel"))
    model_levels, model_top = decode_nsfw_level(model_json.get("nsfwLevel"))

    return {
        # The uploader's own NSFW checkbox. Unreliable on its own (plenty of
        # models sit at nsfw=false with XXX images), so sort on the levels.
        "nsfw": model_json.get("nsfw"),
        # Rates THIS version, i.e. the file being saved.
        "nsfw_level": version_json.get("nsfwLevel"),
        "nsfw_levels": version_levels,
        "nsfw_level_top": version_top,
        # Union across every version of the model, so it reads higher than the
        # downloaded file often deserves.
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
