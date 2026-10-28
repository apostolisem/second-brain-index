from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QCheckBox, QDialog, QMessageBox

from ..config import HubConfig
from ..constants import STATE_FOLDERS, is_other_key, make_hub_key
from ..indexer import SOURCE_SPECS, SourceSpec
from ..models import ManualLink, ManualLocation, Topic
from ..utils import infer_link_type
from .dialogs.source_entry_dialog import SourceEntryDialog
from .creation import CreationOption, CreationPlan
from .dialogs.direct_link_dialog import DirectLinkDialog
from .dialogs.location_dialog import LocationDialog
from .widgets.locations_list import LocationRow


class LocationActionsMixin:
    """A topic's places for MainWindow: its own links and folders, direct links,
    and its entries in the topic lists."""

    def set_source_direct_link(self, row: LocationRow) -> None:
        topic = self.topics_by_key.get(self.current_topic_key or "")
        spec = next((s for s in SOURCE_SPECS if s.attr == row.source_attr), None)
        if topic is None or spec is None:
            return
        dialog = DirectLinkDialog(self, spec.name, row.direct_url)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._save_source_direct_link(self.current_topic_key, topic, spec, dialog.value())
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)

    def _save_source_direct_link(
        self, topic_key: str, topic: Topic | None, spec: SourceSpec, url: str
    ) -> None:
        """Point a source's location at ``url``: update its bookmark or add one.

        ``topic`` is None for an entry the next refresh will bring in.
        """
        if topic is not None:
            links = topic.manual_links
        else:
            links = [
                link
                for link in self.link_store.list_links()
                if link.topic_name == topic_key
            ]
        existing = next(
            (
                link
                for link in links
                if link.link_name.strip().casefold() == spec.attr
            ),
            None,
        )
        if existing is not None:
            saved = ManualLink(
                id=existing.id,
                topic_name=existing.topic_name,
                link_name=existing.link_name,
                url=url,
                link_type=infer_link_type(url),
                title=existing.title,
            )
            self.link_store.update_link(saved)
        else:
            saved = self.link_store.add_link(
                ManualLink(
                    id=0,
                    topic_name=topic_key,
                    link_name=spec.name,
                    url=url,
                    link_type=infer_link_type(url),
                    title=spec.name,
                )
            )
        if topic is not None:
            topic.manual_links = [
                item for item in topic.manual_links if item.id != saved.id
            ] + [saved]
            topic.manual_links.sort(key=lambda link: link.link_name.casefold())

    def clear_source_direct_link(self, row: LocationRow) -> None:
        topic = self.topics_by_key.get(self.current_topic_key or "")
        if topic is None or row.source_attr is None:
            return
        existing = self.find_source_override(topic, row.source_attr)
        if existing is None:
            return
        self.link_store.delete_link(existing.id)
        topic.manual_links = [
            item for item in topic.manual_links if item.id != existing.id
        ]
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)

    def add_manual_location(self) -> None:
        if not self.current_topic_key:
            return
        topic = self.topics_by_key.get(self.current_topic_key)
        if not topic:
            return
        dialog = LocationDialog(self, "Add location")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target, label = dialog.values()
        location = ManualLocation(
            id=0,
            topic_name=self.current_topic_key,
            label=label,
            target=target,
            kind=dialog.kind(),
        )
        saved = self.link_store.add_location(location)
        topic.manual_locations.append(saved)
        self.apply_filter()
        if saved.kind == "file":
            self.start_filename_indexing()
        self.show_topic_details(self.current_topic_key)

    def edit_manual_location(self, location: ManualLocation) -> None:
        dialog = LocationDialog(
            self,
            "Edit location",
            target=location.target,
            label=location.label,
            accept_label="Save",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target, label = dialog.values()
        updated = ManualLocation(
            id=location.id,
            topic_name=location.topic_name,
            label=label,
            target=target,
            kind=dialog.kind(),
        )
        self.link_store.update_location(updated)
        topic = self.topics_by_key.get(updated.topic_name)
        if topic:
            topic.manual_locations = [
                updated if item.id == updated.id else item
                for item in topic.manual_locations
            ]
        self.apply_filter()
        if "file" in (location.kind, updated.kind):
            self.start_filename_indexing()
        self.show_topic_details(self.current_topic_key)

    def delete_manual_location(self, location: ManualLocation) -> None:
        topic = self.topics_by_key.get(location.topic_name)
        # The topic's only place: without it there is nothing left to show.
        last_place = (
            topic is not None
            and topic.standalone
            and all(item.id == location.id for item in topic.manual_locations)
        )
        text = (
            f"Remove '{location.display_label}' from this topic?\n\n"
            "The folder or page itself is not touched."
        )
        extras = self.describe_stored_extras(topic) if last_place else None
        if last_place:
            text += (
                f"\n\nIt is the last place of '{topic.name}', so the topic"
                " disappears too."
            )
        confirmed, forget = self.confirm_removal("Remove location", text, extras)
        if not confirmed:
            return
        self.link_store.delete_location(location.id)
        if last_place:
            self.link_store.remove_standalone_topic(location.topic_name)
            if forget:
                self.link_store.forget_topic(location.topic_name)
            self.refresh_index()
            return
        if topic:
            topic.manual_locations = [
                item for item in topic.manual_locations if item.id != location.id
            ]
        self.apply_filter()
        if location.kind == "file":
            self.start_filename_indexing()
        self.show_topic_details(self.current_topic_key)

    def _populate_locations_add_menu(self, menu: QMenu) -> None:
        """Offer the folders and managed lists the current topic does not have yet."""
        topic = self.topics_by_key.get(self.current_topic_key or "")
        if topic is None:
            return
        if self.populate_folder_menu(menu, topic):
            separator = menu.addSeparator()
            if not self.populate_json_source_menu(menu, topic):
                menu.removeAction(separator)
            return
        self.populate_json_source_menu(menu, topic)

    def create_missing_topic_folder(self, topic: Topic, option: CreationOption) -> None:
        """Create one folder for an existing topic, as the New topic wizard would."""
        plan = CreationPlan(
            topic.name,
            topic.display_state,
            topic.hub_key,
            self.hub_for_topic(topic),
            folders=[option],
        )
        report = self.execute_creation_plan(plan)
        if report.failed:
            QMessageBox.warning(self, "Add location", "\n".join(report.failed))
            return
        self.refresh_index()
        self.statusBar().showMessage(f"Created {option.path}", 4000)

    def add_topic_to_json_source(self, source: str) -> None:
        if not self.current_topic_key:
            return
        # An Others folder has no PARA section to be listed under.
        if is_other_key(self.current_topic_key):
            return
        topic = self.topics_by_key.get(self.current_topic_key)
        if not topic:
            return

        spec = next((s for s in SOURCE_SPECS if s.name == source), None)
        if spec is None:
            return
        hub = self.hub_for_topic(topic)
        path = spec.topics_path(hub)
        existing_paths = spec.paths_of(topic)

        if existing_paths:
            QMessageBox.information(
                self,
                source,
                f"'{topic.name}' already exists in {path.name}.",
            )
            return

        dialog = SourceEntryDialog(
            self, source, path, topic_name=topic.name, state=topic.display_state
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        _name, _state, url = dialog.values()

        data, _original, error = self.load_or_create_json_target(path)
        if error:
            self._warn_json_source(source, error, path)
            return
        if data is None:
            self._warn_json_source(source, f"{path.name} is empty or invalid.", path)
            return

        if self.topic_exists_in_json_data(data, topic.name):
            QMessageBox.information(
                self,
                source,
                f"'{topic.name}' already exists in {path.name}.",
            )
            self.refresh_index()
            return

        added, add_error = self.add_topic_to_json_data(
            data, topic.name, topic.display_state
        )
        if add_error:
            QMessageBox.warning(self, source, add_error)
            return
        if not added:
            return

        write_error = self.write_json_target(path, data)
        if write_error:
            QMessageBox.warning(self, source, write_error)
            return
        if url:
            self._save_source_direct_link(self.current_topic_key, topic, spec, url)

        self.refresh_index()

    def remove_topic_from_json_source(self, row: LocationRow) -> None:
        topic = self.topics_by_key.get(self.current_topic_key or "")
        spec = next((s for s in SOURCE_SPECS if s.attr == row.source_attr), None)
        if topic is None or spec is None:
            return
        path = spec.topics_path(self.hub_for_topic(topic))
        override = self.find_source_override(topic, spec.attr)
        lines = [
            f"Remove '{topic.name}' from {path.name}?",
            "",
            f"The folder in {spec.name} itself is not touched.",
        ]
        if override is not None:
            lines.append("Its direct link is removed too.")
        topic_key = self.current_topic_key or ""
        places_left = bool(topic.locations) or any(
            other.paths_of(topic) for other in SOURCE_SPECS if other is not spec
        )
        # Its own links and folders cannot hold a topic up by themselves; it
        # has to become a links-only topic, or it vanishes along with them.
        keeps_own_places = not places_left and bool(topic.manual_locations)
        fallback_state = (
            topic.display_state if topic.display_state in STATE_FOLDERS else "Projects"
        )
        extras = None
        if keeps_own_places:
            lines.append(
                f"It keeps its own links and folders and stays in {fallback_state}."
            )
        elif not places_left:
            lines.append("This topic exists only in this list and will disappear.")
            extras = self.describe_stored_extras(
                topic, skip_link_ids={override.id} if override is not None else set()
            )
        confirmed, forget = self.confirm_removal(
            f"Remove from {spec.name}", "\n".join(lines), extras
        )
        if not confirmed:
            return

        data, _original, error = self.load_json_target(path)
        if error or data is None:
            self._warn_json_source(
                spec.name, error or f"{path.name} is empty or invalid.", path
            )
            return
        if self.remove_topic_from_json_data(data, topic.name):
            write_error = self.write_json_target(path, data)
            if write_error:
                QMessageBox.warning(self, spec.name, write_error)
                return
        if override is not None:
            self.link_store.delete_link(override.id)
        if keeps_own_places:
            self.link_store.set_standalone_topic(topic_key, fallback_state)
        if forget:
            self.link_store.forget_topic(topic_key)
        self.refresh_index()

    def describe_stored_extras(
        self, topic: Topic, skip_link_ids: set[int] = frozenset()
    ) -> str | None:
        """What stays stored when a topic disappears, e.g. "its 2 bookmarks and pin"."""
        links = sum(1 for link in topic.manual_links if link.id not in skip_link_ids)
        parts = []
        if links:
            parts.append(f"{links} bookmark{'s' if links != 1 else ''}")
        if topic.tags:
            parts.append(f"{len(topic.tags)} tag{'s' if len(topic.tags) != 1 else ''}")
        if topic.pinned_rank is not None:
            parts.append("pin")
        if not parts:
            return None
        if len(parts) > 1:
            parts = [", ".join(parts[:-1]), parts[-1]]
        return "its " + " and ".join(parts)

    def confirm_removal(
        self, title: str, text: str, extras: str | None
    ) -> tuple[bool, bool]:
        """Ask Yes/No; with ``extras`` left behind, offer to delete them too.

        Returns whether the user confirmed and whether the extras go as well.
        """
        if extras is None:
            answer = QMessageBox.question(
                self,
                title,
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            return answer == QMessageBox.StandardButton.Yes, False
        text += (
            f"\n\n{extras[0].upper()}{extras[1:]} stay stored and come back if you"
            " create the topic again."
        )
        return self.confirm_with_option(title, text, f"Also delete {extras}")

    def confirm_with_option(
        self, title: str, text: str, option: str
    ) -> tuple[bool, bool]:
        """Yes/No with a checkbox under the text; (confirmed, ticked)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        checkbox = QCheckBox(option)
        box.setCheckBox(checkbox)
        confirmed = box.exec() == QMessageBox.StandardButton.Yes
        return confirmed, confirmed and checkbox.isChecked()

    def create_source_entry(
        self, spec: SourceSpec, hub: HubConfig, hub_key: str | None
    ) -> None:
        """Add an entry of its own to a list: a topic that needs no folder."""
        path = spec.topics_path(hub)
        dialog = SourceEntryDialog(self, spec.name, path)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, state, url = dialog.values()

        data, _original, error = self.load_or_create_json_target(path)
        if error or data is None:
            self._warn_json_source(
                spec.name, error or f"{path.name} is empty or invalid.", path
            )
            return
        if self.topic_exists_in_json_data(data, name):
            QMessageBox.warning(
                self, spec.name, f"'{name}' already exists in {path.name}."
            )
            return
        added, add_error = self.add_topic_to_json_data(data, name, state)
        if add_error or not added:
            QMessageBox.warning(
                self, spec.name, add_error or f"Unable to add '{name}'."
            )
            return
        write_error = self.write_json_target(path, data)
        if write_error:
            QMessageBox.warning(self, spec.name, write_error)
            return

        key = make_hub_key(hub_key, name) if hub_key else name
        if url:
            self._save_source_direct_link(key, self.topics_by_key.get(key), spec, url)
        self.refresh_index()
        item = self.tree_items_by_key.get(key)
        if item is not None:
            self.tree.clearSelection()
            self.tree.setCurrentItem(item)
            item.setSelected(True)

    def _warn_json_source(self, source: str, message: str, path: Path) -> None:
        """Report a list that cannot be used, with a shortcut to the file."""
        box = QMessageBox(QMessageBox.Icon.Warning, source, message, parent=self)
        box.addButton(QMessageBox.StandardButton.Ok)
        open_button = None
        if path.is_file():
            open_button = box.addButton("Open file", QMessageBox.ButtonRole.ActionRole)
        box.exec()
        if open_button is not None and box.clickedButton() is open_button:
            self.open_path(str(path))
