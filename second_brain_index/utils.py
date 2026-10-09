from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse


def infer_link_type(url: str) -> str:
    value = url.strip()
    if not value:
        return "other"

    lowered = value.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "web"
    if lowered.startswith("mailto:"):
        return "email"
    if lowered.startswith("obsidian://"):
        return "obsidian"
    if lowered.startswith("file://"):
        return "file"

    parsed = urlparse(value)
    if parsed.scheme:
        return parsed.scheme

    if value.startswith("/") or value.startswith("~"):
        return "file"
    if re.match(r"^[a-zA-Z]:\\", value):
        return "file"

    if Path(value).exists():
        return "file"

    return "other"
