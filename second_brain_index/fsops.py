from __future__ import annotations

import errno
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

# Windows raises these when another process holds a handle on the folder, which
# for this app usually means Obsidian or Explorer has it open.
WINDOWS_LOCK_ERRORS = {5, 32}


class DestinationExists(OSError):
    """Raised instead of letting a rename replace an existing path."""


@dataclass
class MergeReport:
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    conflicts: list[Path] = field(default_factory=list)


def paths_are_same_entry(first: Path, second: Path) -> bool:
    """True when both paths name the same directory entry."""
    try:
        first_stat = first.stat()
        second_stat = second.stat()
    except OSError:
        return False
    # SMB shares report st_ino == 0 for everything, which would make samefile
    # claim two unrelated folders are one and the same.
    if first_stat.st_ino and second_stat.st_ino:
        return (
            first_stat.st_ino == second_stat.st_ino
            and first_stat.st_dev == second_stat.st_dev
        )
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second)
    )


def temporary_rename_path(target: Path) -> Path:
    suffix = f".rename_tmp_{os.getpid()}"
    candidate = target.with_name(f"{target.name}{suffix}")
    counter = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.name}{suffix}_{counter}")
        counter += 1
    return candidate


def _rename_with_retry(source: Path, target: Path, retries: int, delay: float) -> None:
    for attempt in range(retries):
        try:
            os.rename(source, target)
            return
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                shutil.move(str(source), str(target))
                return
            locked = isinstance(exc, PermissionError) or (
                getattr(exc, "winerror", None) in WINDOWS_LOCK_ERRORS
            )
            if not locked or attempt == retries - 1:
                raise
            time.sleep(delay)


def safe_rename(
    source: Path, target: Path, *, retries: int = 5, delay: float = 0.15
) -> None:
    """Rename source to target without ever replacing an existing entry.

    POSIX os.rename silently replaces an empty destination directory while
    Windows refuses outright; checking here makes both platforms refuse, and
    checking immediately before the call keeps the race window microscopic.
    """
    if source == target:
        return
    if target.exists():
        if paths_are_same_entry(source, target):
            # Case-only rename on a case-insensitive filesystem: the two names
            # address one folder, so bounce through a temporary name.
            temporary = temporary_rename_path(target)
            _rename_with_retry(source, temporary, retries, delay)
            _rename_with_retry(temporary, target, retries, delay)
            return
        raise DestinationExists(errno.EEXIST, "Destination already exists", str(target))
    _rename_with_retry(source, target, retries, delay)


def merge_tree(
    source: Path,
    destination: Path,
    conflict_dirname: str,
    *,
    apply: bool = True,
) -> MergeReport:
    """Move everything from source into destination, nesting what would clash.

    Nothing is overwritten and nothing is skipped: an entry that cannot land at
    its natural place goes under destination/conflict_dirname/<relative path>,
    so the source folder always ends up empty and removed. Every move is
    recorded, which makes the operation exactly reversible.
    """
    report = MergeReport()
    _merge_into(source, destination, source, destination / conflict_dirname, report, apply)
    if apply and source.exists() and not any(source.iterdir()):
        source.rmdir()
    return report


def _merge_into(
    source: Path,
    destination: Path,
    source_root: Path,
    conflict_root: Path,
    report: MergeReport,
    apply: bool,
) -> None:
    for item in sorted(source.iterdir(), key=lambda path: path.name):
        target = destination / item.name
        if not target.exists():
            report.moves.append((item, target))
            if apply:
                destination.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(target))
            continue
        if item.is_dir() and target.is_dir():
            _merge_into(item, target, source_root, conflict_root, report, apply)
            if apply and item.exists() and not any(item.iterdir()):
                item.rmdir()
            continue
        report.conflicts.append(target)
        nested = conflict_root / item.relative_to(source_root)
        report.moves.append((item, nested))
        if apply:
            nested.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(nested))
