from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..config import DEFAULT_TOPIC_LIST_NAMES, MARKER_FILENAME, HubConfig
from ..constants import STATE_FOLDERS, display_name_from_key, make_hub_key
from ..indexer import SOURCE_SPECS, SourceSpec
from ..json_lists import empty_topics_document
from ..models import ManualLocation

FOLDER_SOURCES = {"filesystem": "Filesystem", "obsidian": "Obsidian"}
README_FILENAME = "README.md"


def creation_key(name: str, hub_key: str | None) -> str:
    return make_hub_key(hub_key, name) if hub_key else name


@dataclass(frozen=True)
class HubChoice:
    key: str | None
    label: str
    hub: HubConfig


@dataclass(frozen=True)
class CreationOption:
    """One place the wizard can create a topic in: a folder, or a list entry."""

    source: str
    label: str
    detail: str
    path: Path | None = None
    spec: SourceSpec | None = None
    # Why it cannot be picked; None when it can.
    blocked: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.path is not None


@dataclass
class CreationPlan:
    name: str
    state: str
    hub_key: str | None
    hub: HubConfig
    folders: list[CreationOption] = field(default_factory=list)
    # Each list with the direct link to save for it ("" for none).
    lists: list[tuple[SourceSpec, str]] = field(default_factory=list)
    make_subhub: bool = False
    # The user's own links and folders, as on Add location; topic_name is
    # filled in when they are saved.
    manual_locations: list[ManualLocation] = field(default_factory=list)

    @property
    def key(self) -> str:
        return creation_key(self.name, self.hub_key)

    @property
    def is_standalone(self) -> bool:
        """No folder or list: the topic is made of the user's own places only."""
        return not self.folders and not self.lists and bool(self.manual_locations)


@dataclass(frozen=True)
class ManualAction:
    source: str
    text: str
    url: str | None


@dataclass
class CreationReport:
    done: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    manual_actions: list[ManualAction] = field(default_factory=list)


class TopicCreationMixin:
    """Planning and creating a new topic for MainWindow. No widgets here."""

    def creation_hubs(self) -> list[HubChoice]:
        choices = [HubChoice(None, "Main hub", self.config.root_hub)]
        for key in sorted(self.hubs_by_project_key, key=str.casefold):
            choices.append(
                HubChoice(key, f"Sub-hub: {display_name_from_key(key)}", self.hubs_by_project_key[key])
            )
        return choices

    def creation_hub(self, hub_key: str | None) -> HubConfig:
        if hub_key and hub_key in self.hubs_by_project_key:
            return self.hubs_by_project_key[hub_key]
        return self.config.root_hub

    def validate_new_topic(self, name: str, state: str, hub_key: str | None) -> str | None:
        """Why a topic cannot be created there; None when it can.

        A topic that already exists in the same section is fine: the wizard
        then only adds the places it is still missing.
        """
        error = self.validate_topic_name(name)
        if error:
            return error
        key = creation_key(name.strip(), hub_key)
        existing = self.find_colliding_topic_key(key, "")
        if existing is None:
            return None
        if existing != key:
            return (
                f"A topic named '{display_name_from_key(existing)}' already exists;"
                " names may not differ only in case or accents."
            )
        topic = self.topics_by_key[existing]
        if state not in topic.states:
            return (
                f"'{topic.name}' already exists in "
                f"{self.format_section_list(topic.states)}. Use Move to change its section."
            )
        return None

    def creation_options(
        self, name: str, state: str, hub_key: str | None
    ) -> list[CreationOption]:
        hub = self.creation_hub(hub_key)
        topic = self.topics_by_key.get(creation_key(name, hub_key))
        section = STATE_FOLDERS[state]
        options: list[CreationOption] = []

        roots = [("filesystem", root) for root in hub.para_roots]
        if hub.obsidian_root is not None:
            roots.append(("obsidian", hub.obsidian_root))
        for source, root in roots:
            path = root / section / name
            blocked = None
            if not root.is_dir():
                blocked = "root not found"
            elif os.path.lexists(path):
                blocked = "already exists"
            options.append(
                CreationOption(source, FOLDER_SOURCES[source], str(path), path=path, blocked=blocked)
            )

        for spec in SOURCE_SPECS:
            if not spec.enabled(hub):
                continue
            blocked = "already listed" if topic and spec.paths_of(topic) else None
            options.append(
                CreationOption(
                    spec.attr,
                    spec.name,
                    f"Entry in {spec.topics_path(hub).name}",
                    spec=spec,
                    blocked=blocked,
                )
            )
        return options

    def creation_section_label(self, plan: CreationPlan) -> str:
        """The section as the user finds it in the app, with the sub-hub project first."""
        section = STATE_FOLDERS[plan.state]
        if plan.hub_key:
            return f"{display_name_from_key(plan.hub_key)} / {section}"
        return section

    def manual_create_action(self, plan: CreationPlan, spec: SourceSpec) -> ManualAction:
        hint = spec.create_hint.format(
            name=plan.name, section=self.creation_section_label(plan)
        )
        return ManualAction(
            spec.name, f"{spec.name}: {hint} in the {spec.name} app.", spec.base_url(plan.hub)
        )

    def describe_creation_plan(self, plan: CreationPlan) -> list[str]:
        lines = []
        if plan.is_standalone and plan.key not in self.topics_by_key:
            lines.append(
                f"Create the topic in {self.creation_section_label(plan)}"
                " with no folder or list entry"
            )
        for option in plan.folders:
            lines.append(f"Create folder ({option.label}): {option.path}")
            if option.source == "obsidian":
                lines.append(f"Add {README_FILENAME} in it")
        if plan.make_subhub:
            lines.append(
                "Set it up as a Sub-Hub: PARA section folders, "
                f"{MARKER_FILENAME} and empty topic lists"
            )
        for spec, url in plan.lists:
            lines.append(f"Add an entry to {spec.topics_path(plan.hub).name}")
            if url:
                lines.append(f"Save {spec.name} direct link: {url}")
        for location in plan.manual_locations:
            lines.append(
                f"Add {location.source_label.lower()}: {location.display_label} ({location.target})"
            )
        return lines

    def execute_creation_plan(self, plan: CreationPlan) -> CreationReport:
        """Create the folders, then the list entries.

        Folders come first and all-or-nothing: when one fails, everything
        created so far is taken back and no list is touched, so a half-made
        topic never shows up. Lists are independent files; each one that
        fails is reported without undoing the others.
        """
        report = CreationReport()
        if plan.is_standalone and plan.key not in self.topics_by_key:
            self.link_store.set_standalone_topic(plan.key, plan.state)
            report.done.append(
                f"Created '{plan.name}' in {self.creation_section_label(plan)}"
                " with only your own links and folders"
            )
        created: list[Path] = []
        try:
            for option in plan.folders:
                self.create_topic_folder(option, plan, created)
                report.done.append(f"{option.label}: created {option.path}")
            if plan.make_subhub and plan.folders:
                self.create_subhub_contents(plan, created)
                report.done.append(f"Set up '{plan.name}' as a Sub-Hub")
        except OSError as exc:
            self.remove_created_paths(created)
            report.done.clear()
            report.failed.append(f"Unable to create the folders: {exc}")
            return report

        for spec, url in plan.lists:
            error = self.add_new_topic_to_list(spec, plan)
            if error:
                report.failed.append(f"{spec.name}: {error}")
                continue
            report.done.append(f"{spec.name}: added to {spec.topics_path(plan.hub).name}")
            if url:
                self._save_source_direct_link(
                    plan.key, self.topics_by_key.get(plan.key), spec, url
                )
            report.manual_actions.append(self.manual_create_action(plan, spec))

        if plan.manual_locations:
            self.save_new_topic_locations(plan, report)
        return report

    def save_new_topic_locations(self, plan: CreationPlan, report: CreationReport) -> None:
        # A link or folder of the user's own does not make a topic; with every
        # folder and list failed there is no topic to hang it on.
        if not report.done and plan.key not in self.topics_by_key:
            report.failed.append(
                "Your links and folders were not added: the topic itself was not created."
            )
            return
        for location in plan.manual_locations:
            self.link_store.add_location(
                ManualLocation(0, plan.key, location.label, location.target, location.kind)
            )
            report.done.append(f"{location.source_label}: added {location.display_label}")

    def create_topic_folder(
        self, option: CreationOption, plan: CreationPlan, created: list[Path]
    ) -> None:
        path = option.path
        section_folder = path.parent
        if not section_folder.exists():
            # Archive in particular often does not exist yet in a new root.
            section_folder.mkdir()
            created.append(section_folder)
        path.mkdir()
        created.append(path)
        if option.source == "obsidian":
            readme = path / README_FILENAME
            readme.write_text(self.readme_text(plan.name), encoding="utf-8")
            created.append(readme)

    def readme_text(self, name: str) -> str:
        return (
            f"# {name}\n\n"
            f"Created with Second Brain Hub on {date.today().isoformat()}.\n"
        )

    def create_subhub_contents(self, plan: CreationPlan, created: list[Path]) -> None:
        """Turn the new project into a Sub-Hub.

        The marker and lists go in the filesystem folder when there is one.
        A vault folder created alongside is left out of the marker, so the
        Sub-Hub finds it by mirroring the project's path instead of pointing
        at a placeholder vault.
        """
        primary = next(
            (option for option in plan.folders if option.source == "filesystem"),
            plan.folders[0],
        )
        has_vault_folder = any(option.source == "obsidian" for option in plan.folders)
        for option in plan.folders:
            for folder_name in STATE_FOLDERS.values():
                folder = option.path / folder_name
                folder.mkdir()
                created.append(folder)
        self.write_subhub_files(primary.path, created, mirror_obsidian=has_vault_folder)

    def create_subhub_structure(self, project_path: Path) -> None:
        project_path.mkdir(parents=True, exist_ok=False)
        for folder_name in STATE_FOLDERS.values():
            (project_path / folder_name).mkdir()
        self.write_subhub_files(project_path, [])

    def write_subhub_files(
        self, project_path: Path, created: list[Path], mirror_obsidian: bool = False
    ) -> None:
        marker = self.build_subhub_marker_template(project_path.name)
        if mirror_obsidian:
            marker["obsidian"] = {}
        marker_path = project_path / MARKER_FILENAME
        marker_path.write_text(f"{json.dumps(marker, indent=2)}\n", encoding="utf-8")
        created.append(marker_path)
        payload = json.dumps(empty_topics_document(), indent=2)
        for filename in DEFAULT_TOPIC_LIST_NAMES.values():
            list_path = project_path / filename
            list_path.write_text(payload, encoding="utf-8")
            created.append(list_path)

    def build_subhub_marker_template(self, project_name: str) -> dict[str, object]:
        return {
            "para_root": ".",
            "obsidian": {
                "vault": f"~/Obsidian/{project_name}",
                "vault_name": project_name,
            },
            "onenote": {
                "enabled": False,
                "base_url": "https://www.onenote.com/notebooks/example",
                "topics_list": DEFAULT_TOPIC_LIST_NAMES["onenote"],
            },
            "outlook": {
                "enabled": False,
                "base_url": "https://outlook.office.com/mail",
                "topics_list": DEFAULT_TOPIC_LIST_NAMES["outlook"],
            },
            "plm": {
                "enabled": False,
                "base_url": "https://plm.example.com",
                "topics_list": DEFAULT_TOPIC_LIST_NAMES["plm"],
            },
        }

    def remove_created_paths(self, created: list[Path]) -> None:
        """Take back what a failed creation made, newest first.

        Only what this run created is in the list, and a folder goes only once
        empty, so nothing the user put there in the meantime is lost.
        """
        for path in reversed(created):
            try:
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                else:
                    path.unlink()
            except OSError:
                continue

    def add_new_topic_to_list(self, spec: SourceSpec, plan: CreationPlan) -> str | None:
        path = spec.topics_path(plan.hub)
        data, _original, error = self.load_or_create_json_target(path)
        if error or data is None:
            return error or f"{path.name} is empty or invalid."
        if self.topic_exists_in_json_data(data, plan.name):
            return f"'{plan.name}' already exists in {path.name}."
        added, add_error = self.add_topic_to_json_data(data, plan.name, plan.state)
        if add_error or not added:
            return add_error or f"Unable to add '{plan.name}'."
        return self.write_json_target(path, data)
