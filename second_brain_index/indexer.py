from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .aggregation import build_topic_index, choose_display_state
from .config import AppConfig, HubConfig
from .constants import OTHER_STATE, STATE_FOLDERS, STATE_ORDER, hub_key_prefix, make_hub_key
from .json_lists import write_text_atomic
from .models import ManualLink, Topic
from .onenote import OneNoteTopicsError, infer_states_from_paths as infer_onenote_states
from .onenote import load_onenote_topics
from .outlook import OutlookTopicsError, infer_states_from_paths as infer_outlook_states
from .outlook import load_outlook_topics
from .plm import (
    PLMTopicsError,
    infer_states_from_paths as infer_plm_states,
    load_plm_topics,
)
from .scanner import scan_other_folders, scan_para_roots
from .subhub import discover_subhubs


@dataclass(frozen=True)
class SourceSpec:
    name: str
    attr: str
    loader: Callable[[Path], dict[str, list[str]]]
    error_class: type[Exception]
    infer_states: Callable[[list[str], str], set[str]]

    def enabled(self, hub: HubConfig) -> bool:
        return getattr(hub, f"{self.attr}_enabled")

    def topics_path(self, hub: HubConfig) -> Path:
        return getattr(hub, f"{self.attr}_topics_path")

    def paths_of(self, topic: Topic) -> list[str]:
        return getattr(topic, f"{self.attr}_paths")

    def set_paths(self, topic: Topic, paths: list[str]) -> None:
        setattr(topic, f"{self.attr}_paths", paths)


SOURCE_SPECS = (
    SourceSpec(
        "OneNote",
        "onenote",
        load_onenote_topics,
        OneNoteTopicsError,
        infer_onenote_states,
    ),
    SourceSpec(
        "Outlook",
        "outlook",
        load_outlook_topics,
        OutlookTopicsError,
        infer_outlook_states,
    ),
    SourceSpec(
        "PLM",
        "plm",
        load_plm_topics,
        PLMTopicsError,
        infer_plm_states,
    ),
)


@dataclass
class ScanResult:
    """Everything a refresh reads from disk, built off the GUI thread.

    ``errors`` holds (title, message) pairs for the window to report; the scan
    itself never touches a widget.
    """

    topics_by_key: dict[str, Topic] = field(default_factory=dict)
    hubs_by_project_key: dict[str, HubConfig] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)
    # Configured roots, sub-hub ones included, that are not a folder on disk.
    # Checked here so the window never touches a possibly dead mount.
    missing_roots: frozenset[Path] = frozenset()


def run_scan(
    config: AppConfig,
    manual_links: list[ManualLink],
    should_cancel: Callable[[], bool] = lambda: False,
) -> ScanResult:
    result = ScanResult()
    root_hub = config.root_hub

    filesystem_topics = scan_para_roots(root_hub.para_roots, "filesystem")
    obsidian_topics: dict[str, list] = {}
    if root_hub.obsidian_root:
        obsidian_topics = scan_para_roots([root_hub.obsidian_root], "obsidian")
    other_topics = scan_other_folders(config.other_folders, "filesystem")

    result.topics_by_key = build_topic_index(
        filesystem_topics=filesystem_topics,
        obsidian_topics=obsidian_topics,
        other_topics=other_topics,
        manual_links=manual_links,
    )
    for spec in SOURCE_SPECS:
        add_source_topics(result, spec, manual_links, root_hub, None)

    result.hubs_by_project_key, hub_errors = discover_subhubs(
        result.topics_by_key, root_hub
    )
    for project_key, message in hub_errors:
        result.errors.append((f"Sub-hub '{project_key}'", message))

    for project_key, hub in result.hubs_by_project_key.items():
        hub_filesystem = scan_para_roots(hub.para_roots, "filesystem")
        hub_obsidian: dict[str, list] = {}
        if hub.obsidian_root:
            hub_obsidian = scan_para_roots([hub.obsidian_root], "obsidian")
        hub_topics = build_topic_index(
            filesystem_topics=hub_filesystem,
            obsidian_topics=hub_obsidian,
            other_topics={},
            manual_links=manual_links,
            key_prefix=hub_key_prefix(project_key),
            hub_key=project_key,
        )
        result.topics_by_key.update(hub_topics)
        for spec in SOURCE_SPECS:
            add_source_topics(result, spec, manual_links, hub, project_key)

    result.missing_roots = _missing_roots(config, result.hubs_by_project_key)

    # A cancelled scan must not overwrite the export with a result nobody uses.
    if not should_cancel():
        export_topics(result, config)
    return result


def _missing_roots(
    config: AppConfig, hubs_by_project_key: dict[str, HubConfig]
) -> frozenset[Path]:
    roots: set[Path] = set(config.other_folders)
    for hub in (config.root_hub, *hubs_by_project_key.values()):
        roots.update(hub.para_roots)
        if hub.obsidian_root is not None:
            roots.add(hub.obsidian_root)
    return frozenset(root for root in roots if not root.is_dir())


def add_source_topics(
    result: ScanResult,
    spec: SourceSpec,
    manual_links: list[ManualLink],
    hub: HubConfig,
    hub_key: str | None,
) -> None:
    if not spec.enabled(hub):
        return

    try:
        source_topics = spec.loader(spec.topics_path(hub))
    except spec.error_class as exc:
        source_label = spec.name if hub_key is None else f"{spec.name} ({hub_key})"
        result.errors.append((source_label, str(exc)))
        return

    if not source_topics:
        return

    links_by_topic: dict[str, list[ManualLink]] = defaultdict(list)
    for link in manual_links:
        links_by_topic[link.topic_name].append(link)

    for name, paths in source_topics.items():
        key = make_hub_key(hub_key, name) if hub_key else name
        source_states = spec.infer_states(paths, name)
        topic = result.topics_by_key.get(key)
        if topic:
            spec.set_paths(topic, paths)
            combined_states = topic.states | source_states
            has_inconsistency = len(combined_states) > 1
            topic.states = combined_states
            topic.has_inconsistency = has_inconsistency
            topic.display_state = choose_display_state(
                topic.name, combined_states, has_inconsistency
            )
            continue

        states = source_states
        has_inconsistency = len(states) > 1
        display_state = choose_display_state(name, states, has_inconsistency)
        topic_links = links_by_topic.get(key, [])
        topic_links.sort(key=lambda item: item.link_name.casefold())
        new_topic = Topic(
            name=name,
            locations=[],
            manual_links=topic_links,
            states=states,
            display_state=display_state,
            has_inconsistency=has_inconsistency,
            hub_key=hub_key,
        )
        spec.set_paths(new_topic, paths)
        result.topics_by_key[key] = new_topic


def export_topics(result: ScanResult, config: AppConfig) -> None:
    root_export_path = config.root_hub.onenote_topics_path.parent / "Topics_export.json"
    _write_topics_export(result, root_export_path, None)
    for project_key, hub in result.hubs_by_project_key.items():
        export_dir = hub.marker_path.parent if hub.marker_path else hub.para_roots[0]
        _write_topics_export(result, export_dir / "Topics_export.json", project_key)


def _write_topics_export(
    result: ScanResult, export_path: Path, hub_key: str | None
) -> None:
    sections = []
    for state in STATE_ORDER:
        if state == OTHER_STATE:
            continue
        topics = [
            topic.name
            for topic in result.topics_by_key.values()
            if topic.display_state == state and topic.hub_key == hub_key
        ]
        topics.sort(key=lambda name: (name.casefold(), name))
        sections.append({"name": STATE_FOLDERS[state], "topics": topics})

    payload = {"sections": sections}
    try:
        write_text_atomic(export_path, json.dumps(payload, indent=2))
    except OSError as exc:
        result.errors.append(
            ("Export failed", f"Failed to write topics export: {exc}")
        )
