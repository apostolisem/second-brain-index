"""Reading and creating the JSON topic lists behind OneNote, Outlook and PLM."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from .constants import PREFIX_TO_STATE, STATE_FOLDERS


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


class TopicListError(Exception):
    """A topic list that could not be read; each source has its own subclass."""


def load_topic_list(path: Path, error_class: type[TopicListError]) -> dict[str, list[str]]:
    """Topic name to the paths a list gives it; {} when the list does not exist."""
    if not path.exists():
        return {}
    try:
        # utf-8-sig: Windows editors like to prepend a byte order mark.
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise error_class(f"Unable to read {path.name}: {exc}") from exc
    try:
        data = parse_topics_json(content)
    except json.JSONDecodeError as exc:
        raise error_class(f"Invalid JSON in {path.name}: {exc}") from exc
    topics = _extract_topics(data)
    return {name: sorted(paths, key=str.casefold) for name, paths in topics.items()}


def infer_states_from_paths(paths: list[str], topic_name: str) -> set[str]:
    states: set[str] = set()
    for path in paths:
        state = _state_from_path(path)
        if state:
            states.add(state)
    if not states:
        for prefix, state in PREFIX_TO_STATE.items():
            if topic_name.startswith(prefix):
                states.add(state)
                break
    return states


def _extract_topics(data: Any) -> dict[str, set[str]]:
    topics: dict[str, set[str]] = defaultdict(set)
    if isinstance(data, list):
        _parse_topic_entries(data, topics, None)
        return topics
    if isinstance(data, dict):
        if "topics" in data:
            _parse_topic_entries(data.get("topics"), topics, None)
        if "sections" in data:
            _parse_sections(data.get("sections"), topics, None)
        if not topics:
            for key, value in data.items():
                if isinstance(value, list):
                    _parse_topic_entries(value, topics, key)
                elif isinstance(value, dict):
                    _parse_section_like(value, topics, key)
        return topics
    return topics


def _parse_section_like(section: Any, topics: dict[str, set[str]], prefix: str | None) -> None:
    if not isinstance(section, dict):
        return
    if "topics" in section:
        _parse_topic_entries(section.get("topics"), topics, prefix)
    if "sections" in section:
        _parse_sections(section.get("sections"), topics, prefix)


def _parse_sections(sections: Any, topics: dict[str, set[str]], prefix: str | None) -> None:
    if not isinstance(sections, list):
        return
    for section in sections:
        if not isinstance(section, dict):
            continue
        name = _clean_string(section.get("name") or section.get("section"))
        section_prefix = _join_path(prefix, name) if name else prefix
        _parse_topic_entries(section.get("topics"), topics, section_prefix)
        _parse_sections(section.get("sections"), topics, section_prefix)


def _parse_topic_entries(entries: Any, topics: dict[str, set[str]], prefix: str | None) -> None:
    if not isinstance(entries, list):
        return
    for entry in entries:
        if isinstance(entry, str):
            _add_topic(topics, entry, None, prefix)
        elif isinstance(entry, dict):
            name = _clean_string(entry.get("name") or entry.get("topic") or entry.get("title"))
            if not name:
                continue
            path_value = _clean_string(entry.get("path"))
            _add_topic(topics, name, path_value, prefix)


def _add_topic(
    topics: dict[str, set[str]],
    name: str,
    path_value: str | None,
    prefix: str | None,
) -> None:
    cleaned = _clean_string(name)
    if not cleaned:
        return
    if path_value:
        resolved = path_value
    else:
        resolved = _join_path(prefix, cleaned) if prefix else cleaned
    if resolved:
        topics[cleaned].add(resolved)


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _join_path(prefix: str | None, name: str) -> str:
    if prefix:
        return f"{prefix} / {name}"
    return name


def _state_from_path(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    parts = [part.strip() for part in normalized.split("/") if part.strip()]
    if not parts:
        return None
    first = parts[0]
    for state, folder_name in STATE_FOLDERS.items():
        if first.casefold() == folder_name.casefold():
            return state
    return None
