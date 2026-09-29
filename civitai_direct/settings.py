"""Settings and the API key.

Everything is read from the environment first, then from a `.env` file in the
node's folder. Values are re-read on every call, so a change applies to the
next download without restarting ComfyUI.
"""

import os
from pathlib import Path
from typing import Optional

NODE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = NODE_DIR / ".env"
KEY_FILE = NODE_DIR / "civitai_api_key.txt"


def _env_file_value(path: Path, name: str) -> str:
    """One NAME=value out of a dotenv-style file."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def get_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    if ENV_FILE.is_file():
        return _env_file_value(ENV_FILE, name)
    return ""


def get_api_key_with_source() -> tuple[str, Optional[str]]:
    """(key, where it came from): environment, .env, then civitai_api_key.txt."""
    key = os.getenv("CIVITAI_API_KEY", "").strip()
    if key:
        return key, "environment"
    if ENV_FILE.is_file():
        key = _env_file_value(ENV_FILE, "CIVITAI_API_KEY")
        if key:
            return key, ".env"
    if KEY_FILE.is_file():
        try:
            key = KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if key:
            return key, "civitai_api_key.txt"
    return "", None


def get_api_key() -> str:
    return get_api_key_with_source()[0]


def _choice(name: str, allowed: set[str], default: str) -> str:
    value = get_setting(name).strip().lower()
    return value if value in allowed else default


def sidecar_mode() -> str:
    """CIVITAI_SIDECARS: "info" writes <stem>.civitai.info next to each model;
    "full" also writes this tool's <file>.json + .md notes."""
    return _choice("CIVITAI_SIDECARS", {"info", "full"}, "info")


def previews_mode() -> str:
    """CIVITAI_PREVIEWS: "sfw" saves the tamest image and skips models with
    nothing at PG/PG13, "any" takes Civitai's cover, "off" saves none."""
    return _choice("CIVITAI_PREVIEWS", {"sfw", "any", "off"}, "sfw")


def base_folders_enabled() -> bool:
    """CIVITAI_BASE_FOLDERS: "on" sorts models into <type>/<base model>/,
    "off" saves them straight into <type>/."""
    return _choice("CIVITAI_BASE_FOLDERS", {"on", "off"}, "on") == "on"
