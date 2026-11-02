from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from .constants import OTHER_STATE, STATE_FOLDERS, make_other_key
from .models import TopicLocation


def topic_path(entry: Path) -> Path:
    """The path a topic's folder is operated on through.

    A symlinked topic keeps the link's own path: resolving it would make a
    rename or move act on the link's target, outside the configured roots.
    """
    return entry if entry.is_symlink() else entry.resolve()


def scan_para_roots(roots: list[Path], source: str) -> dict[str, list[TopicLocation]]:
    topics: dict[str, list[TopicLocation]] = defaultdict(list)
    for root in roots:
        if not root.exists():
            continue
        for state, folder_name in STATE_FOLDERS.items():
            state_path = root / folder_name
            if not state_path.is_dir():
                continue
            for entry in state_path.iterdir():
                if entry.is_dir():
                    topics[entry.name].append(
                        TopicLocation(source=source, state=state, path=topic_path(entry))
                    )
    return topics


def scan_other_folders(roots: list[Path], source: str) -> dict[str, list[TopicLocation]]:
    topics: dict[str, list[TopicLocation]] = defaultdict(list)
    for root in roots:
        if not root.exists():
            continue
        for entry in root.iterdir():
            if entry.is_dir():
                topics[make_other_key(entry.name)].append(
                    TopicLocation(source=source, state=OTHER_STATE, path=topic_path(entry))
                )
    return topics


def path_signature(path: Path) -> object:
    """Enough of a watched path to tell whether it changed: a folder's entry
    names, a file's modification time and size; None when it is gone."""
    try:
        if path.is_dir():
            return frozenset(os.listdir(path))
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def watch_snapshot(paths: Iterable[str]) -> dict[str, object]:
    return {value: path_signature(Path(value)) for value in paths}
