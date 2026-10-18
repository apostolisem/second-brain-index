from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from .config import AppConfig, HubConfig
from .constants import display_name_from_key
from .indexer import SOURCE_SPECS
from .models import Topic

if TYPE_CHECKING:
    # filename_index imports Qt; this module stays importable without it.
    from .filename_index import FileEntry

KIND_ALL = "all"
KIND_SOURCE = "source"
KIND_ROOT = "root"
KIND_NO_FOLDER = "no_folder"

SOURCE_FILESYSTEM = "filesystem"
SOURCE_OBSIDIAN = "obsidian"
SOURCE_ADDED_FOLDER = "added_folder"
SOURCE_ADDED_LINK = "added_link"


@dataclass(frozen=True)
class LocationFilter:
    """One choice of the Location filter.

    Identity is ``kind`` + ``value`` + ``hub_key``; the label and tooltip are
    for display only, so a rebuilt option still equals the active filter.
    """

    kind: str
    # A source id for KIND_SOURCE, the root's path for KIND_ROOT.
    value: str = ""
    label: str = field(default="All locations", compare=False)
    tooltip: str = field(default="", compare=False)
    # Set for a sub-hub root: the owning project's topic key.
    hub_key: str | None = None

    def is_all(self) -> bool:
        return self.kind == KIND_ALL


ALL_LOCATIONS = LocationFilter(KIND_ALL)
NO_FOLDER = LocationFilter(
    KIND_NO_FOLDER,
    label="No folder on disk",
    tooltip="Topics known only from OneNote, Outlook, PLM or added links",
)


@dataclass(frozen=True)
class LocationOptions:
    sources: tuple[LocationFilter, ...] = ()
    roots: tuple[LocationFilter, ...] = ()
    subhub_roots: tuple[LocationFilter, ...] = ()

    def all(self) -> tuple[LocationFilter, ...]:
        return (ALL_LOCATIONS, *self.sources, *self.roots, *self.subhub_roots, NO_FOLDER)


def path_is_under(path: PurePath, root: PurePath) -> bool:
    """True when ``path`` is ``root`` or inside it.

    Purely lexical: nothing is resolved, so a dead mount cannot block it.
    """
    return path == root or root in path.parents


def manual_folder_paths(topic: Topic) -> list[Path]:
    return [
        Path(location.target.strip()).expanduser()
        for location in topic.manual_locations
        if location.kind == "file" and location.target.strip()
    ]


def topic_folder_paths(topic: Topic) -> list[Path]:
    """Every folder of a topic on disk: discovered ones, then added ones."""
    return [location.path for location in topic.locations] + manual_folder_paths(topic)


def topic_matches_location(topic: Topic, flt: LocationFilter) -> bool:
    if flt.kind == KIND_ALL:
        return True
    if flt.kind == KIND_NO_FOLDER:
        return not topic_folder_paths(topic)
    if flt.kind == KIND_ROOT:
        root = Path(flt.value)
        return any(path_is_under(path, root) for path in topic_folder_paths(topic))
    if flt.kind == KIND_SOURCE:
        return _topic_has_source(topic, flt.value)
    return False


def _topic_has_source(topic: Topic, source: str) -> bool:
    if source in (SOURCE_FILESYSTEM, SOURCE_OBSIDIAN):
        return any(location.source == source for location in topic.locations)
    if source == SOURCE_ADDED_FOLDER:
        return any(location.kind == "file" for location in topic.manual_locations)
    if source == SOURCE_ADDED_LINK:
        return any(location.kind != "file" for location in topic.manual_locations)
    for spec in SOURCE_SPECS:
        if spec.attr == source:
            return bool(spec.paths_of(topic))
    return False


def allowed_index_roots(topic: Topic, flt: LocationFilter) -> list[Path] | None:
    """Folders whose files count for ``flt``; ``None`` means no restriction.

    OneNote, Outlook, PLM, added links and "No folder on disk" have no files,
    so they allow none.
    """
    if flt.kind == KIND_ALL:
        return None
    if flt.kind == KIND_ROOT:
        return [Path(flt.value)]
    if flt.kind == KIND_SOURCE:
        if flt.value in (SOURCE_FILESYSTEM, SOURCE_OBSIDIAN):
            return [
                location.path
                for location in topic.locations
                if location.source == flt.value
            ]
        if flt.value == SOURCE_ADDED_FOLDER:
            return manual_folder_paths(topic)
    return []


def filter_file_entries(
    entries: Sequence["FileEntry"], roots: list[Path] | None
) -> tuple["FileEntry", ...]:
    """Keep the entries that lie under one of ``roots``.

    A topic's entries share a handful of indexed folders, so the path work is
    done once per folder, never per file: a topic can hold 100k entries.
    """
    if roots is None:
        return tuple(entries)
    if not roots:
        return ()
    verdicts: dict[Path, bool | tuple[str, ...]] = {}
    kept: list["FileEntry"] = []
    for entry in entries:
        verdict = verdicts.get(entry.root)
        if verdict is None:
            verdict = verdicts[entry.root] = _folder_verdict(entry.root, roots)
        if verdict is True:
            kept.append(entry)
        elif verdict:
            rel_path = os.path.normcase(entry.rel_path)
            if any(
                rel_path == prefix or rel_path.startswith(prefix + os.sep)
                for prefix in verdict
            ):
                kept.append(entry)
    return tuple(kept)


def _folder_verdict(folder: Path, roots: list[Path]) -> bool | tuple[str, ...]:
    """Whether an indexed folder's entries are kept: all, none, or those below
    the returned ``rel_path`` prefixes (allowed roots that sit inside it)."""
    if any(path_is_under(folder, root) for root in roots):
        return True
    return tuple(
        os.path.normcase(str(root.relative_to(folder)))
        for root in roots
        if path_is_under(root, folder)
    ) or False


def build_location_options(
    config: AppConfig,
    hubs_by_project_key: Mapping[str, HubConfig],
    topics: Iterable[Topic],
) -> LocationOptions:
    topics = list(topics)
    return LocationOptions(
        sources=tuple(_source_options(config, hubs_by_project_key, topics)),
        roots=tuple(_root_options(config)),
        subhub_roots=tuple(_subhub_root_options(config, hubs_by_project_key)),
    )


def _source_options(
    config: AppConfig,
    hubs_by_project_key: Mapping[str, HubConfig],
    topics: list[Topic],
) -> list[LocationFilter]:
    hubs = [config.root_hub, *hubs_by_project_key.values()]
    options: list[LocationFilter] = []
    if any(hub.para_roots for hub in hubs) or config.other_folders or any(
        _topic_has_source(topic, SOURCE_FILESYSTEM) for topic in topics
    ):
        options.append(
            LocationFilter(
                KIND_SOURCE,
                SOURCE_FILESYSTEM,
                "Filesystem",
                "Topics with a folder under a PARA or Others root",
            )
        )
    if any(hub.obsidian_root for hub in hubs) or any(
        _topic_has_source(topic, SOURCE_OBSIDIAN) for topic in topics
    ):
        options.append(
            LocationFilter(
                KIND_SOURCE, SOURCE_OBSIDIAN, "Obsidian", "Topics with an Obsidian folder"
            )
        )
    for spec in SOURCE_SPECS:
        if any(spec.enabled(hub) for hub in hubs) or any(
            spec.paths_of(topic) for topic in topics
        ):
            options.append(
                LocationFilter(
                    KIND_SOURCE,
                    spec.attr,
                    spec.name,
                    f"Topics listed in the {spec.name} topic list",
                )
            )
    if any(_topic_has_source(topic, SOURCE_ADDED_FOLDER) for topic in topics):
        options.append(
            LocationFilter(
                KIND_SOURCE,
                SOURCE_ADDED_FOLDER,
                "Added folder",
                "Topics with a folder you added",
            )
        )
    if any(_topic_has_source(topic, SOURCE_ADDED_LINK) for topic in topics):
        options.append(
            LocationFilter(
                KIND_SOURCE,
                SOURCE_ADDED_LINK,
                "Added link",
                "Topics with a link you added",
            )
        )
    return options


def _root_options(config: AppConfig) -> list[LocationFilter]:
    entries: list[tuple[Path, str]] = []
    for root in config.root_hub.para_roots:
        entries.append((root, ""))
    if config.root_hub.obsidian_root is not None:
        entries.append((config.root_hub.obsidian_root, ""))
    for root in config.other_folders:
        entries.append((root, " (Others)"))

    unique: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for root, suffix in entries:
        if root in seen:
            continue
        seen.add(root)
        unique.append((root, suffix))

    labels = _disambiguated_names([root for root, _suffix in unique])
    return [
        LocationFilter(KIND_ROOT, str(root), f"{label}{suffix}", str(root))
        for (root, suffix), label in zip(unique, labels)
    ]


def _disambiguated_names(paths: list[Path]) -> list[str]:
    """Folder names, widened to ``parent/name`` where two roots share one."""
    names = [path.name or str(path) for path in paths]
    counts = Counter(names)
    return [
        f"{path.parent.name}/{name}" if counts[name] > 1 and path.name else name
        for path, name in zip(paths, names)
    ]


def _subhub_root_options(
    config: AppConfig,
    hubs_by_project_key: Mapping[str, HubConfig],
) -> list[LocationFilter]:
    main_roots = set(config.root_hub.para_roots) | set(config.other_folders)
    if config.root_hub.obsidian_root is not None:
        main_roots.add(config.root_hub.obsidian_root)

    def project_name(project_key: str) -> str:
        # Only the project topic itself has this key.
        return display_name_from_key(project_key)

    options: list[LocationFilter] = []
    seen: set[Path] = set(main_roots)
    for project_key, hub in sorted(
        hubs_by_project_key.items(),
        key=lambda item: (project_name(item[0]).casefold(), item[0]),
    ):
        name = project_name(project_key)
        roots = [(root, "PARA") for root in hub.para_roots]
        if hub.obsidian_root is not None:
            roots.append((hub.obsidian_root, "Vault"))
        for root, fallback in roots:
            if root in seen:
                continue
            seen.add(root)
            root_label = fallback if root.name in ("", name) else root.name
            options.append(
                LocationFilter(
                    KIND_ROOT,
                    str(root),
                    f"{name} · {root_label}",
                    str(root),
                    hub_key=project_key,
                )
            )
    return options
