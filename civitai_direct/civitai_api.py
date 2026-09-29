"""Talking to Civitai: URL parsing, the API calls, and gated-download errors."""

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote, urlparse

import aiohttp

from . import LOGGER_NAME, NODE_VERSION, REPO_URL
from .classify import (
    build_classification,
    normalize_base_model,
    pick_primary_model_file,
    pick_strongest_hash,
)
from .settings import get_api_key, get_api_key_with_source
from .text import log_safe, strip_html

log = logging.getLogger(LOGGER_NAME)

CIVITAI_MODEL_API = "https://civitai.com/api/v1/models/{model_id}"
CIVITAI_VERSION_API = "https://civitai.com/api/v1/model-versions/{version_id}"
CIVITAI_IMAGE_API = "https://civitai.com/api/v1/images"
CIVITAI_HOSTS = ("civitai.com", "civitai.red")


def is_civitai_url(url: Any) -> bool:
    """True only for http(s) URLs on Civitai's own domains. Anything a client
    sends us that we then fetch must pass this, so the API key can never be
    sent to another host."""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return any(host == domain or host.endswith("." + domain) for domain in CIVITAI_HOSTS)


def user_agent() -> str:
    return f"ComfyUI-Civitai-Direct/{NODE_VERSION} (+{REPO_URL})"


def auth_headers() -> dict[str, str]:
    headers = {"User-Agent": user_agent()}
    key = get_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def open_session() -> aiohttp.ClientSession:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    return aiohttp.ClientSession(headers=auth_headers(), timeout=timeout)


def add_token_to_url(url: str) -> str:
    """Append ?token= on Civitai hosts. The download endpoint has historically
    honored the query token more reliably than the Authorization header, so
    downloads send both."""
    key = get_api_key()
    if not key or not is_civitai_url(url):
        return url
    return f"{url}{'&' if '?' in url else '?'}token={quote(key)}"


def _query_int(url: str, key: str) -> Optional[int]:
    match = re.search(rf"[?&]{re.escape(key)}=(\d+)", url or "")
    return int(match.group(1)) if match else None


def parse_ids_from_url(url: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """(model_id, model_version_id, model_slug) parsed from any Civitai URL."""
    url = url or ""
    version_id = _query_int(url, "modelVersionId")
    if version_id is None:
        match = re.search(r"/api/download/models/(\d+)", url)
        version_id = int(match.group(1)) if match else None

    model_id = None
    slug = None
    # The number in /api/download/models/N is a VERSION id, not a model id;
    # only page-style /models/N URLs carry the model id.
    match = re.search(r"(?<!/api/download)/models/(\d+)(?:/([^/?#]+))?", url)
    if match:
        model_id = int(match.group(1))
        slug = match.group(2)
    return model_id, version_id, slug


def parse_image_id_from_url(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    try:
        match = re.search(r"/images/(\d+)", urlparse(url).path)
        return int(match.group(1)) if match else None
    except ValueError:
        return None


async def fetch_json(session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    async with session.get(url) as resp:
        if resp.status in (401, 403):
            raise RuntimeError(
                f"Civitai returned {resp.status} for {url}. The model may need a login: "
                "set an API key (CIVITAI_API_KEY or civitai_api_key.txt)."
            )
        if resp.status == 404:
            raise RuntimeError(f"Civitai has nothing at {url} (404). Check the model or version id.")
        resp.raise_for_status()
        return await resp.json()


async def resolve_model(
    session: aiohttp.ClientSession,
    model_id: Optional[int],
    version_id: Optional[int],
    slug: Optional[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(model_json, version_json, meta) from whatever ids we have."""
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
    # every shared key: availability, index and covered exist nowhere else,
    # and availability drives the Early Access message.
    if detail_json is None or detail_json.get("id") != version_json.get("id"):
        detail_json = None
        try:
            detail_json = await fetch_json(
                session, CIVITAI_VERSION_API.format(version_id=version_json.get("id"))
            )
        except Exception as exc:  # noqa: BLE001 -- extra detail, never a blocker
            log.info("civitai-direct: version %s detail unavailable (%s)",
                     version_json.get("id"), log_safe(exc))
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


async def probe_gate_reason(session: aiohttp.ClientSession, url: str) -> str:
    """Ask the download endpoint why it refused, as JSON.

    Civitai answers a gated download with a JSON body ({"error": "Early
    Access", "deadline": ...}) when asked as an API client, but with a
    redirect to a login page when asked like a browser. Returns '' on anything
    unexpected: this only decorates an error we already have."""
    try:
        async with session.get(
            url, headers={"Accept": "application/json"}, allow_redirects=False,
        ) as resp:
            if resp.status in (401, 403):
                return (await resp.text())[:2000]
    except Exception:  # noqa: BLE001 -- best-effort diagnostics
        pass
    return ""


def gated_error(detail: str, body_text: str, availability: Optional[str]) -> RuntimeError:
    """A readable error for a refused download.

    The models API reports Early Access versions as "Public", so the download
    endpoint's own answer is the only honest source for that state."""
    body: dict[str, Any] = {}
    try:
        parsed = json.loads(body_text) if body_text else None
        if isinstance(parsed, dict):
            body = parsed
    except ValueError:
        pass

    key, source = get_api_key_with_source()
    api_error = str(body.get("error") or "")
    if availability == "EarlyAccess" or "early access" in api_error.lower():
        deadline = str(body.get("deadline") or "")[:10]
        when = (
            f"It unlocks for everyone on {deadline}."
            if deadline
            else "It becomes downloadable when the early-access window ends."
        )
        return RuntimeError(
            f"{detail}: this version is in Early Access, which needs the early-access "
            f"purchase or subscription on your Civitai account. An API key alone does "
            f"not unlock it. {when}"
        )
    if not key:
        return RuntimeError(
            f"{detail}: Civitai requires a login to download this model and no API key is "
            "set. Create one at civitai.com/user/account and add it as CIVITAI_API_KEY "
            "(see the README), then try again."
        )
    return RuntimeError(
        f"{detail}: Civitai refused the API key from {source}. Check that it is valid at "
        "civitai.com/user/account and that your account can download this model in the browser."
    )
