from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtWidgets import QDialog, QMessageBox

from ..config import HubConfig
from ..constants import (
    OTHER_STATE,
    display_name_from_key,
    is_hub_key,
    is_other_key,
    split_hub_key,
)
from .dialogs.topic_picker_dialog import TopicPickerDialog
from .dialogs.orphan_dialog import OrphanDialog


@dataclass(frozen=True)
class OrphanEntry:
    """What is stored under a topic key that no topic answers to any more."""

    key: str
    label: str
    stored: dict[str, object]
    # A missing root, sub-hub or unreadable list may bring the topic back.
    may_come_back: bool
    # Why it cannot be brought back as a links-only topic; None when it can.
    restore_blocked: str | None

    def summary(self) -> str:
        parts = []
        for field, singular, plural in (
            ("links", "bookmark", "bookmarks"),
            ("locations", "link or folder", "links or folders"),
            ("tags", "tag", "tags"),
        ):
            count = int(self.stored[field])
            if count:
                parts.append(f"{count} {singular if count == 1 else plural}")
        if self.stored["pinned"]:
            parts.append("pinned")
        if self.stored["section"]:
            parts.append(f"links-only in {self.stored['section']}")
        return " · ".join(parts)


class OrphanCleanupMixin:
    """Finding and resolving orphaned topic data for MainWindow."""

    def find_orphans(self) -> list[OrphanEntry]:
        entries = []
        for key, stored in self.link_store.stored_topic_keys().items():
            if key in self.topics_by_key:
                continue
            entries.append(
                OrphanEntry(
                    key,
                    self.orphan_label(key),
                    stored,
                    self.orphan_may_come_back(key),
                    self.orphan_restore_blocker(key, stored),
                )
            )
        entries.sort(key=lambda entry: (entry.label.casefold(), entry.label))
        return entries

    def orphan_label(self, key: str) -> str:
        name = display_name_from_key(key)
        if is_hub_key(key):
            return f"{name} — in {split_hub_key(key)[0]}"
        if is_other_key(key):
            return f"{name} — Others"
        return name

    def orphan_may_come_back(self, key: str) -> bool:
        if self.scan_errors:
            # A list that could not be read hides every topic it holds.
            return True
        if is_hub_key(key):
            hub = self.hubs_by_project_key.get(split_hub_key(key)[0])
            if hub is None:
                # The sub-hub is gone for now, or for good: no telling which.
                return True
            return self._hub_root_missing(hub)
        if is_other_key(key):
            return any(root in self.missing_roots for root in self.config.other_folders)
        return self._hub_root_missing(self.config.root_hub)

    def _hub_root_missing(self, hub: HubConfig) -> bool:
        roots: list[Path] = list(hub.para_roots)
        if hub.obsidian_root is not None:
            roots.append(hub.obsidian_root)
        return any(root in self.missing_roots for root in roots)

    def orphan_restore_blocker(self, key: str, stored: dict[str, object]) -> str | None:
        if is_other_key(key):
            return "An Others topic is its folder; it cannot be links-only."
        if is_hub_key(key) and split_hub_key(key)[0] not in self.hubs_by_project_key:
            return "Its Sub-Hub is not available."
        if not stored["locations"]:
            # A links-only topic is shown by its own links and folders.
            return "It has no links or folders of its own; create the topic again instead."
        existing = self.find_colliding_topic_key(key, "")
        if existing is not None:
            return (
                f"'{display_name_from_key(existing)}' already exists;"
                " reattach to it instead."
            )
        return None

    def show_orphan_dialog(self) -> None:
        OrphanDialog(self, self).exec()

    def reattach_orphans(self, keys: list[str]) -> bool:
        """Hand what the keys carry to a topic the user picks; True when done."""
        entries = self.collect_topic_picker_entries(set())
        if not entries:
            QMessageBox.information(self, "Reattach", "There is no topic to reattach to.")
            return False
        states = {
            key: OTHER_STATE if is_other_key(key) else self.topics_by_key[key].display_state
            for key, _label in entries
        }
        dialog = TopicPickerDialog(
            self, "Reattach to topic", entries, states=states, accept_label="Reattach"
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        target_key = dialog.selected_key()
        if not target_key:
            return False
        with self.link_store.transaction():
            for key in keys:
                link_ids = self.link_store.link_ids_for_topic(key)
                location_ids = self.link_store.location_ids_for_topic(key)
                # The section of a topic that is gone must not become the target's.
                self.link_store.remove_standalone_topic(key)
                self.link_store.reassign_topic_key(key, target_key)
                self.link_store.dedupe_manual_links(target_key, link_ids)
                self.link_store.dedupe_manual_locations(target_key, location_ids)
        self.refresh_index()
        name = self.topics_by_key[target_key].name if target_key in self.topics_by_key else target_key
        self.statusBar().showMessage(f"Reattached to '{name}'", 4000)
        return True

    def restore_orphans(self, keys: list[str], state: str) -> None:
        """Bring the keys back as links-only topics in ``state``."""
        with self.link_store.transaction():
            for key in keys:
                self.link_store.set_standalone_topic(key, state)
        self.refresh_index()
        self.statusBar().showMessage(f"Restored {len(keys)} topic(s) in {state}", 4000)

    def delete_orphans(self, keys: list[str]) -> bool:
        noun = "topic" if len(keys) == 1 else "topics"
        answer = QMessageBox.question(
            self,
            "Delete orphaned data",
            f"Delete everything stored for {len(keys)} {noun}: bookmarks, links"
            " and folders, tags and pins? Folders and pages themselves are not"
            " touched.\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        with self.link_store.transaction():
            for key in keys:
                self.link_store.forget_topic(key)
        self.refresh_index()
        self.statusBar().showMessage(f"Deleted stored data of {len(keys)} {noun}", 4000)
        return True
