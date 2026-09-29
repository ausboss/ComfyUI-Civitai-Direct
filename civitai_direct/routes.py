"""The /civitai/* routes, registered on ComfyUI's own web server."""

import asyncio
from typing import Any, Optional

from aiohttp import web
from server import PromptServer

from . import NODE_VERSION
from .civitai_api import parse_ids_from_url, parse_image_id_from_url
from .classify import normalize_base_model, normalize_model_type
from .downloads import ModelDownload
from .images import run_image_download
from .jobs import JOBS, create_job
from .library import find_downloaded_version
from .paths import (
    images_target_dir,
    resolve_type_dir,
    search_roots,
    workflow_metadata_dir,
    workflow_target_dir,
)
from .settings import base_folders_enabled, get_api_key_with_source, previews_mode, sidecar_mode

routes = PromptServer.instance.routes

_HEALTH_FOLDERS = (
    "checkpoints", "diffusion_models", "loras", "vae", "embeddings",
    "controlnet", "upscale_models", "text_encoders", "hypernetworks",
)
# A task the event loop only weakly references can be collected mid-run.
_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _json_object(request) -> Optional[dict[str, Any]]:
    """The request body as a dict, or None when it isn't a JSON object."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 -- any parse failure is a bad body
        return None
    return data if isinstance(data, dict) else None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def request_user_id(request) -> Optional[str]:
    """The ComfyUI user for this request; None if it names an unknown user."""
    user_manager = getattr(PromptServer.instance, "user_manager", None)
    if user_manager is None:
        return "default"
    try:
        return user_manager.get_request_user_id(request)
    except KeyError:
        return None


@routes.get("/civitai/health")
async def civitai_health(request):
    key, source = get_api_key_with_source()
    try:
        workflows_dir = str(workflow_target_dir("default", "unknown").parent)
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
        "base_folders": "on" if base_folders_enabled() else "off",
        "dirs": {name: str(resolve_type_dir(name)) for name in _HEALTH_FOLDERS},
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


@routes.get("/civitai/exists")
async def civitai_exists(request):
    """Read-only library check for the userscripts: is this version already on
    disk? Sidecars only (no hashing, no Civitai calls), so a model page can
    label its button the moment it renders."""
    version_id = _as_int(request.query.get("model_version_id"))
    if version_id is None:
        return web.json_response({"error": "need a numeric model_version_id"}, status=400)
    model_id = _as_int(request.query.get("model_id"))

    model_type = normalize_model_type(request.query.get("type"))
    base_model = normalize_base_model(request.query.get("base_model"), request.query.get("air"))
    if model_type == "Workflow":
        # A wrong user id just means "not found" on a read-only check.
        user_id = request_user_id(request) or "default"
        roots = [workflow_metadata_dir(user_id, base_model), workflow_target_dir(user_id, base_model)]
    else:
        roots = search_roots(model_type, base_model)

    existing = await asyncio.to_thread(find_downloaded_version, roots, version_id, model_id)
    return web.json_response({
        "exists": existing is not None,
        "path": str(existing) if existing else None,
        "model_version_id": version_id,
    })


@routes.post("/civitai/download")
async def civitai_download(request):
    """Accepts the page userscript's {url, metadata} and the grid userscript's
    {model_id, model_version_id}."""
    data = await _json_object(request)
    if data is None:
        return web.json_response({"error": "the request body must be a JSON object"}, status=400)

    model_id = _as_int(data.get("model_id"))
    version_id = _as_int(data.get("model_version_id"))
    slug = data.get("model_slug") if isinstance(data.get("model_slug"), str) else None
    url = data.get("url") if isinstance(data.get("url"), str) else None

    if model_id is None and url:
        model_id, url_version_id, url_slug = parse_ids_from_url(url)
        version_id = version_id or url_version_id
        slug = slug or url_slug
        # The page userscript also sends the page URL inside metadata.
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        page_url = metadata.get("page_url") if isinstance(metadata.get("page_url"), str) else ""
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

    job_id = create_job(
        model_id=model_id, model_version_id=version_id, model_name=None, version_name=None,
        type=None, base_model=None, bytes_done=0, bytes_total=None, path=None, error=None,
        user_id=user_id,
    )
    _spawn(ModelDownload(job_id, model_id, version_id, slug, user_id).run())
    return web.json_response({"status": "queued", "job_id": job_id})


@routes.post("/civitai/download-image")
async def civitai_download_image(request):
    data = await _json_object(request)
    if data is None:
        return web.json_response({"error": "the request body must be a JSON object"}, status=400)

    page_url = data.get("page_url") if isinstance(data.get("page_url"), str) else None
    image_id = _as_int(data.get("image_id")) or parse_image_id_from_url(page_url)
    if not image_id and not data.get("image_url"):
        return web.json_response(
            {"error": "need image_id, image_url, or a Civitai /images/N page_url"},
            status=400,
        )

    job_id = create_job(
        kind="image", image_id=image_id, bytes_done=0, bytes_total=None, path=None, error=None,
    )
    _spawn(run_image_download(job_id, data, image_id))
    return web.json_response({"status": "queued", "job_id": job_id})
