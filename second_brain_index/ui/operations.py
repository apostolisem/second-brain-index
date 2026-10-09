from __future__ import annotations

import copy
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config import HubConfig
from ..constants import (
    OTHER_STATE,
    STATE_FOLDERS,
    STATE_ORDER,
    display_name_from_key,
    hub_key_prefix,
    is_hub_key,
    is_other_key,
    make_hub_key,
    make_other_key,
    split_hub_key,
)
from ..indexer import SOURCE_SPECS
from ..json_lists import empty_topics_document, parse_topics_json, write_text_atomic
from ..fsops import MergeReport, merge_tree, paths_are_same_entry, safe_rename
from ..naming import normalize_key, validate_portable_name
from ..models import Topic, TopicLocation


@dataclass
class RenameOperation:
    """One folder-level step of a rename.

    kind is "rename" when the destination is free and "merge" when it already
    holds a folder whose contents have to be folded in.
    """

    kind: str
    source: Path
    target: Path
    report: MergeReport | None = None

@dataclass
class RenamePreview:
    new_name: str
    new_key: str
    operations: list[RenameOperation]
    details: list[str]
    manual_actions: list[str]
    error: str | None = None
    is_merge: bool = False
    collision_key: str | None = None
    section_choices: list[str] = field(default_factory=list)
    target_state: str | None = None

    @property
    def conflicts(self) -> list[Path]:
        return [
            path
            for operation in self.operations
            if operation.report
            for path in operation.report.conflicts
        ]
# Sentinel for "do not consolidate a cross-section merge into one section".
KEEP_SECTIONS = "Keep current sections"

class UndoAborted(Exception):
    """No folder could be moved back, so the undo left everything as it was."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


def merge_conflict_dirname(old_name: str) -> str:
    return f"Merged from {old_name}"

def clean_manual_action_text(action: str) -> str:
    cleaned = action.strip()
    while cleaned.startswith("-"):
        cleaned = cleaned[1:].strip()
    return cleaned


class TopicOperationsMixin:
    """Rename, merge, move and undo logic for MainWindow. No widgets here."""

    def add_topic_to_json_data(
        self, data: object, topic_name: str, state: str
    ) -> tuple[bool, str | None]:
        cleaned = topic_name.strip()
        if not cleaned:
            return False, "Topic name is empty."

        if isinstance(data, list):
            return self.add_topic_to_entries(data, cleaned)

        if isinstance(data, dict):
            if "sections" in data:
                return self.add_topic_to_sections(data, cleaned, state)
            if "topics" in data:
                topics = data.get("topics")
                if not isinstance(topics, list):
                    return False, "Topics list is not a list."
                return self.add_topic_to_entries(topics, cleaned)

            return self.add_topic_to_named_lists(data, cleaned, state)

        return False, "Unsupported JSON format."

    def add_topic_to_sections(
        self, data: dict[str, object], topic_name: str, state: str
    ) -> tuple[bool, str | None]:
        sections = data.get("sections")
        if not isinstance(sections, list):
            return False, "Sections value is not a list."

        section_names = self.section_names_for_state(state)
        if not section_names:
            return False, "Unable to determine a section for this topic."

        section = None
        for candidate in section_names:
            section = self.find_section_by_name(sections, candidate)
            if section:
                break

        if section is None:
            section = {"name": section_names[0], "topics": []}
            sections.append(section)

        topics = section.get("topics")
        if topics is None:
            topics = []
            section["topics"] = topics
        if not isinstance(topics, list):
            return False, "Section topics value is not a list."
        return self.add_topic_to_entries(topics, topic_name)

    def add_topic_to_named_lists(
        self, data: dict[str, object], topic_name: str, state: str
    ) -> tuple[bool, str | None]:
        section_names = self.section_names_for_state(state)
        list_keys = [
            key
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, list)
        ]
        for candidate in section_names:
            for key, value in data.items():
                if not isinstance(key, str):
                    continue
                if key.strip().casefold() != candidate.casefold():
                    continue
                if isinstance(value, list):
                    return self.add_topic_to_entries(value, topic_name)
                if isinstance(value, dict):
                    # A section object, which the loaders read as well.
                    return self.add_topic_to_entries(
                        value.setdefault("topics", []), topic_name
                    )
                # Replacing it would throw away whatever it holds.
                return False, f"'{key}' is neither a list nor a section."

        if len(list_keys) == 1:
            return self.add_topic_to_entries(data[list_keys[0]], topic_name)

        if section_names:
            data[section_names[0]] = []
            return self.add_topic_to_entries(data[section_names[0]], topic_name)

        return False, "Unable to locate a topics list in the JSON file."

    def add_topic_to_entries(
        self, entries: object, topic_name: str
    ) -> tuple[bool, str | None]:
        if not isinstance(entries, list):
            return False, "Topics list is not a list."
        if self.topic_in_entries(entries, topic_name):
            return False, f"'{topic_name}' already exists in the list."

        has_dict = any(isinstance(entry, dict) for entry in entries)
        has_str = any(isinstance(entry, str) for entry in entries)
        if has_dict and not has_str:
            entries.append({"name": topic_name})
        else:
            entries.append(topic_name)
        return True, None

    def find_section_by_name(
        self, sections: object, target_name: str
    ) -> dict[str, object] | None:
        if not isinstance(sections, list):
            return None
        for section in sections:
            if not isinstance(section, dict):
                continue
            name = self.clean_topic_string(section.get("name") or section.get("section"))
            if name and name.casefold() == target_name.casefold():
                return section
            nested = self.find_section_by_name(section.get("sections"), target_name)
            if nested:
                return nested
        return None

    def section_names_for_state(self, state: str) -> list[str]:
        names = []
        cleaned = state.strip() if state else ""
        mapped = STATE_FOLDERS.get(cleaned)
        if mapped:
            names.append(mapped)
        if cleaned and cleaned not in names:
            names.append(cleaned)
        return names

    def uses_named_lists(self, data: dict[str, object]) -> bool:
        """Whether the loaders read this document through its named lists.

        They fall back to those whenever "topics" and "sections" yield no
        topic, present or not; an edit has to look in the same place or a
        topic the app shows could be neither renamed nor removed.
        """
        return not (
            self.entries_hold_topics(data.get("topics"))
            or self.sections_hold_topics(data.get("sections"))
        )

    def entries_hold_topics(self, entries: object) -> bool:
        if not isinstance(entries, list):
            return False
        return any(self.topic_entry_name(entry) for entry in entries)

    def sections_hold_topics(self, sections: object) -> bool:
        if not isinstance(sections, list):
            return False
        return any(
            self.entries_hold_topics(section.get("topics"))
            or self.sections_hold_topics(section.get("sections"))
            for section in sections
            if isinstance(section, dict)
        )

    def topic_exists_in_json_data(self, data: object, topic_name: str) -> bool:
        if isinstance(data, list):
            return self.topic_in_entries(data, topic_name)
        if isinstance(data, dict):
            if self.topic_in_entries(data.get("topics"), topic_name):
                return True
            if self.topic_in_sections(data.get("sections"), topic_name):
                return True
            if not self.uses_named_lists(data):
                return False
            for value in data.values():
                if isinstance(value, list) and self.topic_in_entries(value, topic_name):
                    return True
                if isinstance(value, dict) and self.topic_exists_in_json_data(
                    value, topic_name
                ):
                    return True
        return False

    def topic_in_sections(self, sections: object, topic_name: str) -> bool:
        if not isinstance(sections, list):
            return False
        for section in sections:
            if not isinstance(section, dict):
                continue
            if self.topic_in_entries(section.get("topics"), topic_name):
                return True
            if self.topic_in_sections(section.get("sections"), topic_name):
                return True
        return False

    def topic_in_entries(self, entries: object, topic_name: str) -> bool:
        if not isinstance(entries, list):
            return False
        target = topic_name.strip().casefold()
        if not target:
            return False
        for entry in entries:
            name = self.topic_entry_name(entry)
            if name and name.casefold() == target:
                return True
        return False

    def topic_entry_name(self, entry: object) -> str | None:
        if isinstance(entry, str):
            return self.clean_topic_string(entry)
        if isinstance(entry, dict):
            for key in ("name", "topic", "title"):
                value = entry.get(key)
                cleaned = self.clean_topic_string(value)
                if cleaned:
                    return cleaned
        return None

    def clean_topic_string(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned if cleaned else None

    def take_topic_entries(self, entries: object, topic_name: str) -> list[object]:
        """Remove and return a topic's entries, named the way the loaders do.

        Exact names only: "Alpha" and "alpha" are two topics to the loaders, so
        moving one must leave the other where it is.
        """
        if not isinstance(entries, list):
            return []
        taken = [
            entry for entry in entries if self.entry_names_topic(entry, topic_name)
        ]
        if taken:
            entries[:] = [
                entry
                for entry in entries
                if not self.entry_names_topic(entry, topic_name)
            ]
        return taken

    def ensure_topic_in_entries(self, entries: object, topic_name: str) -> bool:
        if not isinstance(entries, list):
            return False
        if self.topic_in_entries(entries, topic_name):
            return False
        has_dict = any(isinstance(entry, dict) for entry in entries)
        has_str = any(isinstance(entry, str) for entry in entries)
        if has_dict and not has_str:
            entries.append({"name": topic_name})
        else:
            entries.append(topic_name)
        return True

    def take_topic_from_sections(
        self, sections: object, topic_name: str, keep: list[object] | None = None
    ) -> list[object]:
        """Take a topic's entries out of every section except the ``keep`` list."""
        if not isinstance(sections, list):
            return []
        taken: list[object] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            topics = section.get("topics")
            if topics is not keep:
                taken.extend(self.take_topic_entries(topics, topic_name))
            taken.extend(
                self.take_topic_from_sections(
                    section.get("sections"), topic_name, keep
                )
            )
        return taken

    def place_moved_entries(
        self,
        entries: list[object],
        taken: list[object],
        topic_name: str,
        target_state: str,
    ) -> bool:
        """Land a topic in its new section; returns whether anything changed.

        The entries themselves move, so a path or any other field they carry
        survives; only a path that names the old section is pointed at the new.
        """
        if not taken:
            if any(self.entry_names_topic(entry, topic_name) for entry in entries):
                return False
            return self.ensure_topic_in_entries(entries, topic_name)
        for entry in taken:
            if isinstance(entry, dict):
                self.retarget_entry_path(entry, target_state)
            if entry not in entries:
                entries.append(entry)
        return True

    def retarget_entry_path(self, entry: dict[str, object], target_state: str) -> None:
        """Swap a leading PARA folder in an entry's path for the target's.

        The loaders read an entry's section from that first segment, so a path
        left as it was would keep the topic in the section it just left.
        """
        path_value = entry.get("path")
        target_folder = STATE_FOLDERS.get(target_state)
        if not isinstance(path_value, str) or not target_folder:
            return
        separator = next(
            (index for index, char in enumerate(path_value) if char in "/\\"), None
        )
        if separator is None:
            return
        head = path_value[:separator].strip()
        folders = {folder.casefold() for folder in STATE_FOLDERS.values()}
        if not head or head.casefold() not in folders:
            return
        entry["path"] = (
            path_value[:separator].replace(head, target_folder, 1)
            + path_value[separator:]
        )

    def journal_rename(self, **payload: object) -> bool:
        """Persist enough to reverse the operation exactly.

        The merge never overwrites or deletes a file, so every move recorded
        here has a clean inverse; only duplicate bookmarks are removed, and
        those are stored in full so they can be re-inserted.
        """
        serialisable = dict(payload)
        serialisable["moves"] = [
            [str(source), str(target)] for source, target in payload["moves"]
        ]
        serialisable["boundaries"] = [str(path) for path in payload["boundaries"]]
        # The text as written lets undo notice a list that changed afterwards.
        serialisable["json_targets"] = [
            [label, str(path), original, self.serialize_json_target(data)]
            for label, path, data, original in payload["json_targets"]
        ]
        try:
            self.link_store.record_operation(str(payload["kind"]), serialisable)
        except Exception:
            # An unrecordable journal must not fail an operation that already
            # succeeded on disk; the user simply loses the undo affordance.
            self.clear_undo_prompt()
            return False
        return True

    def restore_topic_tags(
        self,
        old_key: str,
        new_key: str,
        old_tags: list[str],
        new_tags: list[str],
    ) -> None:
        """Split a merged tag set back onto the two topics.

        reassign_topic_key folds the old topic's tags into the new one and then
        drops the old rows, so undo has to replay the two snapshots taken before
        the merge rather than trying to read intent out of the result.
        """
        for tag in old_tags:
            self.link_store.add_topic_tag(old_key, tag)
        kept = {tag.casefold() for tag in new_tags}
        for tag in old_tags:
            if tag.casefold() not in kept:
                self.link_store.remove_topic_tag(new_key, tag)

    def serialize_json_target(self, data: object) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)

    def undo_blockers(self, payload: dict[str, object]) -> list[str]:
        """Why the journalled operation can no longer be reversed safely.

        The undo button outlives later work, so the disk may have moved on:
        undoing then would overwrite newer list edits or hand the topic's
        bookmarks and tags to a name that does not come back.
        """
        blockers: list[str] = []
        moves = [
            (Path(source), Path(target))
            for source, target in payload.get("moves", [])
        ]
        present = [target.exists() for _source, target in moves]
        # A rename moved whole folders, so each one has to be there; a merge
        # moved single files, and losing one of those need not stop the rest.
        intact = all(present) if payload.get("kind") == "rename" else any(present)
        if moves and not intact:
            blockers.append("The folders are no longer where the operation left them.")
        for source, target in moves:
            # Something else took the old place: moving back would fail, and the
            # bookmarks and tags would be handed to that unrelated folder.
            if os.path.lexists(source) and not paths_are_same_entry(source, target):
                blockers.append(f"{source} exists again and is in the way.")
        for entry in payload.get("json_targets", []):
            if len(entry) < 4:
                continue  # journalled before the written text was recorded
            label, path_value, written = entry[0], entry[1], entry[3]
            path = Path(path_value)
            try:
                current = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                blockers.append(f"{label}: Unable to read {path.name}: {exc}")
                continue
            if current != written:
                blockers.append(f"{label}: {path.name} was changed afterwards.")
        return blockers

    def apply_undo(self, payload: dict[str, object]) -> list[str]:
        errors: list[str] = []
        moves = [
            (Path(source), Path(target))
            for source, target in payload.get("moves", [])
        ]
        boundaries = [Path(value) for value in payload.get("boundaries", [])]
        move_errors = self.reverse_moves(moves, boundaries)
        if moves and len(move_errors) >= len(moves):
            # Nothing came back, so the lists and the bookmarks stay with the
            # folders: restoring them would point at a name that is not there.
            raise UndoAborted(move_errors)
        errors.extend(move_errors)

        for entry in payload.get("json_targets", []):
            label, path_value, original = entry[:3]
            try:
                write_text_atomic(Path(path_value), original)
            except OSError as exc:
                errors.append(f"{label}: Unable to restore {path_value}: {exc}")

        old_key = str(payload.get("old_key") or "")
        new_key = str(payload.get("new_key") or "")
        try:
            if payload.get("tagged_previous_name"):
                self.link_store.remove_topic_tag(new_key, str(payload["old_name"]))
            if payload.get("kind") == "rename":
                # Nothing else lived under the new key, so whatever is there
                # now, bookmarks and tags added since included, is this topic's.
                self.link_store.reassign_topic_key(new_key, old_key)
                if payload.get("hub_renamed"):
                    self.link_store.reassign_topic_prefix(
                        hub_key_prefix(new_key), hub_key_prefix(old_key)
                    )
            else:
                self.split_merged_metadata(payload, old_key, new_key)
            self.link_store.restore_links(
                old_key, list(payload.get("removed_links", []))
            )
            self.link_store.restore_locations(
                old_key, list(payload.get("removed_locations", []))
            )
        except Exception as exc:
            errors.append(f"Topic metadata could not be restored: {exc}")
        return errors

    def split_merged_metadata(
        self, payload: dict[str, object], old_key: str, new_key: str
    ) -> None:
        """Hand a merged topic's migrated rows back, leaving the survivor's."""
        surviving = set(self.link_store.link_ids_for_topic(new_key))
        self.link_store.reassign_links_by_id(
            [
                link_id
                for link_id in payload.get("migrated_link_ids", [])
                if link_id in surviving
            ],
            old_key,
        )
        surviving = set(self.link_store.location_ids_for_topic(new_key))
        self.link_store.reassign_locations_by_id(
            [
                location_id
                for location_id in payload.get("migrated_location_ids", [])
                if location_id in surviving
            ],
            old_key,
        )
        self.restore_topic_tags(
            old_key,
            new_key,
            list(payload.get("old_tags", [])),
            list(payload.get("new_tags", [])),
        )
        self.link_store.set_pin_rank(new_key, payload.get("new_pin_rank"))
        self.link_store.set_pin_rank(old_key, payload.get("old_pin_rank"))

    def should_tag_previous_name(self, old_name: str, new_name: str) -> bool:
        # Case-only renames carry no history: tags are UNIQUE COLLATE NOCASE, so
        # the tag would render as a duplicate of the topic's current name.
        return old_name.casefold() != new_name.casefold()

    def record_previous_name_tag(self, topic_key: str, old_name: str) -> str | None:
        """Attach the pre-rename name as a tag. Returns a message on failure."""
        try:
            self.link_store.add_topic_tag(topic_key, old_name)
        except Exception as exc:
            return f"Could not tag the topic with its previous name '{old_name}': {exc}"
        return None

    def build_rename_preview(
        self,
        old_key: str,
        topic: Topic,
        old_name: str,
        new_name: str,
        target_state: str | None = None,
    ) -> RenamePreview:
        def blocked(message: str, new_key: str = old_key) -> RenamePreview:
            return RenamePreview(
                new_name=new_name,
                new_key=new_key,
                operations=[],
                details=[],
                manual_actions=[],
                error=message,
            )

        if new_name == old_name and target_state is None:
            return blocked("Enter a different topic name.")

        error_message = self.validate_topic_name(new_name)
        if error_message:
            return blocked(error_message)

        if is_other_key(old_key):
            new_key = make_other_key(new_name)
        elif is_hub_key(old_key):
            new_key = make_hub_key(split_hub_key(old_key)[0], new_name)
        else:
            new_key = new_name

        collision_key = self.find_colliding_topic_key(new_key, old_key)
        if collision_key is not None:
            blocker = self.merge_blocker(old_key, collision_key)
            if blocker:
                return blocked(blocker, new_key)
            # A collision is an intent to merge, not a dead end: fold this
            # topic's folders, sources and metadata into the existing one.
            new_key = collision_key
            new_name = display_name_from_key(collision_key)

        manual_actions = self.collect_manual_rename_actions(
            topic, old_name, new_name
        )
        collision_topic = (
            self.topics_by_key.get(collision_key) if collision_key else None
        )
        section_choices, target_state = self.merge_section_choices(
            topic, collision_topic, target_state
        )
        # The destination's own folders go first so they claim the target path
        # before the renamed folder is merged into it.
        rename_locations = self.locations_to_consolidate(
            collision_topic, target_state
        )
        rename_locations += self.movable_locations(topic)
        operations, validation_errors = self.collect_rename_operations(
            rename_locations,
            new_name,
            allow_merge=collision_key is not None,
            target_state=target_state,
            conflict_dirname=merge_conflict_dirname(old_name),
        )
        if validation_errors:
            return RenamePreview(
                new_name=new_name,
                new_key=new_key,
                operations=operations,
                details=validation_errors,
                manual_actions=manual_actions,
                error="\n".join(validation_errors),
                collision_key=collision_key,
            )

        details = self.describe_rename_preview(
            topic, old_name, new_name, operations, collision_key, target_state
        )
        return RenamePreview(
            new_name=new_name,
            new_key=new_key,
            operations=operations,
            details=details,
            manual_actions=manual_actions,
            error=None,
            is_merge=collision_key is not None,
            collision_key=collision_key,
            section_choices=section_choices,
            target_state=target_state,
        )

    def movable_locations(self, topic: Topic | None) -> list[TopicLocation]:
        """Folders a rename may touch, skipping Others and JSON-only sources."""
        if topic is None:
            return []
        return [
            loc
            for loc in topic.locations
            if loc.source in {"filesystem", "obsidian"} and loc.state != OTHER_STATE
        ]

    def locations_to_consolidate(
        self, collision_topic: Topic | None, target_state: str | None
    ) -> list[TopicLocation]:
        """Destination folders that must move for the merge to land in one section.

        Without this the chosen section would only move the renamed folder and
        quietly leave the destination behind in its own section.
        """
        if not target_state or target_state == KEEP_SECTIONS:
            return []
        return [
            loc
            for loc in self.movable_locations(collision_topic)
            if loc.state != target_state
        ]

    def merge_blocker(self, old_key: str, collision_key: str) -> str | None:
        """Reject merges the app cannot carry out safely."""
        for key in (old_key, collision_key):
            if key in self.hubs_by_project_key:
                name = display_name_from_key(key)
                return (
                    f"'{name}' is a Sub-Hub. Sub-Hubs cannot be merged "
                    "automatically because each one owns a .parahub.json "
                    "configuration; move its topics by hand first."
                )
        if is_other_key(old_key) != is_other_key(collision_key):
            return (
                "A topic in Others cannot be merged with a PARA topic."
            )
        return None

    def merge_section_choices(
        self,
        topic: Topic,
        collision_topic: Topic | None,
        target_state: str | None,
    ) -> tuple[list[str], str | None]:
        """Offer a landing section only when the merge actually spans sections.

        Folders in different PARA sections never collide on disk, so the merge
        would otherwise silently produce one topic flagged as inconsistent.
        """
        if collision_topic is None:
            return [], None
        source_states = {
            loc.state for loc in topic.locations if loc.state != OTHER_STATE
        }
        destination_states = {
            loc.state
            for loc in collision_topic.locations
            if loc.state != OTHER_STATE
        }
        combined = source_states | destination_states
        if len(combined) < 2:
            return [], None
        choices = [state for state in STATE_ORDER if state in combined]
        choices.append(KEEP_SECTIONS)
        default = collision_topic.display_state
        if target_state in choices and target_state != KEEP_SECTIONS:
            return choices, target_state
        if target_state == KEEP_SECTIONS:
            return choices, KEEP_SECTIONS
        if target_state is None and default in choices:
            return choices, default
        return choices, KEEP_SECTIONS

    def describe_rename_preview(
        self,
        topic: Topic,
        old_name: str,
        new_name: str,
        operations: list[RenameOperation],
        collision_key: str | None,
        target_state: str | None,
    ) -> list[str]:
        merging = collision_key is not None
        verb = "Merge" if merging else "Rename"
        details = [f"{verb} '{old_name}' {'into' if merging else 'to'} '{new_name}'."]

        renames = [op for op in operations if op.kind == "rename"]
        merges = [op for op in operations if op.kind == "merge"]
        effect_parts = []
        if renames:
            effect_parts.append(f"rename {len(renames)} folder(s) on disk")
        if merges:
            effect_parts.append(f"merge {len(merges)} folder(s) on disk")
        effect_parts.append("move bookmarks and tags to the new topic")
        if self.should_tag_previous_name(old_name, new_name):
            effect_parts.append(f"tag the topic with its previous name '{old_name}'")
        topic_hub = self.hub_for_topic(topic)
        for spec in SOURCE_SPECS:
            if spec.enabled(topic_hub):
                effect_parts.append(f"update {spec.name} topics list")
        details.append("This will " + ", ".join(effect_parts) + ".")

        if target_state and target_state != KEEP_SECTIONS:
            details.append(f"Merged topic lands in: {STATE_FOLDERS[target_state]}.")
        elif target_state == KEEP_SECTIONS:
            details.append(
                "Folders stay in their current sections; the merged topic will "
                "span more than one section."
            )

        if renames:
            details.append("")
            details.append("Folders to rename:")
            details.extend(f"  {op.source} -> {op.target}" for op in renames)
        if merges:
            details.append("")
            details.append("Folders to merge:")
            details.extend(f"  {op.source} -> {op.target}" for op in merges)

        conflicts = [
            path for op in merges if op.report for path in op.report.conflicts
        ]
        if conflicts:
            details.append("")
            details.append(
                f"Conflicting files kept in 'Merged from {old_name}':"
            )
            details.extend(f"  {path}" for path in conflicts)
        elif merges:
            details.append("")
            details.append("No file conflicts; nothing will be overwritten.")
        return details

    def rebuild_normalized_topic_keys(self) -> None:
        """Index topic keys case- and composition-insensitively.

        Collisions have to be spotted the way the least forgiving filesystem
        would see them, so a Linux vault refuses names that would clash once it
        is opened on Windows.
        """
        self.topic_keys_by_normalized = {
            normalize_key(key): key for key in self.topics_by_key
        }

    def find_colliding_topic_key(self, new_key: str, old_key: str) -> str | None:
        existing = self.topic_keys_by_normalized.get(normalize_key(new_key))
        if existing is None or existing == old_key:
            return None
        return existing

    def validate_topic_name(self, name: str) -> str | None:
        return validate_portable_name(name)

    def collect_manual_rename_actions(
        self, topic: Topic, old_name: str, new_name: str
    ) -> list[str]:
        actions: list[str] = []
        hub = self.hub_for_topic(topic)
        for spec in SOURCE_SPECS:
            paths = spec.paths_of(topic)
            if not spec.enabled(hub) or not paths:
                continue
            states = spec.infer_states(paths, old_name)
            sections = self.format_section_list(states)
            actions.append(
                f"- {spec.name}: rename the corresponding path in the {spec.name} "
                f"app under {sections} from '{old_name}' to '{new_name}'."
            )
        return actions

    def format_section_list(self, states: set[str]) -> str:
        labels: list[str] = []
        for state in STATE_ORDER:
            if state == OTHER_STATE:
                continue
            if state in states:
                section_name = STATE_FOLDERS.get(state, state)
                if section_name != state:
                    labels.append(f"{state} ({section_name})")
                else:
                    labels.append(state)

        if not labels and states:
            for state in sorted(states, key=str.casefold):
                if state == OTHER_STATE:
                    continue
                section_name = STATE_FOLDERS.get(state, state)
                if section_name != state:
                    labels.append(f"{state} ({section_name})")
                else:
                    labels.append(state)

        if not labels:
            return "the relevant section"

        return f"section(s): {', '.join(labels)}"

    def get_json_source_states(self, topic: Topic) -> list[tuple[str, set[str]]]:
        sources: list[tuple[str, set[str]]] = []
        hub = self.hub_for_topic(topic)
        for spec in SOURCE_SPECS:
            paths = spec.paths_of(topic)
            if spec.enabled(hub) and paths:
                sources.append((spec.name, spec.infer_states(paths, topic.name)))
        return sources

    def topic_has_unarchived_json(self, topic: Topic) -> bool:
        for _name, states in self.get_json_source_states(topic):
            if not states or any(state != "Archive" for state in states):
                return True
        return False

    def rename_target_path(
        self, location: TopicLocation, new_name: str, target_state: str | None
    ) -> Path:
        """Where a location's folder should end up after the rename.

        A chosen landing section moves the folder sideways into that section's
        folder as well as renaming it, so a merge that spans sections leaves one
        consolidated topic instead of an inconsistent one.
        """
        target = location.path.with_name(new_name)
        if (
            target_state
            and target_state != KEEP_SECTIONS
            and location.state != OTHER_STATE
            and location.state != target_state
        ):
            state_folder = STATE_FOLDERS.get(target_state)
            current_folder = STATE_FOLDERS.get(location.state)
            if state_folder and current_folder:
                section_root = location.path.parent.parent / state_folder
                target = section_root / new_name
        return target

    def collect_rename_operations(
        self,
        locations: list[TopicLocation],
        new_name: str,
        allow_merge: bool = False,
        target_state: str | None = None,
        conflict_dirname: str = "",
    ) -> tuple[list[RenameOperation], list[str]]:
        operations: list[RenameOperation] = []
        errors: list[str] = []
        planned: dict[Path, Path] = {}
        for location in locations:
            source = location.path
            if not source.exists():
                errors.append(f"Missing source folder: {source}")
                continue
            if not source.is_dir():
                errors.append(f"Source is not a folder: {source}")
                continue
            target = self.rename_target_path(location, new_name, target_state)
            if source == target:
                continue
            if source.is_symlink() and (target in planned or target.is_dir()):
                if not paths_are_same_entry(source, target):
                    # Merging would empty the folder the link points at.
                    errors.append(
                        f"{source} is a symbolic link; merge its contents by hand."
                    )
                    continue
            if target in planned:
                # An earlier operation will land a folder here; plan the merge
                # against that folder's current contents.
                occupant = planned[target]
                report = merge_tree(source, occupant, conflict_dirname, apply=False)
                report.conflicts = [
                    target / path.relative_to(occupant) for path in report.conflicts
                ]
                operations.append(
                    RenameOperation(
                        kind="merge", source=source, target=target, report=report
                    )
                )
                continue
            if target.exists():
                if paths_are_same_entry(source, target):
                    # Case-only rename of the very same folder.
                    operations.append(
                        RenameOperation(kind="rename", source=source, target=target)
                    )
                    continue
                if not target.is_dir():
                    errors.append(f"Destination is not a folder: {target}")
                    continue
                if not allow_merge:
                    errors.append(f"Destination already exists: {target}")
                    continue
                try:
                    report = merge_tree(
                        source, target, conflict_dirname, apply=False
                    )
                except OSError as exc:
                    errors.append(f"Cannot plan merge for {source}: {exc}")
                    continue
                operations.append(
                    RenameOperation(
                        kind="merge", source=source, target=target, report=report
                    )
                )
                continue
            planned[target] = source
            operations.append(
                RenameOperation(kind="rename", source=source, target=target)
            )
        return operations, errors

    def execute_rename_operations(
        self, operations: list[RenameOperation], conflict_dirname: str
    ) -> tuple[list[str], list[RenameOperation], list[tuple[Path, Path]]]:
        """Apply folder operations, rolling every one of them back on failure.

        Returns (errors, completed operations, individual file moves). The moves
        are what makes a merge undoable: nothing is ever overwritten, so
        replaying them backwards restores the original layout exactly.
        """
        if not operations:
            return [], [], []
        errors: list[str] = []
        completed: list[RenameOperation] = []
        moves: list[tuple[Path, Path]] = []
        for operation in operations:
            try:
                if operation.kind == "merge":
                    # Owned here, not returned: the moves made before a failure
                    # are then still known and can be reversed.
                    operation.report = MergeReport()
                    operation.target.mkdir(parents=True, exist_ok=True)
                    merge_tree(
                        operation.source,
                        operation.target,
                        conflict_dirname,
                        apply=True,
                        report=operation.report,
                    )
                    moves.extend(operation.report.moves)
                else:
                    operation.target.parent.mkdir(parents=True, exist_ok=True)
                    safe_rename(operation.source, operation.target)
                    moves.append((operation.source, operation.target))
                completed.append(operation)
            except Exception as exc:
                errors.append(f"{operation.source} -> {operation.target}: {exc}")
                if operation.kind == "merge":
                    completed.append(operation)
                break

        if errors:
            rollback_errors = self.rollback_rename_operations(completed)
            for error in rollback_errors:
                errors.append(f"Rollback failed: {error}")
            return errors, [], []
        return errors, completed, moves

    def rollback_rename_operations(
        self, completed: list[RenameOperation]
    ) -> list[str]:
        errors: list[str] = []
        for operation in reversed(completed):
            try:
                if operation.kind == "merge" and operation.report:
                    errors.extend(
                        self.reverse_moves(
                            operation.report.moves, [operation.target]
                        )
                    )
                else:
                    safe_rename(operation.target, operation.source)
            except Exception as exc:
                errors.append(f"{operation.target} -> {operation.source}: {exc}")
        return errors

    def reverse_moves(
        self, moves: list[tuple[Path, Path]], boundaries: list[Path]
    ) -> list[str]:
        """Undo file moves in reverse, dropping the folders they created.

        Each move is pruned as soon as it is reversed: a later folder rename can
        move a boundary out from under a directory that still needs cleaning.
        """
        errors: list[str] = []
        for source, target in reversed(moves):
            source_path = Path(source)
            target_path = Path(target)
            try:
                if not target_path.exists():
                    errors.append(f"Missing moved item: {target_path}")
                    continue
                source_path.parent.mkdir(parents=True, exist_ok=True)
                safe_rename(target_path, source_path)
            except Exception as exc:
                errors.append(f"{target_path} -> {source_path}: {exc}")
                continue
            self.prune_empty_parents(target_path.parent, boundaries)
        return errors

    def prune_empty_parents(self, directory: Path, boundaries: list[Path]) -> None:
        """Remove emptied folders, never touching a boundary or anything above it.

        The boundaries are the folders a merge wrote into; without them the walk
        could climb out of the topic and delete a PARA section or a vault root.
        """
        current = directory
        while self.is_inside_any(current, boundaries):
            try:
                if any(current.iterdir()):
                    return
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def is_inside_any(self, path: Path, boundaries: list[Path]) -> bool:
        for boundary in boundaries:
            try:
                relative = path.relative_to(boundary)
            except ValueError:
                continue
            if relative.parts:
                return True
        return False

    def prepare_json_rename_targets(
        self, hub: HubConfig
    ) -> tuple[list[tuple[str, Path, object, str]], list[str]]:
        targets: list[tuple[str, Path, object, str]] = []
        errors: list[str] = []

        for spec in SOURCE_SPECS:
            if not spec.enabled(hub):
                continue
            path = spec.topics_path(hub)
            if not path.exists():
                # A list that was never created has nothing to rename; the
                # loaders treat it as empty too.
                continue
            data, original, error = self.load_json_target(path)
            if error:
                errors.append(f"{spec.name}: {error}")
            else:
                targets.append((spec.name, path, data, original))

        return targets, errors

    def prepare_json_archive_targets(
        self, topics: list[Topic]
    ) -> tuple[list[tuple[str, Path, object, str, list[str]]], list[str]]:
        # Each target carries the topic names it applies to, so batches that
        # span the root hub and sub-hubs only touch their own JSON sources.
        targets: list[tuple[str, Path, object, str, list[str]]] = []
        errors: list[str] = []
        names_by_path: dict[Path, list[str]] = {}
        target_by_path: dict[Path, tuple[str, Path, object, str]] = {}
        failed_paths: set[Path] = set()

        for topic in topics:
            hub = self.hub_for_topic(topic)
            for spec in SOURCE_SPECS:
                if not spec.enabled(hub) or not spec.paths_of(topic):
                    continue
                path = spec.topics_path(hub)
                if path in failed_paths:
                    continue
                if path not in target_by_path:
                    data, original, error = self.load_json_target(path)
                    if error:
                        errors.append(f"{spec.name}: {error}")
                        failed_paths.add(path)
                        continue
                    target_by_path[path] = (spec.name, path, data, original)
                    names_by_path[path] = []
                if topic.name not in names_by_path[path]:
                    names_by_path[path].append(topic.name)

        for path, (label, target_path, data, original) in target_by_path.items():
            targets.append((label, target_path, data, original, names_by_path[path]))

        return targets, errors

    def load_json_target(
        self, path: Path
    ) -> tuple[object | None, str | None, str | None]:
        if not path.exists():
            return None, None, f"{path.name} not found."
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            return None, None, f"Unable to read {path.name}: {exc}"
        try:
            data = parse_topics_json(content)
        except json.JSONDecodeError as exc:
            return None, None, f"Invalid JSON in {path.name}: {exc}"
        return data, content, None

    def load_or_create_json_target(
        self, path: Path
    ) -> tuple[object | None, str | None, str | None]:
        """Like load_json_target, but a missing list starts out empty."""
        if not path.exists():
            return empty_topics_document(), None, None
        return self.load_json_target(path)

    def write_json_target(self, path: Path, data: object) -> str | None:
        """Write a list back; returns what went wrong, if anything."""
        try:
            payload = self.serialize_json_target(data)
        except (TypeError, ValueError) as exc:
            return f"Unable to serialize {path.name}: {exc}"
        try:
            write_text_atomic(path, payload)
        except OSError as exc:
            return f"Unable to write {path.name}: {exc}"
        return None

    def write_json_snapshot(
        self, path: Path, payload: str, original: str
    ) -> str | None:
        """Write a list unless it changed on disk since ``original`` was read.

        The data being written was derived from that reading; another editor,
        a sync client or a second window may have saved in between, and writing
        anyway would silently drop what they added.
        """
        try:
            current = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            return f"Unable to re-read {path.name}: {exc}"
        if current != original:
            return (
                f"{path.name} was changed by something else in the meantime; "
                "it was left as it is."
            )
        try:
            write_text_atomic(path, payload)
        except OSError as exc:
            return f"Unable to write {path.name}: {exc}"
        return None

    def remove_topic_from_json_data(self, data: object, name: str) -> int:
        """Delete every entry for a topic, in whichever section it is listed."""
        if isinstance(data, list):
            return self.remove_topic_entries(data, name)
        if isinstance(data, dict):
            count = 0
            named_lists = self.uses_named_lists(data)
            if "topics" in data:
                count += self.remove_topic_entries(data.get("topics"), name)
            if "sections" in data:
                count += self.remove_topic_sections(data.get("sections"), name)
            if named_lists:
                for value in data.values():
                    if isinstance(value, list):
                        count += self.remove_topic_entries(value, name)
                    elif isinstance(value, dict):
                        count += self.remove_section_like(value, name)
            return count
        return 0

    def remove_topic_sections(self, sections: object, name: str) -> int:
        if not isinstance(sections, list):
            return 0
        return sum(self.remove_section_like(section, name) for section in sections)

    def remove_section_like(self, section: object, name: str) -> int:
        if not isinstance(section, dict):
            return 0
        count = 0
        if "topics" in section:
            count += self.remove_topic_entries(section.get("topics"), name)
        if "sections" in section:
            count += self.remove_topic_sections(section.get("sections"), name)
        return count

    def remove_topic_entries(self, entries: object, name: str) -> int:
        if not isinstance(entries, list):
            return 0
        kept = [entry for entry in entries if not self.entry_names_topic(entry, name)]
        removed = len(entries) - len(kept)
        entries[:] = kept
        return removed

    def apply_json_renames(
        self,
        targets: list[tuple[str, Path, object, str]],
        old_name: str,
        new_name: str,
        target_state: str | None = None,
    ) -> tuple[list[str], list[tuple[str, Path, object, str]]]:
        """Rename a topic in each list; ``target_state`` also moves it there.

        A merge that consolidates the folders into one section has to take the
        list entries along, or the merged topic still spans two sections.
        """
        errors: list[str] = []
        written: list[tuple[str, Path, object, str]] = []
        for label, path, data, original in targets:
            try:
                before = self.serialize_json_target(data)
                self.rename_topic_in_json_data(data, old_name, new_name)
                # A rename onto an existing name would otherwise leave the
                # topic listed twice in the same section.
                self.dedupe_topic_in_json_data(data, new_name)
                if target_state and self.json_data_lists_topic(data, new_name):
                    # A list without sections has nowhere to move it to; that
                    # is no reason to fail the rename.
                    self.move_topic_in_json_data(data, new_name, target_state)
                payload = self.serialize_json_target(data)
            except (TypeError, ValueError) as exc:
                errors.append(f"{label}: Unable to serialize {path.name}: {exc}")
                errors.extend(self.restore_json_targets(written))
                return errors, []
            if payload == before:
                continue
            error = self.write_json_snapshot(path, payload, original)
            if error:
                errors.append(f"{label}: {error}")
                errors.extend(self.restore_json_targets(written))
                return errors, []
            written.append((label, path, data, original))
        return errors, written

    def json_data_lists_topic(self, data: object, name: str) -> bool:
        return self.remove_topic_from_json_data(copy.deepcopy(data), name) > 0

    def restore_json_targets(
        self, targets: list[tuple[str, Path, object, str]]
    ) -> list[str]:
        """Put back the lists this operation wrote, and only those.

        A list that no longer holds what was written has been edited since;
        restoring the old text over it would undo somebody else's change.
        """
        errors: list[str] = []
        for label, path, data, original in targets:
            try:
                current = path.read_text(encoding="utf-8-sig")
                if current == original:
                    continue
                if current != self.serialize_json_target(data):
                    errors.append(
                        f"{label}: {path.name} changed again after it was "
                        "written; it was not restored."
                    )
                    continue
                write_text_atomic(path, original)
            except (OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
                errors.append(f"{label}: Unable to restore {path.name}: {exc}")
        return errors

    def dedupe_topic_in_json_data(self, data: object, name: str) -> int:
        if isinstance(data, list):
            return self.dedupe_topic_entries(data, name)
        if isinstance(data, dict):
            count = 0
            named_lists = self.uses_named_lists(data)
            if "topics" in data:
                count += self.dedupe_topic_entries(data.get("topics"), name)
            if "sections" in data:
                count += self.dedupe_topic_sections(data.get("sections"), name)
            if named_lists:
                for value in data.values():
                    if isinstance(value, list):
                        count += self.dedupe_topic_entries(value, name)
                    elif isinstance(value, dict):
                        count += self.dedupe_section_like(value, name)
            return count
        return 0

    def dedupe_topic_sections(self, sections: object, name: str) -> int:
        if not isinstance(sections, list):
            return 0
        return sum(self.dedupe_section_like(section, name) for section in sections)

    def dedupe_section_like(self, section: object, name: str) -> int:
        if not isinstance(section, dict):
            return 0
        count = 0
        if "topics" in section:
            count += self.dedupe_topic_entries(section.get("topics"), name)
        if "sections" in section:
            count += self.dedupe_topic_sections(section.get("sections"), name)
        return count

    def dedupe_topic_entries(self, entries: object, name: str) -> int:
        """Collapse identical repeats of one topic within a single list.

        Entries in different sections are left alone: they mirror a topic that
        legitimately spans PARA sections. So are entries that differ in what
        they carry, such as one topic listed with two paths.
        """
        if not isinstance(entries, list):
            return 0
        seen: list[dict[str, object]] = []
        removed = 0
        index = 0
        while index < len(entries):
            if self.entry_names_topic(entries[index], name):
                details = self.entry_details(entries[index])
                if details in seen:
                    del entries[index]
                    removed += 1
                    continue
                seen.append(details)
            index += 1
        return removed

    def entry_details(self, entry: object) -> dict[str, object]:
        """What an entry carries besides the topic's name."""
        if not isinstance(entry, dict):
            return {}
        details = dict(entry)
        for key in ("name", "topic", "title"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                del details[key]
                break
        return details

    def entry_names_topic(self, entry: object, name: str) -> bool:
        # Same precedence as the loaders: an entry is the topic its first
        # filled-in name key says, not any topic one of its keys mentions.
        return self.topic_entry_name(entry) == name

    def rename_topic_in_json_data(
        self, data: object, old_name: str, new_name: str
    ) -> int:
        if isinstance(data, list):
            return self.rename_topic_entries(data, old_name, new_name)
        if isinstance(data, dict):
            count = 0
            named_lists = self.uses_named_lists(data)
            if "topics" in data:
                count += self.rename_topic_entries(
                    data.get("topics"), old_name, new_name
                )
            if "sections" in data:
                count += self.rename_topic_sections(
                    data.get("sections"), old_name, new_name
                )
            if named_lists:
                for value in data.values():
                    if isinstance(value, list):
                        count += self.rename_topic_entries(value, old_name, new_name)
                    elif isinstance(value, dict):
                        count += self.rename_section_like(value, old_name, new_name)
            return count
        return 0

    def rename_topic_sections(
        self, sections: object, old_name: str, new_name: str
    ) -> int:
        if not isinstance(sections, list):
            return 0
        count = 0
        for section in sections:
            count += self.rename_section_like(section, old_name, new_name)
        return count

    def rename_section_like(
        self, section: object, old_name: str, new_name: str
    ) -> int:
        if not isinstance(section, dict):
            return 0
        count = 0
        if "topics" in section:
            count += self.rename_topic_entries(
                section.get("topics"), old_name, new_name
            )
        if "sections" in section:
            count += self.rename_topic_sections(
                section.get("sections"), old_name, new_name
            )
        return count

    def rename_topic_entries(
        self, entries: object, old_name: str, new_name: str
    ) -> int:
        if not isinstance(entries, list):
            return 0
        count = 0
        for index, entry in enumerate(entries):
            if isinstance(entry, str):
                if entry.strip() == old_name:
                    entries[index] = new_name
                    count += 1
                continue
            if isinstance(entry, dict):
                count += self.rename_topic_entry_dict(entry, old_name, new_name)
        return count

    def rename_topic_entry_dict(
        self, entry: dict[str, object], old_name: str, new_name: str
    ) -> int:
        count = 0
        for key in ("name", "topic", "title"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            # Only the key the loaders read the name from identifies the entry.
            if value.strip() == old_name:
                entry[key] = new_name
                count += 1
            break
        path_value = entry.get("path")
        if isinstance(path_value, str):
            updated = self.rename_topic_path(path_value, old_name, new_name)
            if updated != path_value:
                entry["path"] = updated
                count += 1
        return count

    def rename_topic_path(self, path_value: str, old_name: str, new_name: str) -> str:
        stripped = path_value.strip()
        if stripped == old_name:
            return new_name
        if stripped.endswith(old_name):
            prefix = path_value[: -len(old_name)]
            if prefix.rstrip().endswith(("/", "\\")):
                return f"{prefix}{new_name}"
        return path_value

    def describe_operations(
        self,
        operations: list[tuple[TopicLocation, Path]],
        json_targets: list[tuple[str, Path, object, str, list[str]]],
        topic_names: list[str],
    ) -> list[str]:
        details: list[str] = []
        if topic_names:
            details.append("Topics: " + ", ".join(topic_names))
        if operations:
            details.append("")
            details.append("Folders to move:")
            details.extend(
                f"  {location.path} → {destination}"
                for location, destination in operations
            )
        if json_targets:
            details.append("")
            details.append("JSON sources to update:")
            details.extend(f"  {target[1].name}" for target in json_targets)
        return details

    def execute_move_operations(
        self, operations: list[tuple[TopicLocation, Path]]
    ) -> tuple[list[str], list[str]]:
        move_errors: list[str] = []
        merge_conflicts: list[str] = []
        for location, destination in operations:
            if not location.path.exists():
                move_errors.append(f"Missing source: {location.path}")
                continue
            if not location.path.is_dir():
                move_errors.append(f"Source is not a folder: {location.path}")
                continue

            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if not destination.is_dir():
                        move_errors.append(
                            f"Destination exists and is not a folder: {destination}"
                        )
                        continue
                    if location.path.is_symlink():
                        # Merging would empty the folder the link points at.
                        move_errors.append(
                            f"{location.path} is a symbolic link and {destination} "
                            "already exists; merge its contents by hand."
                        )
                        continue
                    # Same rules as a rename merge: nothing is overwritten and
                    # nothing is skipped, so the source folder always empties
                    # and the moved topic stops showing up twice in the tree.
                    report = merge_tree(
                        location.path,
                        destination,
                        merge_conflict_dirname(location.path.name),
                    )
                    merge_conflicts.extend(str(path) for path in report.conflicts)
                else:
                    shutil.move(str(location.path), str(destination))
            except Exception as exc:
                move_errors.append(f"{location.path}: {exc}")
        return move_errors, merge_conflicts

    def collect_manual_move_actions(self, topic: Topic, target_state: str) -> list[str]:
        actions: list[str] = []
        target_label = STATE_FOLDERS.get(target_state, target_state)
        for name, states in self.get_json_source_states(topic):
            if states == {target_state}:
                continue
            sections = self.format_section_list(states)
            actions.append(
                f"- {name}: move the corresponding path from {sections} to "
                f"'{target_label}' in the {name} app."
            )
        return actions

    def resolve_move_destination(
        self, location: TopicLocation, target_state: str
    ) -> Path | None:
        root = self._resolve_location_root(location)
        if root is None:
            return None
        return root / STATE_FOLDERS[target_state] / location.path.name

    def move_topic_in_sections(
        self, sections: object, topic_name: str, target_state: str
    ) -> tuple[bool, str | None]:
        if not isinstance(sections, list):
            return False, "Sections value is not a list."

        target_names = self.section_names_for_state(target_state)
        if not target_names:
            return False, f"Unable to determine section name for '{target_state}'."

        target_section = None
        for candidate in target_names:
            target_section = self.find_section_by_name(sections, candidate)
            if target_section:
                break

        # Checked before anything is taken out of the other sections.
        if target_section is not None and not isinstance(
            target_section.get("topics", []), list
        ):
            return False, f"'{target_state}' section topics value is not a list."

        if target_section is None:
            target_section = {"name": target_names[0], "topics": []}
            sections.append(target_section)
        topics = target_section.setdefault("topics", [])

        taken = self.take_topic_from_sections(sections, topic_name, keep=topics)
        return self.place_moved_entries(topics, taken, topic_name, target_state), None

    def move_topic_in_named_lists(
        self, data: dict[str, object], topic_name: str, target_state: str
    ) -> tuple[bool, str | None]:
        list_keys = [
            key
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, list)
        ]
        if not list_keys:
            return False, "Unable to locate a topics list in the JSON file."

        target_names = self.section_names_for_state(target_state)
        target_key = None
        for candidate in target_names:
            for key in list_keys:
                if key.strip().casefold() == candidate.casefold():
                    target_key = key
                    break
            if target_key:
                break

        if not target_key:
            target_key = target_names[0] if target_names else target_state
        # Checked before anything is taken out of the other lists.
        if not isinstance(data.get(target_key, []), list):
            return False, f"'{target_state}' list is not a list."
        target_entries = data.setdefault(target_key, [])

        taken: list[object] = []
        for key in list_keys:
            if data[key] is not target_entries:
                taken.extend(self.take_topic_entries(data[key], topic_name))
        return (
            self.place_moved_entries(target_entries, taken, topic_name, target_state),
            None,
        )

    def move_topic_in_json_data(
        self, data: object, topic_name: str, target_state: str
    ) -> tuple[bool, str | None]:
        if isinstance(data, dict):
            if "sections" in data:
                return self.move_topic_in_sections(
                    data.get("sections"), topic_name, target_state
                )
            if "topics" in data:
                return (
                    False,
                    "JSON format does not support sections; cannot move topic.",
                )
            return self.move_topic_in_named_lists(data, topic_name, target_state)
        if isinstance(data, list):
            return (
                False,
                "JSON format does not support sections; cannot move topic.",
            )
        return False, "Unsupported JSON format."

    def apply_json_moves(
        self,
        targets: list[tuple[str, Path, object, str, list[str]]],
        target_state: str,
    ) -> tuple[list[str], list[tuple[str, Path, object, str]]]:
        errors: list[str] = []
        written: list[tuple[str, Path, object, str]] = []
        for label, path, data, original, topic_names in targets:
            updated = False
            for topic_name in topic_names:
                topic_updated, error = self.move_topic_in_json_data(
                    data, topic_name, target_state
                )
                if error:
                    errors.append(f"{label}: {error}")
                    errors.extend(self.restore_json_targets(written))
                    return errors, []
                updated = updated or topic_updated
            if not updated:
                continue
            try:
                payload = self.serialize_json_target(data)
            except (TypeError, ValueError) as exc:
                errors.append(f"{label}: Unable to serialize {path.name}: {exc}")
                errors.extend(self.restore_json_targets(written))
                return errors, []
            error = self.write_json_snapshot(path, payload, original)
            if error:
                errors.append(f"{label}: {error}")
                errors.extend(self.restore_json_targets(written))
                return errors, []
            written.append((label, path, data, original))
        return errors, written
