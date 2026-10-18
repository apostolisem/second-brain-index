from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from PyQt6.QtCore import QObject, pyqtSignal


class FileEntry(NamedTuple):
    root: Path
    rel_path: str
    is_dir: bool

    @property
    def name(self) -> str:
        return os.path.basename(self.rel_path)

    @property
    def path(self) -> Path:
        return self.root / self.rel_path


@dataclass(frozen=True)
class TopicFileIndex:
    """Search blob plus the entries it was built from.

    ``blob`` holds the unique casefolded names joined by newlines, so a search
    token (which never contains a newline) cannot match across two names.
    """

    blob: str = ""
    entries: tuple[FileEntry, ...] = ()


def collect_entries(
    root: Path, should_cancel: Callable[[], bool] | None = None
) -> list[FileEntry]:
    """List everything under ``root``; a ``root`` that is a file lists itself.

    ``should_cancel`` is polled once per directory; the walk stops when it
    returns true and what was collected so far is returned.
    """
    entries: list[FileEntry] = []
    if not root.exists():
        return entries
    if root.is_file():
        if not root.name.startswith("."):
            entries.append(FileEntry(root.parent, root.name, False))
        return entries
    try:
        for dirpath, dirnames, filenames in os.walk(
            root, topdown=True, onerror=lambda _err: None
        ):
            if should_cancel is not None and should_cancel():
                break
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            rel_dir = os.path.relpath(dirpath, root)
            prefix = "" if rel_dir == "." else rel_dir
            for dirname in dirnames:
                entries.append(FileEntry(root, os.path.join(prefix, dirname), True))
            for filename in filenames:
                if filename.startswith("."):
                    continue
                entries.append(FileEntry(root, os.path.join(prefix, filename), False))
    except OSError:
        return entries
    return entries


def build_topic_file_index(
    paths: list[Path], should_cancel: Callable[[], bool] | None = None
) -> TopicFileIndex:
    entries: list[FileEntry] = []
    for path in paths:
        entries.extend(collect_entries(path, should_cancel))
    names = {entry.name.casefold() for entry in entries}
    return TopicFileIndex(blob="\n".join(sorted(names)), entries=tuple(entries))


class FilenameIndexWorker(QObject):
    """Walks topic folders off the UI thread and emits one index per topic."""

    progress = pyqtSignal(int, int, int)
    finished = pyqtSignal(int, dict)

    def __init__(self, generation: int, paths_by_key: dict[str, list[Path]]) -> None:
        super().__init__()
        self._generation = generation
        self._paths_by_key = paths_by_key
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        indexes: dict[str, TopicFileIndex] = {}
        total = len(self._paths_by_key)
        for done, (key, paths) in enumerate(self._paths_by_key.items(), start=1):
            if self._cancelled:
                return
            indexes[key] = build_topic_file_index(paths, lambda: self._cancelled)
            if done % 25 == 0 or done == total:
                self.progress.emit(self._generation, done, total)
        if self._cancelled:
            return
        self.finished.emit(self._generation, indexes)
