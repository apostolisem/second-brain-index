from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal


def collect_recursive_names(root: Path) -> set[str]:
    names: set[str] = set()
    if not root.exists():
        return names
    try:
        for _dirpath, dirnames, filenames in os.walk(
            root, topdown=True, onerror=lambda _err: None
        ):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for dirname in dirnames:
                names.add(dirname)
            for filename in filenames:
                if filename.startswith("."):
                    continue
                names.add(filename)
    except OSError:
        return names
    return names


def build_filename_blob(paths: list[Path]) -> str:
    names: set[str] = set()
    for path in paths:
        names.update(collect_recursive_names(path))
    return " ".join(sorted(names, key=str.casefold)).casefold()


class FilenameIndexWorker(QObject):
    """Walks topic folders off the UI thread and emits one blob per topic."""

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
        blobs: dict[str, str] = {}
        total = len(self._paths_by_key)
        for done, (key, paths) in enumerate(self._paths_by_key.items(), start=1):
            if self._cancelled:
                return
            blobs[key] = build_filename_blob(paths)
            if done % 25 == 0 or done == total:
                self.progress.emit(self._generation, done, total)
        if self._cancelled:
            return
        self.finished.emit(self._generation, blobs)
