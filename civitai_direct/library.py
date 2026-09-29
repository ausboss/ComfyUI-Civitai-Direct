"""Finding what is already on disk, so nothing is downloaded twice.

A version counts as present when a sidecar names it (a `.civitai.info` from
any tool, or this tool's own `<file>.json` record), or when a same-named file
has the SHA256 Civitai publishes for it. Scans follow symlinked folders and
subfolders, because libraries get reorganized.
"""

import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

# Extensions a model file next to a sidecar can carry, most likely first.
MODEL_FILE_SUFFIXES = (
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".zip",
)

# Scans cover whole type folders, which can hold arbitrary JSON from other
# tools. Don't slurp anything that is clearly not a sidecar.
MAX_SIDECAR_BYTES = 8 * 1024 * 1024

_CACHE_LIMIT = 50_000
_cache: dict[str, tuple[int, int, Optional[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def iter_files(root: Path) -> Iterator[Path]:
    """Every file under root. Symlinked folders are followed, and a folder is
    visited once even when links loop back."""
    seen: set[str] = set()
    stack = [str(root)]
    while stack:
        directory = stack.pop()
        try:
            real = os.path.realpath(directory)
            if real in seen:
                continue
            seen.add(real)
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    stack.append(entry.path)
                elif entry.is_file():
                    yield Path(entry.path)
            except OSError:
                continue


def _is_v1_sidecar_name(name: str) -> bool:
    """`<model file>.json` or `<workflow>.civitai.json`, never a `.failed` record."""
    if not name.endswith(".json") or name.endswith((".failed.json", ".failed.civitai.json")):
        return False
    if name.endswith(".civitai.json"):
        return True
    return Path(name[: -len(".json")]).suffix.lower() in MODEL_FILE_SUFFIXES


def _summarize(payload: Any, kind: str) -> Optional[dict[str, Any]]:
    """The few fields the duplicate check needs, from either dialect."""
    if not isinstance(payload, dict):
        return None
    if kind == "info":
        return {
            "kind": "info",
            "version_id": str(payload.get("id")),
            "model_id": str(payload["modelId"]) if payload.get("modelId") else None,
        }
    if payload.get("status") == "failed":
        return None
    model = payload.get("model") or {}
    local = payload.get("local") or {}
    return {
        "kind": "v1",
        "version_id": str(model.get("version_id")),
        "model_id": str(model["id"]) if model.get("id") else None,
        "model_file": local.get("model_file"),
        "model_file_name": local.get("model_file_name"),
    }


def _read_sidecar(path: Path, kind: str) -> Optional[dict[str, Any]]:
    """Parsed summary of one sidecar, cached until the file changes."""
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > MAX_SIDECAR_BYTES:
        return None
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        record = _summarize(json.loads(path.read_text(encoding="utf-8")), kind)
    except (OSError, ValueError):
        record = None
    with _cache_lock:
        if len(_cache) >= _CACHE_LIMIT:
            _cache.clear()
        _cache[key] = (st.st_mtime_ns, st.st_size, record)
    return record


def _model_file_for(path: Path, record: dict[str, Any]) -> Optional[Path]:
    if record["kind"] == "info":
        # The model file shares the sidecar's stem. Probe extensions instead
        # of globbing: model names can contain glob metacharacters.
        stem = path.name[: -len(".civitai.info")]
        for suffix in MODEL_FILE_SUFFIXES:
            candidate = path.parent / (stem + suffix)
            if candidate.exists():
                return candidate
        return None

    recorded = record.get("model_file")
    if recorded and Path(str(recorded)).exists():
        return Path(str(recorded))
    # The file may have been moved (with its sidecars), leaving the recorded
    # absolute path stale. Trust a copy sitting next to the sidecar.
    moved_name = record.get("model_file_name") or (Path(str(recorded)).name if recorded else None)
    if moved_name:
        moved = path.parent / str(moved_name)
        if moved.exists():
            return moved
    return None


def find_downloaded_version(
    roots: Iterable[Path], model_version_id: Any, model_id: Any = None
) -> Optional[Path]:
    """The existing model file for this exact version, found through its sidecar."""
    if not model_version_id:
        return None
    version = str(model_version_id)
    model = str(model_id) if model_id else None

    for root in roots:
        if not root.is_dir():
            continue
        for path in iter_files(root):
            name = path.name
            if name.endswith(".civitai.info"):
                kind = "info"
            elif _is_v1_sidecar_name(name):
                kind = "v1"
            else:
                continue
            record = _read_sidecar(path, kind)
            if not record or record["version_id"] != version:
                continue
            if model and record["model_id"] and record["model_id"] != model:
                continue
            found = _model_file_for(path, record)
            if found:
                return found
    return None


def find_same_named_files(roots: Iterable[Path], filename: str, limit: int = 5) -> list[Path]:
    """Files called `filename` anywhere under the roots, subfolders included,
    so a model that was reorganized is still recognized. Bounded, because
    every match may get hashed."""
    if not filename:
        return []
    wanted = filename.lower()
    matches: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in iter_files(root):
            if path.name.lower() == wanted:
                matches.append(path)
                if len(matches) >= limit:
                    return matches
    return matches


async def calculate_sha256(path: Path) -> str:
    def _hash() -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    return await asyncio.to_thread(_hash)
