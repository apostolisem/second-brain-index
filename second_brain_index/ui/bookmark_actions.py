from __future__ import annotations

import sqlite3

from PyQt6.QtWidgets import QDialog, QMessageBox

from ..constants import OTHER_STATE, is_hub_key, is_other_key, split_hub_key
from ..models import ManualLink
from ..utils import infer_link_type
from .dialogs.manual_link_dialog import ManualLinkDialog
from .dialogs.topic_picker_dialog import TopicPickerDialog


class BookmarkActionsMixin:
    """Adding, editing, moving, copying and deleting bookmarks for MainWindow."""

    def add_manual_link(self) -> None:
        if not self.current_topic_key:
            return
        dialog = ManualLinkDialog(
            self,
            "Add bookmark",
            name_suggestions=self.collect_link_name_suggestions(),
            accept_label="Add bookmark",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, url = dialog.values()
        link_type = infer_link_type(url)
        new_link = ManualLink(
            id=0,
            topic_name=self.current_topic_key,
            link_name=name,
            url=url,
            link_type=link_type,
            title=name,
        )
        saved_link = self.link_store.add_link(new_link)
        topic = self.topics_by_key[self.current_topic_key]
        topic.manual_links.append(saved_link)
        topic.manual_links.sort(key=lambda link: link.link_name.casefold())
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)

    def edit_manual_link(self, link: ManualLink) -> None:
        dialog = ManualLinkDialog(
            self,
            "Edit bookmark",
            link.link_name,
            link.url,
            name_suggestions=self.collect_link_name_suggestions(),
            accept_label="Save",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, url = dialog.values()
        updated_link = ManualLink(
            id=link.id,
            topic_name=link.topic_name,
            link_name=name,
            url=url,
            link_type=infer_link_type(url),
            title=name,
        )
        self.link_store.update_link(updated_link)

        topic = self.topics_by_key.get(updated_link.topic_name)
        if topic:
            topic.manual_links = [
                updated_link if item.id == updated_link.id else item
                for item in topic.manual_links
            ]
            topic.manual_links.sort(key=lambda item: item.link_name.casefold())
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)

    def delete_manual_link(self, link: ManualLink) -> None:
        confirmation = QMessageBox.question(
            self,
            "Delete bookmark",
            f"Delete bookmark '{link.link_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        self.link_store.delete_link(link.id)
        topic = self.topics_by_key.get(link.topic_name)
        if topic:
            topic.manual_links = [item for item in topic.manual_links if item.id != link.id]
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)

    def delete_manual_links(self, links: list[ManualLink]) -> None:
        if not links:
            return
        if len(links) == 1:
            self.delete_manual_link(links[0])
            return
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle("Delete bookmarks")
        dialog.setText(f"Delete {len(links)} bookmarks?")
        dialog.setDetailedText("\n".join(link.link_name for link in links))
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if dialog.exec() != QMessageBox.StandardButton.Yes:
            return

        for link in links:
            self.link_store.delete_link(link.id)
            topic = self.topics_by_key.get(link.topic_name)
            if topic:
                topic.manual_links = [
                    item for item in topic.manual_links if item.id != link.id
                ]
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)

    def collect_topic_picker_entries(
        self, exclude_keys: set[str]
    ) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for key, topic in self.topics_by_key.items():
            if key in exclude_keys:
                continue
            if is_hub_key(key):
                label = f"{topic.name} — in {split_hub_key(key)[0]}"
            elif is_other_key(key):
                label = f"{topic.name} — Others"
            else:
                label = topic.name
            entries.append((key, label))
        entries.sort(key=lambda entry: (entry[1].casefold(), entry[1]))
        return entries

    def _pick_bookmark_target(
        self, links: list[ManualLink], verb: str
    ) -> str | None:
        """Ask for the topic to move or copy ``links`` to; None when cancelled."""
        source_keys = {link.topic_name for link in links}
        entries = self.collect_topic_picker_entries(source_keys)
        if not entries:
            QMessageBox.information(
                self,
                f"{verb} bookmark",
                f"There is no other topic to {verb.lower()} to.",
            )
            return None
        noun = "bookmark" if len(links) == 1 else "bookmarks"
        title = f"{verb} {len(links)} {noun} to topic"
        states = {
            key: OTHER_STATE if is_other_key(key) else self.topics_by_key[key].display_state
            for key, _label in entries
        }
        dialog = TopicPickerDialog(
            self, title, entries, states=states, accept_label=verb
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_key() or None

    def move_manual_links(self, links: list[ManualLink]) -> None:
        if not links:
            return
        target_key = self._pick_bookmark_target(links, "Move")
        if not target_key:
            return
        self._apply_bookmark_move(links, target_key)
        target_topic = self.topics_by_key.get(target_key)
        target_label = target_topic.name if target_topic else target_key
        self.statusBar().showMessage(
            f"Moved {len(links)} bookmark(s) to '{target_label}'", 4000
        )

    def copy_manual_links(self, links: list[ManualLink]) -> None:
        if not links:
            return
        target_key = self._pick_bookmark_target(links, "Copy")
        if not target_key:
            return
        try:
            copies = self._apply_bookmark_copy(links, target_key)
        except sqlite3.Error as exc:
            # The store rolled back: nothing was copied.
            QMessageBox.warning(
                self, "Copy bookmark", f"The bookmarks could not be copied.\n\n{exc}"
            )
            return
        target_topic = self.topics_by_key.get(target_key)
        target_label = target_topic.name if target_topic else target_key
        message = f"Copied {len(copies)} bookmark(s) to '{target_label}'"
        skipped = len(links) - len(copies)
        if skipped:
            message += f" ({skipped} no longer existed)"
        self.statusBar().showMessage(message, 4000)

    def _apply_bookmark_copy(
        self, links: list[ManualLink], target_key: str
    ) -> list[ManualLink]:
        """Copy ``links`` onto ``target_key``; the originals stay where they are."""
        copies = self.link_store.copy_links_to_topic(
            [link.id for link in links], target_key
        )
        target_topic = self.topics_by_key.get(target_key)
        if target_topic and copies:
            target_topic.manual_links.extend(copies)
            target_topic.manual_links.sort(
                key=lambda item: item.link_name.casefold()
            )
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)
        return copies

    def _apply_bookmark_move(
        self, links: list[ManualLink], target_key: str
    ) -> None:
        target_topic = self.topics_by_key.get(target_key)
        for link in links:
            self.link_store.reassign_link_topic(link.id, target_key)
            source_topic = self.topics_by_key.get(link.topic_name)
            if source_topic:
                source_topic.manual_links = [
                    item for item in source_topic.manual_links if item.id != link.id
                ]
            if target_topic:
                target_topic.manual_links.append(
                    ManualLink(
                        id=link.id,
                        topic_name=target_key,
                        link_name=link.link_name,
                        url=link.url,
                        link_type=link.link_type,
                        title=link.title,
                    )
                )
        if target_topic:
            target_topic.manual_links.sort(
                key=lambda item: item.link_name.casefold()
            )
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)

    def collect_link_name_suggestions(self) -> list[str]:
        names = {
            link.link_name
            for topic in self.topics_by_key.values()
            for link in topic.manual_links
            if link.link_name.strip()
        }
        return sorted(names, key=str.casefold)
