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
    GET  /civitai/status?id=N     state/progress of one download job
    GET  /civitai/jobs            every job this session

The Civitai API key (needed for gated / early-access models) is read from
the CIVITAI_API_KEY environment variable, or from a civitai_api_key.txt file
next to this __init__.py (gitignored).

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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

SAVE_MARKDOWN_NOTES = True
SAVE_RAW_API_JSON = True

_KEY_FILE = Path(__file__).resolve().parent / "civitai_api_key.txt"


def get_api_key() -> str:
    key = os.getenv("CIVITAI_API_KEY", "").strip()
    if not key and _KEY_FILE.is_file():
        try:
            key = _KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
    return key


def auth_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    key = get_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


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


def normalize_base_model(value: Optional[str]) -> str:
    value = (value or "").strip()
    compact = re.sub(r"[\s._-]+", "", value.lower())

    if "zimageturbo" in compact:
        return "ZImageTurbo"
    if "zimage" in compact:
        return "ZImage"
    if "flux2klein9b" in compact:
        return "Flux.2 Klein 9B"
    if "flux1dev" in compact:
        return "Flux.1 Dev"
    if "flux1schnell" in compact:
        return "Flux.1 Schnell"
    if "flux.1" in value.lower():
        return "Flux.1"
    if "illustrious" in compact:
        return "Illustrious"
    if "anima" in compact:
        return "Anima"
    if "sdxl" in compact:
        return "SDXL"
    if "sd15" in compact or "sd1.5" in value.lower():
        return "SD 1.5"
    if "pony" in compact:
        return "Pony"
    if "sd35large" in compact or "sd3.5large" in value.lower():
        return "SD 3.5 Large"
    if "sd35medium" in compact or "sd3.5medium" in value.lower():
        return "SD 3.5 Medium"
    if "sd3" in compact:
        return "SD 3"
    if "sd21" in compact or "sd2.1" in value.lower():
        return "SD 2.1"
    if "sd14" in compact or "sd1.4" in value.lower():
        return "SD 1.4"
    return safe_folder(value or "UnknownBaseModel", "UnknownBaseModel")


# SD-family checkpoints load through CheckpointLoaderSimple and belong in
# checkpoints/; newer diffusion models (Flux, Wan, LTX, ...) load through
# "load diffusion model" and belong in diffusion_models/.
_STABLE_DIFFUSION_BASES = {"Base Model", "Illustrious", "Anima", "SDXL", "SD 1.5", "Pony"}

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
    if model_id is None and version_id is not None:
        version_json = await fetch_json(session, CIVITAI_VERSION_API.format(version_id=version_id))
        model_id = version_json.get("modelId")
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
        "base_model": normalize_base_model(version_json.get("baseModel")),
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
    }
    return model_json, version_json, meta


# ============================================================
# Sidecars (schema-compatible with easy-civitai-downloader)
# ============================================================

def redact_civitai_token(text: Any) -> str:
    return re.sub(r"([?&]token=)[^&\s]+", r"\1REDACTED", str(text or ""))


def build_sidecar_json(
    meta: dict[str, Any],
    api_raw: dict[str, Any],
    save_path: Path,
    download_url: str,
    file_sha256: Optional[str],
    file_size: Optional[int],
    status: str,
) -> dict[str, Any]:
    json_file_path = save_path.with_suffix(save_path.suffix + ".json")
    markdown_file_path = save_path.with_suffix(save_path.suffix + ".md")

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
        },
        "hashes": {
            "page_hash_type": meta.get("hash_type"),
            "page_hash_value": meta.get("hash_value"),
            "downloaded_sha256": file_sha256,
        },
        "api_raw": api_raw if SAVE_RAW_API_JSON else None,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


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


def matching_downloaded_version_sidecar(target_dir: Path, meta: dict[str, Any]) -> Optional[Path]:
    """Existing model file for this exact version, found via its sidecar."""
    if not meta.get("model_version_id") or not target_dir.exists():
        return None

    requested_version = str(meta["model_version_id"])
    requested_model = str(meta["model_id"]) if meta.get("model_id") else None

    for sidecar_path in target_dir.rglob("*.json"):
        if sidecar_path.name.endswith((".failed.json", ".failed.civitai.json")):
            continue
        try:
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

        local_model_file = (payload.get("local") or {}).get("model_file")
        if local_model_file and Path(str(local_model_file)).exists():
            return Path(str(local_model_file))
    return None


async def calculate_sha256(path: Path) -> str:
    def _hash() -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    return await asyncio.to_thread(_hash)


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
    return web.json_response({
        "status": "ok",
        "node": "ComfyUI-Civitai-Direct",
        "version": NODE_VERSION,
        "has_civitai_api_key": bool(get_api_key()),
        "dirs": dirs,
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
    }
    asyncio.create_task(_run_download(job_id, model_id, version_id, slug))
    return web.json_response({"status": "queued", "job_id": job_id})


# Same handler on both paths: /civitai/download matches the main userscript's
# payload ({url, metadata}), and the grid userscript's ({model_id, ...}).
routes.post("/civitai/download")(_handle_download)
routes.post("/civitai/download-by-id")(_handle_download)


async def _run_download(job_id: int, model_id, version_id, slug) -> None:
    job = JOBS[job_id]
    part: Optional[Path] = None
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
            if model_type == "Workflow":
                raise RuntimeError(
                    "Workflow downloads aren't supported yet -- use easy-civitai-downloader for those."
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
            target_dir = choose_target_dir(model_type, meta["base_model"], name_haystack)

            existing = matching_downloaded_version_sidecar(target_dir, meta)
            if existing:
                job.update(state="already_exists", path=str(existing))
                log.info("civitai-direct: version %s already at %s",
                         meta["model_version_id"], ascii(str(existing)))
                return

            job["state"] = "downloading"
            async with session.get(download_url) as resp:
                if resp.status in (401, 403):
                    raise RuntimeError(
                        f"Civitai returned {resp.status} -- gated or early-access model; "
                        "set an API key (CIVITAI_API_KEY or civitai_api_key.txt)."
                    )
                resp.raise_for_status()
                if "text/html" in resp.headers.get("Content-Type", ""):
                    raise RuntimeError(
                        "Civitai returned HTML instead of a model file -- "
                        "the model is gated or your API key is invalid."
                    )

                served_name = None
                if resp.content_disposition and resp.content_disposition.filename:
                    served_name = safe_filename(resp.content_disposition.filename)
                filename = served_name or safe_filename(file.get("name") or "") or slug_aware_filename(meta)
                if is_wan_model(meta):
                    filename = slug_aware_filename(meta)

                target_dir.mkdir(parents=True, exist_ok=True)
                dest = unique_destination_path(target_dir / filename)
                dest_key = str(dest).lower()
                if dest_key in _ACTIVE_DESTS:
                    dest_key = None
                    raise RuntimeError("this file is already being downloaded")
                _ACTIVE_DESTS.add(dest_key)

                part = dest.with_suffix(dest.suffix + ".part")
                job["path"] = str(dest)
                total = resp.headers.get("Content-Length")
                job["bytes_total"] = int(total) if total else None

                with part.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        fh.write(chunk)
                        job["bytes_done"] += len(chunk)

            part.replace(dest)
            part = None

            job["state"] = "hashing"
            file_sha256 = await calculate_sha256(dest)
            file_size = dest.stat().st_size

            job["state"] = "writing_metadata"
            api_raw = {"model": model_json, "model_version": version_json}
            sidecar = build_sidecar_json(
                meta, api_raw, dest, download_url, file_sha256, file_size, "downloaded"
            )
            write_json(dest.with_suffix(dest.suffix + ".json"), sidecar)
            if SAVE_MARKDOWN_NOTES:
                write_markdown(dest.with_suffix(dest.suffix + ".md"), sidecar)

            job["state"] = "done"
            log.info("civitai-direct: saved model %s version %s -> %s",
                     meta["model_id"], meta["model_version_id"], ascii(str(dest)))
    except Exception as exc:  # noqa: BLE001 -- the job record is the error channel
        job.update(state="error", error=f"{type(exc).__name__}: {exc}")
        log.warning("civitai-direct: job %s failed: %s", job_id, ascii(str(exc)))
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        if dest_key is not None:
            _ACTIVE_DESTS.discard(dest_key)


NODE_CLASS_MAPPINGS: dict[str, Any] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, Any] = {}
