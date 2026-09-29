"""The files written next to a download: `.civitai.info`, the preview image,
and this tool's own `.json` + `.md` record."""

import json
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp

from . import LOGGER_NAME
from .civitai_api import is_civitai_url
from .jobs import discard
from .settings import previews_mode
from .text import format_size, log_safe, now_iso, redact_civitai_token

log = logging.getLogger(LOGGER_NAME)

SIDECAR_SCHEMA = "ausboss-civitai-model-sidecar-v1"  # shared with easy-civitai-downloader
CHUNK = 1 << 20  # 1 MiB per event-loop turn keeps the server responsive
_PREVIEW_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ---- <stem>.civitai.info ------------------------------------------------

def civitai_info_path(model_path: Path) -> Path:
    """model.safetensors -> model.civitai.info, the name the wider ecosystem shares."""
    return model_path.with_suffix(".civitai.info")


def build_civitai_info(
    model_json: dict[str, Any], version_json: dict[str, Any], meta: dict[str, Any]
) -> dict[str, Any]:
    """The model-version API response, verbatim. The only additions are the
    top-level keys the model endpoint's embedded copy can drop, filled in so
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


# ---- <stem>.preview.<ext> -----------------------------------------------

def pick_preview_image(version_json: dict[str, Any]) -> Optional[dict[str, Any]]:
    mode = previews_mode()
    if mode == "off":
        return None
    images = [
        entry for entry in (version_json.get("images") or [])
        if isinstance(entry, dict) and (entry.get("type") or "image") == "image"
        and is_civitai_url(entry.get("url"))
    ]
    if not images:
        return None
    if mode == "any":
        return images[0]
    # Per-image nsfwLevel is a single browsing level: 1 PG, 2 PG13, 4 R, ...
    tamest = min(images, key=lambda entry: entry.get("nsfwLevel") or 99)
    return tamest if (tamest.get("nsfwLevel") or 99) <= 2 else None


def preview_path_for(model_path: Path, image_url: str) -> Path:
    """<stem>.preview.<ext>: the name A1111/Forge, Civitai Helper and Lora
    Manager read, which leaves <stem>.<ext> free for a preview the user makes
    themselves (those tools prefer that one)."""
    ext = Path(urlparse(image_url).path).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        ext = ".jpeg"
    return model_path.with_suffix(".preview" + ext)


def image_suffix_from_magic(path: Path) -> Optional[str]:
    """Extension for what the file really is. Civitai's CDN routinely serves
    PNG originals from URLs ending in .jpeg, so name by the bytes."""
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return None
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    return None


async def save_preview_image(
    session: aiohttp.ClientSession,
    version_json: dict[str, Any],
    model_path: Path,
    job_id: int,
) -> bool:
    """Best-effort preview next to the model. Never fails the job."""
    entry = pick_preview_image(version_json)
    if not entry:
        return False
    if any(model_path.with_suffix(".preview" + suffix).exists() for suffix in _PREVIEW_SUFFIXES):
        return True
    dest = preview_path_for(model_path, entry["url"])
    part = dest.parent / (dest.name + ".part")
    try:
        async with session.get(entry["url"]) as resp:
            resp.raise_for_status()
            with part.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    fh.write(chunk)
        true_suffix = image_suffix_from_magic(part)
        if true_suffix:
            dest = model_path.with_suffix(".preview" + true_suffix)
        part.replace(dest)
        return True
    except Exception as exc:  # noqa: BLE001 -- previews are decoration, not data
        log.info("civitai-direct: job %s: preview image failed (%s)", job_id, log_safe(exc))
        discard(part)
        return False


# ---- <file>.json + <file>.md (CIVITAI_SIDECARS=full, and always for workflows)

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
            # AutoV2 is what A1111 shows, AutoV3 what Civitai's UI shows.
            # Both let other tools match this file without rehashing it.
            "api_hashes": meta.get("api_hashes") or {},
        },
        "api_raw": api_raw,
    }


def classification_lines(payload: dict[str, Any]) -> list[str]:
    """The '## Classification' block: stable `- **Key:** value` bullets, so the
    note greps as easily as the JSON parses."""
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
