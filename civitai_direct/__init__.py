"""Civitai Direct: the code behind the /civitai/* routes.

Nothing here imports ComfyUI except `paths` and `routes`, so the rest can be
tested with a plain Python interpreter.
"""

import re
from pathlib import Path

LOGGER_NAME = "civitai-direct"
REPO_URL = "https://github.com/ausboss/ComfyUI-Civitai-Direct"


def _read_version() -> str:
    """The version in pyproject.toml, so there is one place to bump it."""
    try:
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else "unknown"


NODE_VERSION = _read_version()
