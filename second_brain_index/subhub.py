from __future__ import annotations

from pathlib import Path

from .config import MARKER_FILENAME, HubConfig, HubConfigError
from .constants import is_hub_key, is_other_key
from .models import Topic


def discover_subhubs(
    topics_by_key: dict[str, Topic],
    root_hub: HubConfig,
) -> tuple[dict[str, HubConfig], list[tuple[str, str]]]:
    """Find topics whose folder carries a sub-hub marker file.

    Returns (hubs keyed by owning topic key, list of (topic key, error)).
    Only main-hub topics are considered, so sub-hub nesting stops at depth 1.

    The marker is looked up in the topic's filesystem folder first, then in
    its Obsidian folder, so a project that exists only in the vault can still
    be a sub-hub. Missing marker fields are auto-filled by mirroring the
    project's PARA-relative path between the root hub's filesystem roots and
    its Obsidian vault, so sub-hubs stay portable across machines instead of
    hard-coding absolute paths.
    """
    hubs: dict[str, HubConfig] = {}
    errors: list[tuple[str, str]] = []
    for key, topic in topics_by_key.items():
        if is_other_key(key) or is_hub_key(key):
            continue
        marker = _find_marker(topic)
        if marker is None:
            continue
        marker_path, marker_source = marker
        try:
            hub = HubConfig.from_marker(marker_path)
        except HubConfigError as exc:
            errors.append((key, str(exc)))
            continue
        if marker_source == "obsidian":
            _apply_obsidian_marker_defaults(hub, root_hub, marker_path.parent)
        else:
            _apply_filesystem_marker_defaults(hub, root_hub, marker_path.parent)
        # A folder inside the root vault is opened through the root vault's
        # name, so only standalone vault folders get a fabricated name
        # (Obsidian's own default when a folder is added as a vault).
        if (
            hub.obsidian_root is not None
            and not hub.obsidian_vault_name
            and not _is_inside(hub.obsidian_root, root_hub.obsidian_root)
        ):
            hub.obsidian_vault_name = topic.name
        hubs[key] = hub
    return hubs, errors


def _is_inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _find_marker(topic: Topic) -> tuple[Path, str] | None:
    # Filesystem markers take precedence over Obsidian ones.
    for wanted_source in ("filesystem", "obsidian"):
        for location in topic.locations:
            if location.source != wanted_source:
                continue
            marker_path = location.path / MARKER_FILENAME
            if marker_path.is_file():
                return marker_path, wanted_source
    return None


def _apply_filesystem_marker_defaults(
    hub: HubConfig, root_hub: HubConfig, project_path: Path
) -> None:
    if hub.obsidian_root is None and root_hub.obsidian_root is not None:
        relative = _relative_to_any(project_path, root_hub.para_roots)
        if relative is not None:
            candidate = root_hub.obsidian_root / relative
            if candidate.is_dir():
                hub.obsidian_root = candidate


def _apply_obsidian_marker_defaults(
    hub: HubConfig, root_hub: HubConfig, project_path: Path
) -> None:
    # The marker's parent is an Obsidian folder; the para_root "." default
    # would scan it with source "filesystem" and mislabel every location.
    if hub.para_roots == [project_path]:
        hub.para_roots = []
    if hub.obsidian_root is None:
        hub.obsidian_root = project_path
    if not hub.para_roots and root_hub.obsidian_root is not None:
        try:
            relative = project_path.relative_to(root_hub.obsidian_root)
        except ValueError:
            return
        for para_root in root_hub.para_roots:
            candidate = para_root / relative
            if candidate.is_dir():
                hub.para_roots = [candidate]
                break


def _relative_to_any(path: Path, roots: list[Path]) -> Path | None:
    for root in roots:
        try:
            return path.relative_to(root)
        except ValueError:
            continue
    return None
