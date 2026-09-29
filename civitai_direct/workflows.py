"""Workflow downloads: where they land, and safe ZIP extraction."""

import shutil
import zipfile
from pathlib import Path

from .text import safe_filename, safe_folder

MAX_NESTED_ZIP_DEPTH = 8
MAX_NESTED_ZIP_BYTES = 512 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000


def workflow_artifact_path(download_path: Path) -> Path:
    """A ZIP extracts into a folder named after it; anything else stays put."""
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
    """(download path, extracted folder), with " (1)", " (2)"... added until
    neither the download nor its result nor a partial file exists."""
    candidate = download_path
    index = 1
    while True:
        artifact = workflow_artifact_path(candidate)
        part = candidate.with_suffix(candidate.suffix + ".part")
        artifact_part = artifact.with_name(artifact.name + ".part")
        if not any(p.exists() for p in (candidate, artifact, part, artifact_part)):
            return candidate, artifact
        candidate = download_path.with_name(f"{download_path.stem} ({index}){download_path.suffix}")
        index += 1


def _safe_archive_members(archive: zipfile.ZipFile, extract_dir: Path) -> list[zipfile.ZipInfo]:
    """The archive's entries, after refusing anything that would escape
    extract_dir or that is unreasonably large."""
    members = archive.infolist()
    if len(members) > MAX_ZIP_ENTRIES:
        raise RuntimeError(f"ZIP has too many entries ({len(members)})")
    if sum(member.file_size for member in members) > MAX_ZIP_TOTAL_BYTES:
        raise RuntimeError("ZIP would extract to more than 4 GiB")
    resolved_extract_dir = extract_dir.resolve()
    for member in members:
        member_path = (extract_dir / member.filename).resolve()
        if member_path != resolved_extract_dir and resolved_extract_dir not in member_path.parents:
            raise RuntimeError(f"unsafe ZIP entry path: {member.filename}")
    return members


def _hoist_single_subdirectory(extract_dir: Path) -> None:
    """A ZIP that wraps everything in one folder gets that folder removed."""
    children = list(extract_dir.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        return
    inner_dir = children[0]
    for item in inner_dir.iterdir():
        shutil.move(str(item), str(extract_dir / item.name))
    inner_dir.rmdir()


def extract_workflow_zip(zip_path: Path, extract_dir: Path, depth: int = 0) -> None:
    if depth > MAX_NESTED_ZIP_DEPTH:
        raise RuntimeError("nested ZIP depth limit exceeded")

    with zipfile.ZipFile(zip_path) as archive:
        _safe_archive_members(archive, extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        archive.extractall(extract_dir)

    nested_zips = sorted(
        (p for p in extract_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".zip"),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for nested_zip in nested_zips:
        if nested_zip.stat().st_size > MAX_NESTED_ZIP_BYTES:
            raise RuntimeError(f"nested ZIP is too large to extract safely: {nested_zip.name}")
        nested_extract_dir = nested_zip.parent / safe_folder(nested_zip.stem, "workflow")
        extract_workflow_zip(nested_zip, nested_extract_dir, depth + 1)
        nested_zip.unlink()
        _hoist_single_subdirectory(nested_extract_dir)

    _hoist_single_subdirectory(extract_dir)
