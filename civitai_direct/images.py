"""Gallery image downloads: the file, its prompt, and its generation data."""

import asyncio
import json
import logging
import struct
import zlib
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp

from . import LOGGER_NAME
from .civitai_api import CHUNK, CIVITAI_IMAGE_API, get, is_civitai_url, open_session
from .jobs import JOBS, claim_destination, discard, job_error_message, release_destination
from .paths import images_target_dir
from .sidecars import write_json
from .text import console_safe, format_size, log_safe, now_iso, safe_filename

log = logging.getLogger(LOGGER_NAME)

IMAGE_SIDECAR_SCHEMA = "ausboss-civitai-image-sidecar-v1"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
_MAX_TEXT_CHUNK = 64 * 1024 * 1024


def extension_from_url(url: str, fallback: str = ".jpg") -> str:
    try:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return suffix
    except ValueError:
        pass
    return fallback


def original_image_url(url: str) -> str:
    """Rewrite an image.civitai.com delivery URL to fetch the original upload.

    Civitai serves width-optimized renders (.../width=1152/name.jpeg).
    Swapping the transform segment for original=true returns the unmodified
    file, which for PNGs keeps the embedded generation and ComfyUI metadata."""
    try:
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() != "image.civitai.com":
            return url

        parts = parsed.path.split("/")
        if len(parts) >= 2 and "=" in parts[-2]:
            parts[-2] = "original=true"
        elif len(parts) >= 2:
            parts.insert(-1, "original=true")
        else:
            return url
        return parsed._replace(path="/".join(parts)).geturl()
    except ValueError:
        return url


def extension_from_content_type(content_type: str, fallback: str = ".jpg") -> str:
    content_type = (content_type or "").lower()
    for needle, ext in (("png", ".png"), ("webp", ".webp"), ("gif", ".gif"), ("avif", ".avif"),
                        ("jpeg", ".jpg"), ("jpg", ".jpg")):
        if needle in content_type:
            return ext
    return fallback


def extract_png_text_chunks(path: Path) -> dict[str, str]:
    """tEXt, zTXt and iTXt chunks of a PNG as {keyword: text}. ComfyUI keeps
    its prompt and workflow there, A1111 keeps 'parameters'."""
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
                if chunk_type in {b"tEXt", b"zTXt", b"iTXt"} and length < _MAX_TEXT_CHUNK:
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
                    except (ValueError, zlib.error):
                        continue
                else:
                    f.seek(length + 4, 1)
                if chunk_type == b"IEND":
                    break
    except OSError as exc:
        log.warning("civitai-direct: failed to read PNG text chunks from %s: %s",
                    console_safe(path.name), console_safe(exc))
    return result


def _parse_json_maybe(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value


def image_generation_meta(data: dict[str, Any]) -> dict[str, Any]:
    gen = data.get("generation_data") or {}
    meta = gen.get("meta") if isinstance(gen, dict) else None
    return meta if isinstance(meta, dict) else {}


def comfy_workflow_from_request(data: dict[str, Any]) -> Any:
    """The ComfyUI graph from the userscript's generation data, if present.
    Civitai stores {"prompt": ..., "workflow": ...}; the workflow half is what
    ComfyUI loads."""
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

    # The userscript's data comes from the site's own generation-data endpoint
    # and is usually richer than the public API, so it wins.
    meta = {**api_meta, **{k: v for k, v in client_meta.items() if v is not None}}

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
            # The site's resources carry model and version names; the meta
            # variants only carry ids.
            "resources": client_data.get("resources") or meta.get("resources") or meta.get("civitaiResources"),
        },
        "embedded_metadata": {
            # A1111-style generation parameters, if present in the file
            "parameters": embedded.get("parameters"),
            # ComfyUI prompt graph, if present in the file
            "comfyui_prompt": _parse_json_maybe(embedded.get("prompt")),
            "has_comfyui_workflow": bool(embedded.get("workflow")),
            "has_civitai_comfy_workflow": comfy_workflow_from_request(data) is not None,
        },
        "local": {
            "image_file": str(save_path),
            "image_file_name": save_path.name,
        },
        "api_meta_raw": api_meta,
        "generation_data_raw": client_data or None,
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


def existing_image_for_id(images_dir: Path, image_id: Optional[int]) -> Optional[Path]:
    """An already-downloaded copy of this exact image, by its default name."""
    if not image_id or not images_dir.is_dir():
        return None
    for candidate in images_dir.glob(f"civitai-image-{int(image_id)}.*"):
        if candidate.suffix.lower() in IMAGE_EXTENSIONS:
            return candidate
    return None


async def fetch_civitai_image(session: aiohttp.ClientSession, image_id: int) -> Optional[dict[str, Any]]:
    try:
        async with get(session, CIVITAI_IMAGE_API, params={"imageId": str(image_id)}) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001 -- enrichment only, never a blocker
        log.info("civitai-direct: image API fetch failed for %s: %s", image_id, log_safe(exc))
        return None

    items = data.get("items") or []
    return items[0] if items else None


def prompt_from_image_api(image_json: Optional[dict[str, Any]]) -> Optional[str]:
    if not image_json:
        return None
    meta = image_json.get("meta") or {}
    prompt = meta.get("prompt") or meta.get("Prompt")
    return str(prompt).strip() if prompt else None


async def run_image_download(job_id: int, data: dict[str, Any], image_id: Optional[int]) -> None:
    job = JOBS[job_id]
    part: Optional[Path] = None
    claim: Optional[str] = None
    try:
        async with open_session() as session:
            api_image = await fetch_civitai_image(session, image_id) if image_id else None

            image_url = (api_image or {}).get("url") or data.get("image_url")
            if not image_url:
                raise RuntimeError("no image URL found")
            # The URL can come from the browser, and this session carries the
            # API key, so it must be Civitai's.
            if not is_civitai_url(image_url):
                raise RuntimeError("image_url must be a civitai.com address")

            # Fetch the original upload rather than a width-optimized render;
            # for PNGs this preserves embedded generation and ComfyUI metadata.
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
            save_path = unique_image_destination_path(
                images_dir / image_filename(data, image_url, image_id), write_prompt=bool(prompt)
            )

            job["state"] = "downloading"
            async with get(session, image_url) as resp:
                if resp.status in (401, 403):
                    raise RuntimeError(
                        f"Civitai returned {resp.status} for the image. It may require a "
                        "login: see /civitai/health for the API key state."
                    )
                resp.raise_for_status()

                # The original's real type often differs from the URL
                # extension (Civitai serves PNGs from ".jpeg" URLs), so trust
                # the content type.
                actual_ext = extension_from_content_type(resp.headers.get("Content-Type", ""), fallback="")
                current_ext = ".jpg" if save_path.suffix.lower() == ".jpeg" else save_path.suffix.lower()
                if actual_ext and actual_ext != current_ext:
                    save_path = unique_image_destination_path(
                        save_path.with_suffix(actual_ext), write_prompt=bool(prompt)
                    )

                claim = claim_destination(save_path)
                if claim is None:
                    raise RuntimeError("this image is already being downloaded")

                job["path"] = str(save_path)
                total = resp.headers.get("Content-Length")
                job["bytes_total"] = int(total) if total else None
                log.info("civitai-direct: job %s: downloading image %s (%s) -> %s",
                         job_id, image_id or "?",
                         format_size(job["bytes_total"]) or "unknown size", console_safe(save_path))

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
                save_path.with_suffix(".txt").write_text(prompt.strip() + "\n", encoding="utf-8")
                extras.append(".txt prompt")

            write_json(
                save_path.with_suffix(".json"),
                build_image_sidecar(data, image_id, image_url, prompt, api_image, save_path, embedded),
            )

            # Keep the ComfyUI graph beside the image whenever there is one:
            # from the PNG itself, or (for JPEG/WebP originals with no embedded
            # metadata) from the graph the userscript read off the page.
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
        message = job_error_message(exc)
        job.update(state="error", error=message)
        log.warning("civitai-direct: job %s: image download failed: %s", job_id, console_safe(message))
        if part is not None:
            discard(part)
    finally:
        release_destination(claim)
