"""Small text helpers: file-safe names, HTML stripping, log-safe output."""

import html
import re
from datetime import datetime, timezone
from typing import Any, Optional

# Names Windows refuses to create, with or without an extension.
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(value: str, fallback: str = "model") -> str:
    """One path component that is valid on Windows, macOS and Linux."""
    value = html.unescape(value or "").strip()
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.rstrip(". ")
    if value.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
        value = f"_{value}"
    return value or fallback


def safe_folder(value: Optional[str], fallback: str = "Unknown") -> str:
    return safe_filename(value or fallback, fallback=fallback)


def sanitize_filename_component(value: Optional[str], fallback: str = "model", max_length: int = 120) -> str:
    value = html.unescape(value or fallback)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"[^\w.-]+", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip("_")
    value = value[:max_length].strip("_")
    return value or fallback


def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    value = re.sub(r"</p\s*>", "\n\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def format_size(size_bytes: Optional[float]) -> str:
    if not size_bytes:
        return ""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def redact_civitai_token(text: Any) -> str:
    return re.sub(r"([?&]token=)[^&\s]+", r"\1REDACTED", str(text or ""))


def console_safe(value: Any) -> str:
    """ASCII-only rendering for console output. Civitai model names routinely
    carry CJK or emoji that crash a cp1252 Windows console when printed raw;
    this keeps ASCII readable and escapes the rest."""
    return str(value if value is not None else "").encode("ascii", "backslashreplace").decode("ascii")


def log_safe(value: Any) -> str:
    """console_safe plus token redaction, for anything that may carry a URL."""
    return console_safe(redact_civitai_token(value))
