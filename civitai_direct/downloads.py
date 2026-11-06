"""One model or workflow download, from resolving the ids to writing sidecars.

`ModelDownload.run` reads as the pipeline: resolve, look for what is already
here, download, verify, extract (workflows), write sidecars. Every failure
lands in the job record, which is what the userscripts show.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from . import LOGGER_NAME
from .civitai_api import (
    CHUNK,
    add_token_to_url,
    gated_error,
    get,
    open_session,
    probe_gate_reason,
    resolve_model,
)
from .classify import (
    is_wan_model,
    normalize_model_type,
    pick_primary_model_file,
    slug_aware_filename,
)
from .jobs import JOBS, claim_destination, discard, job_error_message, release_destination
from .library import calculate_sha256, find_downloaded_version, find_same_named_files, invalidate_listings
from .paths import choose_target_dir, search_roots, workflow_metadata_dir, workflow_target_dir
from .settings import sidecar_mode
from .sidecars import (
    build_civitai_info,
    build_sidecar_json,
    civitai_info_path,
    save_preview_image,
    write_json,
    write_markdown,
)
from .text import console_safe, format_size, safe_filename
from .workflows import (
    extract_workflow_zip,
    unique_workflow_destination,
    workflow_sidecar_json_path,
    workflow_sidecar_markdown_path,
)

log = logging.getLogger(LOGGER_NAME)


def unique_destination_path(path: Path) -> Path:
    """path, or "name (1).ext", "name (2).ext"... whichever is free."""
    candidate = path
    index = 1
    while candidate.exists():
        candidate = path.parent / f"{path.stem} ({index}){path.suffix}"
        index += 1
    return candidate


class ModelDownload:
    def __init__(self, job_id: int, model_id: Optional[int], version_id: Optional[int],
                 slug: Optional[str], user_id: str):
        self.job_id = job_id
        self.job = JOBS[job_id]
        self.model_id = model_id
        self.version_id = version_id
        self.slug = slug
        self.user_id = user_id

        # Set by run() and _resolve(), listed here so the whole state is visible.
        self.session: Any = None
        self.model_json: dict[str, Any] = {}
        self.version_json: dict[str, Any] = {}
        self.meta: dict[str, Any] = {}
        self.model_type = ""
        self.is_workflow = False
        self.file: dict[str, Any] = {}
        self.download_url = ""
        self.api_sha256 = ""
        self.target_dir = Path()
        self.metadata_dir: Optional[Path] = None
        self.roots: list[Path] = []

        self.dest = Path()                      # where the download is written
        self.workflow_folder: Optional[Path] = None  # where a workflow ZIP extracts to
        self.artifact = Path()                  # the finished file or folder
        self.file_sha256 = ""
        self.file_size = 0
        self.sidecar_names: list[str] = []

        self.part: Optional[Path] = None
        self.extraction_part: Optional[Path] = None
        self.claim: Optional[str] = None
        self._hashes: dict[str, str] = {}

    # ---- the pipeline ----------------------------------------------------

    async def run(self) -> None:
        try:
            async with open_session() as session:
                self.session = session
                await self._resolve()
                if await self._already_downloaded():
                    return
                if await self._identical_file_present():
                    return
                if await self._download():
                    return
                await self._verify()
                await self._extract_workflow()
                await self._write_sidecars()
                invalidate_listings()
                self.job["state"] = "done"
                log.info("civitai-direct: job %s: done -> %s (+ %s)", self.job_id,
                         console_safe(self.artifact), "/".join(self.sidecar_names) or "no sidecars")
        except Exception as exc:  # noqa: BLE001 -- the job record is the error channel
            message = job_error_message(exc)
            self.job.update(state="error", error=message)
            log.warning("civitai-direct: job %s: failed: %s", self.job_id, console_safe(message))
            if self.part is not None:
                discard(self.part)
            if self.extraction_part is not None:
                discard(self.extraction_part)
        finally:
            release_destination(self.claim)

    # ---- resolving -------------------------------------------------------

    async def _resolve(self) -> None:
        self.model_json, self.version_json, self.meta = await resolve_model(
            self.session, self.model_id, self.version_id, self.slug
        )
        meta = self.meta
        self.job.update(
            model_id=meta["model_id"],
            model_version_id=meta["model_version_id"],
            model_name=meta["model_name"],
            version_name=meta["version_name"],
            type=meta["type"],
            base_model=meta["base_model"],
        )

        self.model_type = normalize_model_type(meta["type"])
        self.is_workflow = self.model_type == "Workflow"
        log.info(
            "civitai-direct: job %s: %s (%s) -- %s / %s [model %s, version %s]",
            self.job_id, console_safe(meta["model_name"]), console_safe(meta["version_name"] or "?"),
            self.model_type, console_safe(meta["base_model"]), meta["model_id"], meta["model_version_id"],
        )

        self.file = pick_primary_model_file(self.version_json)
        if self.file is None:
            raise RuntimeError("this version has no downloadable files")
        self.download_url = self.file.get("downloadUrl") or self.version_json.get("downloadUrl")
        if not self.download_url:
            raise RuntimeError("this version has no download URL")
        self.api_sha256 = ((self.file.get("hashes") or {}).get("SHA256") or "").strip().lower()

        haystack = " ".join(filter(None, [meta.get("model_name"), meta.get("version_name"), self.file.get("name")]))
        if self.is_workflow:
            self.target_dir = workflow_target_dir(self.user_id, meta["base_model"])
            self.metadata_dir = workflow_metadata_dir(self.user_id, meta["base_model"])
            self.roots = [self.metadata_dir, self.target_dir]
        else:
            self.target_dir = choose_target_dir(self.model_type, meta["base_model"], haystack)
            self.metadata_dir = None
            # Every folder registered for this type, not only the one new files
            # go to: files the user keeps elsewhere or reorganized still count.
            self.roots = search_roots(self.model_type, meta["base_model"], haystack)

        self.artifact = self.target_dir

    # ---- what is already here --------------------------------------------

    async def _already_downloaded(self) -> bool:
        """First duplicate layer: a sidecar names this exact version."""
        existing = await asyncio.to_thread(
            find_downloaded_version, self.roots, self.meta["model_version_id"], self.meta["model_id"]
        )
        if not existing:
            return False
        self.job.update(state="already_exists", path=str(existing))
        # A model found through a legacy .json record gains the ecosystem's
        # .civitai.info (and a preview), so old libraries converge on the
        # format the loader panels read.
        if not self.is_workflow and existing.is_file():
            self._write_info_if_missing(existing)
            await save_preview_image(self.session, self.version_json, existing, self.job_id)
            invalidate_listings()
        log.info("civitai-direct: job %s: already downloaded -> %s", self.job_id, console_safe(existing))
        return True

    async def _same_bytes(self, path: Path) -> bool:
        """Does this file have the SHA256 Civitai publishes for the version?"""
        if not self.api_sha256 or not path.is_file():
            return False
        sha = self._hashes.get(str(path))
        if sha is None:
            self.job["state"] = "hashing_existing"
            sha = await calculate_sha256(path)
            self._hashes[str(path)] = sha
        return sha.lower() == self.api_sha256

    def _expected_name(self) -> str:
        if not self.is_workflow and is_wan_model(self.meta):
            return slug_aware_filename(self.meta)
        return safe_filename(self.file.get("name") or "") or slug_aware_filename(self.meta)

    async def _identical_file_present(self) -> bool:
        """Second duplicate layer, for files this tool didn't download: the
        same name on disk with the SHA256 Civitai lists means the file is
        already here. It is adopted (sidecars backfilled) instead of saved as
        "name (1)". The same name with a DIFFERENT hash is a genuine collision,
        since versions often reuse filenames, and still gets the suffix."""
        if self.is_workflow or not self.api_sha256:
            return False
        candidates = await asyncio.to_thread(find_same_named_files, self.roots, self._expected_name())
        for candidate in candidates:
            if await self._same_bytes(candidate):
                await self._adopt(candidate)
                return True
        return False

    def _write_info_if_missing(self, path: Path) -> None:
        info_path = civitai_info_path(path)
        if not info_path.exists():
            write_json(info_path, build_civitai_info(self.model_json, self.version_json, self.meta))

    async def _adopt(self, path: Path) -> None:
        self.job.update(state="already_exists", path=str(path))
        self._write_info_if_missing(path)
        if sidecar_mode() == "full":
            json_path = path.with_suffix(path.suffix + ".json")
            if not json_path.exists():
                sidecar = build_sidecar_json(
                    self.meta, {"model": self.model_json, "model_version": self.version_json},
                    path, self.download_url, self._hashes.get(str(path)), path.stat().st_size,
                    "already_exists",
                )
                write_json(json_path, sidecar)
                write_markdown(path.with_suffix(path.suffix + ".md"), sidecar)
        invalidate_listings()
        log.info("civitai-direct: job %s: identical file already on disk, adopted -> %s",
                 self.job_id, console_safe(path))

    # ---- downloading -----------------------------------------------------

    async def _download(self) -> bool:
        """Stream the file to `<name>.part`, then rename it. True when the
        served name turned out to be a file we already have (nothing saved)."""
        self.job["state"] = "downloading"
        availability = self.version_json.get("availability")

        async with get(self.session, add_token_to_url(self.download_url)) as resp:
            if resp.status in (401, 403):
                try:
                    body_text = (await resp.text())[:2000]
                except Exception:  # noqa: BLE001 -- the status alone still errors fine
                    body_text = ""
                if not body_text.lstrip().startswith("{"):
                    body_text = await probe_gate_reason(self.session, self.download_url) or body_text
                raise gated_error(f"Civitai returned {resp.status}", body_text, availability)
            resp.raise_for_status()
            if "text/html" in resp.headers.get("Content-Type", ""):
                raise gated_error(
                    "Civitai returned a web page instead of the file",
                    await probe_gate_reason(self.session, self.download_url),
                    availability,
                )

            served_name = None
            if resp.content_disposition and resp.content_disposition.filename:
                served_name = safe_filename(resp.content_disposition.filename)
            filename = served_name or safe_filename(self.file.get("name") or "") or slug_aware_filename(self.meta)
            if not self.is_workflow and is_wan_model(self.meta):
                filename = slug_aware_filename(self.meta)

            self.target_dir.mkdir(parents=True, exist_ok=True)
            if self.is_workflow:
                self.dest, self.workflow_folder = unique_workflow_destination(self.target_dir / filename)
            else:
                self.dest = self.target_dir / filename
                if self.dest.exists():
                    # The served filename can differ from the API name, so this
                    # can be a path the earlier check never looked at.
                    if await self._same_bytes(self.dest):
                        await self._adopt(self.dest)
                        return True
                    self.dest = unique_destination_path(self.dest)

            self.claim = claim_destination(self.workflow_folder or self.dest)
            if self.claim is None:
                raise RuntimeError("this file is already being downloaded")

            self.part = self.dest.with_suffix(self.dest.suffix + ".part")
            self.job["path"] = str(self.workflow_folder or self.dest)
            total = resp.headers.get("Content-Length")
            self.job["bytes_total"] = int(total) if total else None
            log.info("civitai-direct: job %s: downloading %s -> %s", self.job_id,
                     format_size(self.job["bytes_total"]) or "unknown size", console_safe(self.dest))

            with self.part.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    fh.write(chunk)
                    self.job["bytes_done"] += len(chunk)

        self.part.replace(self.dest)
        self.part = None
        self.artifact = self.dest
        return False

    async def _verify(self) -> None:
        """The hash Civitai publishes doubles as an integrity check. A mismatch
        is a truncated or corrupted transfer and must not sit on disk looking
        like a model."""
        self.job["state"] = "hashing"
        self.file_sha256 = await calculate_sha256(self.dest)
        self.file_size = self.dest.stat().st_size
        if self.api_sha256 and self.file_sha256.lower() != self.api_sha256:
            quarantined = self.dest.parent / (self.dest.name + ".corrupt")
            self.dest.replace(quarantined)
            raise RuntimeError(
                f"SHA256 mismatch: Civitai publishes {self.api_sha256} but the downloaded "
                f"bytes hash to {self.file_sha256.lower()}. The file was kept as "
                f"{quarantined.name} instead of a trusted model file. Try the download again."
            )

    async def _extract_workflow(self) -> None:
        if not (self.is_workflow and self.dest.suffix.lower() == ".zip"):
            return
        if self.workflow_folder is None:
            raise RuntimeError("could not determine workflow extraction directory")
        self.job["state"] = "extracting"
        self.extraction_part = self.workflow_folder.with_name(self.workflow_folder.name + ".part")
        await asyncio.to_thread(extract_workflow_zip, self.dest, self.extraction_part)
        self.extraction_part.replace(self.workflow_folder)
        self.extraction_part = None
        self.dest.unlink()
        self.artifact = self.workflow_folder
        self.job["path"] = str(self.artifact)

    # ---- sidecars --------------------------------------------------------

    async def _write_sidecars(self) -> None:
        self.job["state"] = "writing_metadata"
        api_raw = {"model": self.model_json, "model_version": self.version_json}

        if self.is_workflow:
            json_path = workflow_sidecar_json_path(self.metadata_dir, self.artifact)
            markdown_path = workflow_sidecar_markdown_path(self.metadata_dir, self.artifact)
        else:
            json_path = self.dest.with_suffix(self.dest.suffix + ".json")
            markdown_path = self.dest.with_suffix(self.dest.suffix + ".md")
            write_json(civitai_info_path(self.artifact),
                       build_civitai_info(self.model_json, self.version_json, self.meta))
            self.sidecar_names.append(".civitai.info")
            if await save_preview_image(self.session, self.version_json, self.artifact, self.job_id):
                self.sidecar_names.append(".preview")

        # Workflows always keep the v1 json+md pair: it is the workflow's
        # duplicate-detection record, and .civitai.info is a model-file
        # convention other tools would not look for on a workflow.
        if self.is_workflow or sidecar_mode() == "full":
            sidecar = build_sidecar_json(
                self.meta, api_raw, self.artifact, self.download_url, self.file_sha256, self.file_size,
                "downloaded", json_file_path=json_path, markdown_file_path=markdown_path,
            )
            write_json(json_path, sidecar)
            write_markdown(markdown_path, sidecar)
            self.sidecar_names += [".json", ".md"]
