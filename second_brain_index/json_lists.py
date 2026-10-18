"""Reading and creating the JSON topic lists behind OneNote, Outlook and PLM."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .constants import STATE_FOLDERS


def parse_topics_json(content: str) -> Any:
    """Parse a topic list, forgiving the trailing commas a hand edit leaves behind."""
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        try:
            return json.loads(strip_trailing_commas(content))
        except json.JSONDecodeError:
            # The first error's position is the one that matches the file.
            raise error from None


def strip_trailing_commas(content: str) -> str:
    """Drop every comma that is directly followed by a closing bracket or brace."""
    kept: list[str] = []
    in_string = False
    escaped = False
    for index, character in enumerate(content):
        if in_string:
            kept.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == ",":
            rest = content[index + 1 :].lstrip()
            if rest[:1] in ("]", "}"):
                continue
        kept.append(character)
    return "".join(kept)


def write_text_atomic(path: Path, text: str) -> None:
    """Replace a file's content in one step, or leave it exactly as it was.

    Writing in place truncates first, so a full disk or a crash mid-write would
    leave the user's only copy of a list cut short. The new text goes to a
    sibling file that takes the original's place once it is complete.
    """
    # Through a symlink, so the link survives and its target is what changes.
    target = Path(os.path.realpath(path))
    if not target.exists():
        target.write_text(text, encoding="utf-8")
        return
    handle, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(target, temporary)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def empty_topics_document() -> dict[str, list[dict[str, Any]]]:
    """A list with one empty section per PARA folder."""
    return {
        "sections": [
            {"name": folder_name, "topics": []}
            for folder_name in STATE_FOLDERS.values()
        ]
    }
