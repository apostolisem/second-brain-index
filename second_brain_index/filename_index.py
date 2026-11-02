from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from PyQt6.QtCore import QObject, pyqtSignal

from .search import fold


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

    ``blob`` holds the unique folded names (see search.fold) joined by newlines, so a search
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
    return index_from_entries(entries)


def index_from_entries(entries: list[FileEntry]) -> TopicFileIndex:
    names = {fold(entry.name) for entry in entries}
    return TopicFileIndex(blob="\n".join(sorted(names)), entries=tuple(entries))


def collect_many(
    paths: list[Path], should_cancel: Callable[[], bool] | None = None
) -> dict[Path, list[FileEntry]]:
    """collect_entries for every path, walking each folder on disk only once.

    A sub-hub's topics sit inside its project's folder; their entries are
    taken from the project's walk instead of walking the same tree again.
    """
    walked: dict[Path, list[FileEntry]] = {}
    collect_into(walked, by_depth(paths), should_cancel or (lambda: False))
    return walked


def by_depth(paths: list[Path]) -> list[Path]:
    """Shallowest first, so an enclosing folder is walked before what it holds."""
    return sorted(set(paths), key=lambda value: (len(value.parts), str(value)))


def collect_into(
    walked: dict[Path, list[FileEntry]],
    paths: list[Path],
    should_cancel: Callable[[], bool],
) -> None:
    """Add each path's entries to ``walked``, reusing what it already holds."""
    for path in paths:
        if should_cancel():
            return
        ancestor = next((parent for parent in path.parents if parent in walked), None)
        if ancestor is not None and _walk_reaches(ancestor, path):
            walked[path] = _entries_below(walked[ancestor], ancestor, path)
        else:
            walked[path] = collect_entries(path, should_cancel)


def _walk_reaches(ancestor: Path, path: Path) -> bool:
    """Whether walking ``ancestor`` went into ``path`` as a walk of it would.

    The walk skips hidden folders and does not follow symbolic links.
    """
    if not ancestor.is_dir() or ancestor.is_symlink():
        return False
    current = ancestor
    for part in path.relative_to(ancestor).parts:
        current = current / part
        if part.startswith(".") or current.is_symlink():
            return False
    return path.is_dir()


def _entries_below(
    entries: list[FileEntry], ancestor: Path, path: Path
) -> list[FileEntry]:
    prefix = os.path.join(*path.relative_to(ancestor).parts) + os.sep
    return [
        FileEntry(path, entry.rel_path[len(prefix):], entry.is_dir)
        for entry in entries
        if entry.root == ancestor and entry.rel_path.startswith(prefix)
    ]


class FilenameIndexWorker(QObject):
    """Walks topic folders off the UI thread and emits one index per topic.

    ``reusable`` holds indexes kept from an earlier walk; only the other
    topics are walked, and the result holds both.
    """

    progress = pyqtSignal(int, int, int)
    finished = pyqtSignal(int, dict)

    def __init__(
        self,
        generation: int,
        paths_by_key: dict[str, list[Path]],
        reusable: dict[str, TopicFileIndex] | None = None,
    ) -> None:
        super().__init__()
        self._generation = generation
        self._paths_by_key = paths_by_key
        self._reusable = reusable or {}
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        indexes: dict[str, TopicFileIndex] = dict(self._reusable)
        todo = {
            key: paths for key, paths in self._paths_by_key.items() if key not in indexes
        }
        # Progress counts folders, the slow part; topics are assembled after.
        paths = by_depth([path for paths in todo.values() for path in paths])
        walked: dict[Path, list[FileEntry]] = {}
        for start in range(0, len(paths), 25):
            collect_into(walked, paths[start : start + 25], lambda: self._cancelled)
            if self._cancelled:
                return
            self.progress.emit(self._generation, min(start + 25, len(paths)), len(paths))
        for key, topic_paths in todo.items():
            indexes[key] = index_from_entries(
                [entry for path in topic_paths for entry in walked.get(path, ())]
            )
        self.finished.emit(self._generation, indexes)
