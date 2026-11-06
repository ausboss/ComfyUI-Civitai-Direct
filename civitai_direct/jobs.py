"""The job registry. A job's record is what the userscripts poll, and the
place every background failure is reported."""

import itertools
import shutil
from pathlib import Path
from typing import Any, Optional

from .text import redact_civitai_token

# States a job can end in. Anything else is still in progress: resolving,
# hashing_existing, downloading, hashing, extracting, writing_metadata.
TERMINAL_STATES = frozenset({"done", "already_exists", "error"})
MAX_FINISHED_JOBS = 200

JOBS: dict[int, dict[str, Any]] = {}
_job_ids = itertools.count(1)
_active_destinations: set[str] = set()


def create_job(**fields: Any) -> int:
    job_id = next(_job_ids)
    JOBS[job_id] = {"state": "resolving", **fields}
    _prune()
    return job_id


def _prune() -> None:
    """Forget the oldest finished jobs so a long session doesn't grow forever."""
    finished = [job_id for job_id, job in JOBS.items() if job["state"] in TERMINAL_STATES]
    for job_id in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
        del JOBS[job_id]


def claim_destination(path: Any) -> Optional[str]:
    """Reserve a destination so two jobs never write the same file. Returns a
    key to release, or None when another job already has it."""
    key = str(path).lower()
    if key in _active_destinations:
        return None
    _active_destinations.add(key)
    return key


def release_destination(key: Optional[str]) -> None:
    if key is not None:
        _active_destinations.discard(key)


def discard(path: Any) -> None:
    """Remove a leftover partial file or folder, ignoring any failure."""
    try:
        target = Path(path)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    except OSError:
        pass


def job_error_message(exc: BaseException) -> str:
    """Our own RuntimeErrors are written to be read as they are; anything else
    keeps its type name, which is what helps someone report it."""
    text = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
    return redact_civitai_token(text)
