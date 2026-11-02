from __future__ import annotations

import json
import threading
import time
from functools import partial
from pathlib import Path

from PyQt6.QtCore import QEventLoop, QSettings, Qt, QThread, QUrl, QUrlQuery
from PyQt6.QtGui import (
    QDesktopServices,
    QFont,
    QGuiApplication,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QDialog,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QToolTip,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, HubConfig, settings_group
from ..constants import OTHER_STATE, is_other_key
from ..db import ManualLinkStore
from ..filename_index import FileEntry, FilenameIndexWorker, TopicFileIndex
from ..indexer import SOURCE_SPECS
from ..location_filter import (
    ALL_LOCATIONS,
    LocationFilter,
    LocationOptions,
    build_location_options,
)
from ..models import ManualLink, Topic, TopicLocation
from ..resources import app_icon
from ..search import SearchToken
from .dialogs.new_topic_dialog import NewTopicDialog
from .dialogs.tag_dialog import TopicTagDialog
from .operations import TopicOperationsMixin
from .creation import TopicCreationMixin
from .orphans import OrphanCleanupMixin
from .widgets.bookmarks_panel import BookmarksPanel
from .widgets.bulk_panel import BulkPanel
from .widgets.common import current_theme
from .widgets.header_bar import HeaderBarMixin
from .widgets.locations_list import LocationRow, LocationsList
from .widgets.matching_files_panel import MatchingFilesPanel
from .widgets.status_bar import StatusBarMixin
from .widgets.topic_header import TopicHeaderMixin
from .widgets.topic_tree import TopicTreeMixin
from .refresh import RefreshMixin
from .filename_search import FilenameSearchMixin
from .bookmark_actions import BookmarkActionsMixin
from .location_actions import LocationActionsMixin
from .topic_moves import TopicMovesMixin


# A cold-started Obsidian (a Flatpak especially) takes seconds to come up.
OBSIDIAN_URI_COOLDOWN_S = 3.0


class MainWindow(
    HeaderBarMixin,
    TopicTreeMixin,
    TopicHeaderMixin,
    StatusBarMixin,
    TopicOperationsMixin,
    TopicCreationMixin,
    OrphanCleanupMixin,
    RefreshMixin,
    FilenameSearchMixin,
    BookmarkActionsMixin,
    LocationActionsMixin,
    TopicMovesMixin,
    QMainWindow,
):
    def __init__(self, config: AppConfig, link_store: ManualLinkStore) -> None:
        super().__init__()
        self.config = config
        self.link_store = link_store
        self._settings_group = settings_group(config.env_path)
        self.topics_by_key: dict[str, Topic] = {}
        self.topic_keys_by_normalized: dict[str, str] = {}
        self.hubs_by_project_key: dict[str, HubConfig] = {}
        self._last_obsidian_uri_at = float("-inf")
        self._subtopics_by_hub: dict[str, dict[str, list[tuple[str, Topic]]]] = {}
        self.tree_items_by_key: dict[str, QTreeWidgetItem] = {}
        self.current_topic_key: str | None = None
        self.filename_index_by_key: dict[str, str] = {}
        self.filename_entries_by_key: dict[str, tuple[FileEntry, ...]] = {}
        # Each topic's index with the folders it was built from, so a refresh
        # that leaves those alone need not walk them again.
        self._filename_indexes: dict[str, tuple[tuple[Path, ...], TopicFileIndex]] = {}
        self._indexing_paths: dict[str, list[Path]] = {}
        self._active_tokens: list[SearchToken] = []
        # Session-only: every launch starts at All locations.
        self.location_filter: LocationFilter = ALL_LOCATIONS
        # Filename blobs limited to the active location, built on demand.
        self._filtered_filename_blobs: dict[str, str] = {}
        # Roots the last scan found missing; the Location menu marks them.
        self.missing_roots: frozenset[Path] = frozenset()
        # What the last refresh could not read; see orphan_may_come_back.
        self.scan_errors: list[tuple[str, str]] = []
        self.match_fields_by_key: dict[str, set[str]] = {}
        self._filename_index_generation = 0
        self._filename_index_worker: FilenameIndexWorker | None = None
        self._filename_index_threads: list[QThread] = []
        self._refresh_in_progress = False
        self._refresh_pending = False
        self._pending_reindex = False
        self._closed = False
        self._active_scan: tuple[threading.Event, QEventLoop] | None = None
        self._section_expanded: dict[str, bool] = {}
        self._rebuilding_tree = False
        self._group_others_by_parent = False
        self.focused_hub_key: str | None = None
        self._pre_focus_topic_key: str | None = None
        # Topic the details area last showed; a change resets the bookmarks view.
        self._details_topic_key: str | None = None

        self.setWindowTitle("Second Brain Hub")
        self.setWindowIcon(app_icon())
        self._build_ui()
        restored_geometry = self._restore_settings()
        self.refresh_index()
        if not restored_geometry:
            self.center_on_screen()

    def _build_ui(self) -> None:
        theme = current_theme()
        central = QWidget()
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.resize(1200, 640)
        self.setMinimumSize(960, 640)
        self._configure_tooltips()

        header = self._build_header_bar(theme)
        outer_layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter = splitter
        splitter.setHandleWidth(1)
        outer_layout.addWidget(splitter, 1)

        self._build_topic_tree()
        splitter.addWidget(self.sidebar)

        details_container = QWidget()
        details_layout = QVBoxLayout(details_container)
        details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        details_layout.setContentsMargins(36, 28, 36, 40)
        details_layout.setSpacing(0)

        # Single-topic view and the multi-selection view share the scroll area.
        self.topic_page = QWidget()
        topic_layout = QVBoxLayout(self.topic_page)
        topic_layout.setContentsMargins(0, 0, 0, 0)
        topic_layout.setSpacing(28)
        self._build_topic_header(theme, topic_layout)

        self.matching_files_panel = MatchingFilesPanel(on_open=self.open_path)
        self.locations_panel = LocationsList(
            on_open_folder=self.open_path,
            on_add_location=self.add_manual_location,
            on_edit_location=self.edit_manual_location,
            on_delete_location=self.delete_manual_location,
            populate_json_menu=self._populate_locations_add_menu,
            on_set_direct_link=self.set_source_direct_link,
            on_clear_direct_link=self.clear_source_direct_link,
            on_remove_from_source=self.remove_topic_from_json_source,
        )
        self.manual_links_panel = BookmarksPanel(
            on_add=self.add_manual_link,
            on_edit=self.edit_manual_link,
            on_delete=self.delete_manual_link,
            on_open=self.open_link,
            on_move=self.move_manual_links,
            on_copy=self.copy_manual_links,
            on_delete_many=self.delete_manual_links,
        )
        topic_layout.addWidget(self.matching_files_panel)
        topic_layout.addWidget(self.locations_panel)
        topic_layout.addWidget(self.manual_links_panel)
        topic_layout.addStretch()
        details_layout.addWidget(self.topic_page)

        self.bulk_panel = BulkPanel()
        self.bulk_panel.clear_requested.connect(self.clear_multi_selection)
        self.bulk_panel.move_requested.connect(
            lambda state: self.move_topics(self.get_selected_topics(), state)
        )
        self.bulk_panel.tag_requested.connect(self.add_tag_to_selected_topics)
        self.bulk_panel.setVisible(False)
        details_layout.addWidget(self.bulk_panel)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(details_container)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 900])

        self.setCentralWidget(central)

        self._build_status_bar(theme)

        self._setup_shortcuts()
        self._setup_file_watcher()
        self._apply_theme_icons()
        theme.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, _palette) -> None:
        if self._closed:
            return
        self._apply_theme_icons()
        # Tree and table icons are tinted when built, so rebuild them.
        self.apply_filter()

    def _setup_shortcuts(self) -> None:
        for sequence in ("Ctrl+F", "Ctrl+K"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(self.focus_search)
        new_shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
        new_shortcut.activated.connect(self.new_topic)
        for sequence in ("F5", "Ctrl+R"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(lambda: self.refresh_index(reindex_files=True))

        rename_shortcut = QShortcut(QKeySequence("F2"), self.tree)
        rename_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        rename_shortcut.activated.connect(self.rename_topic)

        down_shortcut = QShortcut(QKeySequence("Down"), self.search_input)
        down_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        down_shortcut.activated.connect(self.focus_first_result)
        self.search_input.returnPressed.connect(self.focus_first_result)

    def _configure_tooltips(self) -> None:
        # Colours and padding come from the theme stylesheet.
        base_font = self.font()
        tooltip_font = QFont(base_font)
        tooltip_font.setPointSize(max(base_font.pointSize() - 4, 7))
        QToolTip.setFont(tooltip_font)

    def closeEvent(self, event) -> None:
        # A closed window must not refresh: its store may already be gone.
        self._closed = True
        if self._fs_watcher is not None:
            self._watch_debounce.stop()
            self._fs_watcher.blockSignals(True)
        if self._active_scan is not None:
            self._cancel_scan(*self._active_scan)
        self._save_settings()
        self.cancel_filename_indexing()
        for thread in list(self._filename_index_threads):
            thread.wait(3000)
        super().closeEvent(event)

    def _save_settings(self) -> None:
        settings = self.settings()
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/splitter", self.splitter.saveState())
        settings.setValue(
            "search/include_filenames", self.filename_search_toggle.isChecked()
        )
        settings.setValue("session/current_topic", self.current_topic_key or "")
        settings.setValue(
            "view/others_group_by_parent", self._group_others_by_parent
        )
        self._save_expanded_sections()

    def settings(self) -> QSettings:
        """This configuration's settings. The theme alone is shared by all."""
        settings = QSettings()
        if self._settings_group:
            settings.beginGroup(self._settings_group)
        return settings

    def _saved_setting(self, settings: QSettings, key: str, default: object = None) -> object:
        """A saved value, falling back to where it was kept before it was per configuration."""
        value = settings.value(key)
        if value is None and self._settings_group:
            value = QSettings().value(key)
        return default if value is None else value

    def _restore_settings(self) -> bool:
        settings = self.settings()
        restored_geometry = False
        geometry = self._saved_setting(settings, "window/geometry")
        if geometry is not None:
            restored_geometry = bool(self.restoreGeometry(geometry))
        splitter_state = self._saved_setting(settings, "window/splitter")
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)
        include_filenames = self._saved_setting(settings, "search/include_filenames")
        if include_filenames is not None:
            self.filename_search_toggle.setChecked(
                include_filenames in (True, "true", "1", 1)
            )
        saved_topic = self._saved_setting(settings, "session/current_topic", "")
        if saved_topic:
            self.current_topic_key = str(saved_topic)
        group_others = self._saved_setting(settings, "view/others_group_by_parent")
        if group_others is not None:
            self._group_others_by_parent = group_others in (True, "true", "1", 1)
        raw_sections = self._saved_setting(settings, "session/expanded_sections", "")
        if raw_sections:
            try:
                data = json.loads(str(raw_sections))
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict):
                self._section_expanded = {
                    str(key): bool(value) for key, value in data.items()
                }
        return restored_geometry

    def sync_topic_tags(self, topic_keys) -> None:
        """Re-read tags for the given topics straight from the store.

        Tag edits only touch the database, so there is nothing to re-scan on
        disk. Going through refresh_index() here would block the UI thread on
        a full filesystem walk of every PARA and OTHER_FOLDERS root.
        """
        for topic_key in topic_keys:
            topic = self.topics_by_key.get(topic_key)
            if not topic:
                continue
            tags = self.link_store.list_tags_for_topic(topic_key)
            topic.tags = sorted(tags, key=lambda value: (value.casefold(), value))
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)

    def toggle_pin_topic(self, topic_key: str) -> None:
        topic = self.topics_by_key.get(topic_key)
        if not topic or is_other_key(topic_key):
            return
        if topic.pinned_rank is None:
            topic.pinned_rank = self.link_store.pin_topic(topic_key)
        else:
            self.link_store.unpin_topic(topic_key)
            topic.pinned_rank = None
        self.apply_filter()
        self.show_topic_details(self.current_topic_key)

    def toggle_pin_current_topic(self) -> None:
        if self.current_topic_key:
            self.toggle_pin_topic(self.current_topic_key)

    def location_options(self) -> LocationOptions:
        return build_location_options(
            self.config, self.hubs_by_project_key, self.topics_by_key.values()
        )

    def set_location_filter(self, location_filter: LocationFilter) -> None:
        self.location_filter = location_filter
        self._filtered_filename_blobs = {}
        self._sync_location_button()
        self.apply_filter()
        self.update_status_summary()

    def _revalidate_location_filter(self) -> str | None:
        """Fall back to All locations when the chosen one no longer exists.

        Returns the message to show when it did.
        """
        self._filtered_filename_blobs = {}
        if self.location_filter.is_all():
            return None
        if self.location_filter in self.location_options().all():
            return None
        label = self.location_filter.label
        self.location_filter = ALL_LOCATIONS
        self._sync_location_button()
        return f"Location '{label}' is no longer available; showing all locations"

    def add_tag_to_selected_topics(self) -> None:
        keys = self.selected_topic_keys()
        if not keys:
            return
        dialog = TopicTagDialog(
            self,
            f"Add tag to {len(keys)} topics" if len(keys) != 1 else "Add tag",
            tag_suggestions=self.collect_tag_name_suggestions(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        tag_name = dialog.value()
        added = 0
        try:
            for key in keys:
                if self.link_store.add_topic_tag(key, tag_name):
                    added += 1
        except ValueError as exc:
            QMessageBox.warning(self, "Add Tag", str(exc))
            return
        self.sync_topic_tags(keys)
        self.statusBar().showMessage(
            f"Added tag '{tag_name}' to {added} topic(s)", 4000
        )

    @property
    def bulk_keys(self) -> list[str]:
        """Selected topic keys while two or more are selected, else empty."""
        keys = self.selected_topic_keys()
        return keys if len(keys) >= 2 else []

    @property
    def bookmark_checked_ids(self) -> set[int]:
        return self.manual_links_panel.checked_ids

    def clear_multi_selection(self) -> None:
        """Back to one topic: keep the current one, drop the rest."""
        item = self.tree_items_by_key.get(self.current_topic_key or "")
        self.tree.clearSelection()
        if item is not None:
            self.tree.setCurrentItem(item)
            item.setSelected(True)
        self.show_topic_details(self.current_topic_key)

    def _show_bulk_panel(self, selected: list[tuple[str, Topic]]) -> None:
        items = [
            (topic.name, OTHER_STATE if is_other_key(key) else topic.display_state)
            for key, topic in selected
        ]
        other_count = sum(1 for key, _topic in selected if is_other_key(key))
        self.bulk_panel.set_selection(items, other_count)
        self.topic_page.setVisible(False)
        self.bulk_panel.setVisible(True)

    def show_topic_details(self, topic_key: str | None) -> None:
        selected = self.get_selected_topics()
        if len(selected) >= 2:
            self._show_bulk_panel(selected)
            return
        self.bulk_panel.setVisible(False)
        self.topic_page.setVisible(True)

        if topic_key != self._details_topic_key:
            # A different topic: its bookmarks start unchecked and unfiltered.
            self._details_topic_key = topic_key
            self.manual_links_panel.reset_view()

        header = self.topic_header
        if not topic_key or topic_key not in self.topics_by_key:
            header.clear()
            self.locations_panel.set_rows([])
            self.manual_links_panel.set_links([])
            self.manual_links_panel.set_enabled(False)
            self.manual_links_panel.setVisible(False)
            self._update_matching_files(None)
            return

        topic = self.topics_by_key[topic_key]
        header.set_kicker(*self.topic_kicker(topic_key, topic))
        header.set_title(topic.name)
        header.set_notice(self.topic_notice(topic))
        header.set_tags(topic.tags)
        header.set_pinned(topic.pinned_rank is not None)

        self.locations_panel.set_rows(self.location_rows(topic))
        self.manual_links_panel.setVisible(True)
        self.manual_links_panel.set_links(self.visible_bookmarks(topic))
        self.manual_links_panel.set_enabled(True)
        can_move, _can_archive, can_rename = self.topic_action_flags(topic_key, topic)
        self.move_button.setEnabled(can_move)
        self.rename_button.setEnabled(can_rename)
        self.pin_button.setEnabled(not is_other_key(topic_key))
        self.open_folder_button.setEnabled(self.topic_folder(topic) is not None)
        is_subhub_project = topic_key in self.hubs_by_project_key
        self.focus_button.setEnabled(is_subhub_project)
        self.topic_header.set_focus_available(is_subhub_project)
        if self.focused_hub_key == topic_key:
            self.focus_button.setText("Focus Active")
            self.focus_button.setEnabled(False)
        else:
            self.focus_button.setText("Focus")
        self._update_matching_files(topic_key)

    def topic_folder(self, topic: Topic) -> Path | None:
        """The folder "Open folder" opens: the first filesystem location."""
        for location in topic.locations:
            if location.source == "filesystem":
                return location.path
        return None

    def open_current_topic_folder(self) -> None:
        topic = self.topics_by_key.get(self.current_topic_key or "")
        folder = self.topic_folder(topic) if topic else None
        if folder is not None:
            self.open_path(str(folder))

    def location_rows(self, topic: Topic) -> list[LocationRow]:
        """Every place a topic lives, in display order: discovered sources first
        (filesystem, Obsidian, OneNote, Outlook, PLM), then manual entries."""
        rows: list[LocationRow] = []
        folder_sources = (
            ("filesystem", "Filesystem", self.open_path),
            ("obsidian", "Obsidian", self.open_obsidian_path),
        )
        for source, source_label, opener in folder_sources:
            locations = sorted(
                (loc for loc in topic.locations if loc.source == source),
                key=lambda loc: str(loc.path).casefold(),
            )
            for location in locations:
                label = self.display_location_path(location)
                if topic.has_inconsistency:
                    label = f"{label}  [{location.state}]"
                rows.append(
                    LocationRow(
                        source=source_label,
                        label=label,
                        tooltip=str(location.path),
                        open=partial(opener, str(location.path)),
                        path=location.path,
                    )
                )
        for spec in SOURCE_SPECS:
            paths = spec.paths_of(topic)
            if not paths:
                continue
            opener = getattr(self, f"open_{spec.attr}_link")
            override_url = self.find_manual_link_url(topic, spec.attr) or ""
            for path in paths:
                rows.append(
                    LocationRow(
                        source=spec.name,
                        label=self.display_source_path(topic, path),
                        tooltip=override_url
                        or f"Opens the {spec.name} home page."
                        " Set a direct link to open this folder.",
                        open=partial(opener, override_url),
                        source_attr=spec.attr,
                        direct_url=override_url,
                    )
                )
        for location in sorted(
            topic.manual_locations, key=lambda loc: loc.display_label.casefold()
        ):
            if location.kind == "file":
                opener = partial(self.open_path, location.target)
                path = Path(location.target).expanduser()
            else:
                opener = partial(self.open_link, location.target)
                path = None
            rows.append(
                LocationRow(
                    source=location.source_label,
                    label=location.display_label,
                    tooltip=location.target,
                    open=opener,
                    path=path,
                    manual=location,
                )
            )
        return rows

    def visible_bookmarks(self, topic: Topic) -> list[ManualLink]:
        """The topic's bookmarks minus those shown as a location's direct link."""
        hidden = set()
        for spec in SOURCE_SPECS:
            if not spec.paths_of(topic):
                continue
            override = self.find_source_override(topic, spec.attr)
            if override is not None:
                hidden.add(override.id)
        return [link for link in topic.manual_links if link.id not in hidden]

    def display_location_path(self, location: TopicLocation) -> str:
        """Shorten a folder path to its root's name plus the part below it."""
        path = location.path
        root = self._resolve_location_root(location)
        if root is not None:
            try:
                relative = path.relative_to(root)
            except ValueError:
                relative = None
            if relative is not None and root.name:
                return " / ".join([root.name, *relative.parts])
        if path.parent.name:
            return f"{path.parent.name} / {path.name}"
        return str(path)

    def focus_current_subhub(self) -> None:
        if self.current_topic_key:
            self.open_subhub_focus(self.current_topic_key)

    def open_subhub_focus(self, topic_key: str) -> None:
        if topic_key not in self.hubs_by_project_key:
            return
        if self.focused_hub_key == topic_key:
            return
        self._pre_focus_topic_key = self.current_topic_key
        self.focused_hub_key = topic_key
        self.current_topic_key = topic_key
        self.apply_filter()
        self.update_status_summary()
        topic = self.topics_by_key.get(topic_key)
        label = topic.name if topic else topic_key
        self.statusBar().showMessage(f"Focused on Sub-Hub '{label}'", 3000)

    def exit_subhub_focus(self) -> None:
        if not self.focused_hub_key:
            return
        preferred_key = self.current_topic_key or self._pre_focus_topic_key
        self.focused_hub_key = None
        if preferred_key and preferred_key in self.topics_by_key:
            self.current_topic_key = preferred_key
        elif self._pre_focus_topic_key and self._pre_focus_topic_key in self.topics_by_key:
            self.current_topic_key = self._pre_focus_topic_key
        self.apply_filter()
        self.update_status_summary()
        self.statusBar().showMessage("Exited Sub-Hub Focus", 3000)

    def filter_by_tag(self, tag_name: str) -> None:
        self.search_input.setText(f'tag:"{tag_name}"')

    def new_topic(self, hub_key: str | None = None, state: str | None = None) -> None:
        """The wizard, at the section given or else where the user is now.

        A section from the tree comes with its hub, None for the main hub, so
        the selection elsewhere does not pull the topic into a Sub-Hub.
        """
        if state is None:
            hub_key = self._active_subhub_key()
        dialog = NewTopicDialog(self, self, hub_key, state)
        dialog.exec()
        # Closing the result page any way at all still leaves the topic made.
        report = dialog.report
        if report is None or not report.done:
            return
        plan = dialog.plan()
        self.refresh_index()
        item = self.tree_items_by_key.get(plan.key)
        if item is not None:
            self.tree.clearSelection()
            self.tree.setCurrentItem(item)
            item.setSelected(True)
        self.statusBar().showMessage(f"Created '{plan.name}'", 4000)

    def add_topic_tag(self) -> None:
        if not self.current_topic_key:
            return
        topic = self.topics_by_key.get(self.current_topic_key)
        dialog = TopicTagDialog(
            self,
            f"Add tag to {topic.name}" if topic else "Add tag",
            tag_suggestions=self.collect_tag_name_suggestions(),
            exclude=topic.tags if topic else None,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        tag_name = dialog.value()
        try:
            added = self.link_store.add_topic_tag(self.current_topic_key, tag_name)
        except ValueError as exc:
            QMessageBox.warning(self, "Add Tag", str(exc))
            return

        if not added:
            QMessageBox.information(
                self,
                "Add Tag",
                f"'{tag_name}' is already attached to this topic.",
            )
            return

        self.sync_topic_tags([self.current_topic_key])

    def edit_topic_tag(self, tag_name: str) -> None:
        if not self.current_topic_key:
            return
        dialog = TopicTagDialog(
            self,
            "Rename tag",
            tag_name=tag_name,
            tag_suggestions=self.collect_tag_name_suggestions(),
            accept_label="Save",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        new_tag_name = dialog.value()
        try:
            changed = self.link_store.replace_topic_tag(
                self.current_topic_key, tag_name, new_tag_name
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Edit Tag", str(exc))
            return

        if not changed:
            return

        self.sync_topic_tags([self.current_topic_key])

    def delete_topic_tag(self, tag_name: str) -> None:
        if not self.current_topic_key:
            return
        confirmation = QMessageBox.question(
            self,
            "Delete tag",
            f"Remove tag '{tag_name}' from this topic?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        try:
            removed = self.link_store.remove_topic_tag(self.current_topic_key, tag_name)
        except ValueError as exc:
            QMessageBox.warning(self, "Delete Tag", str(exc))
            return

        if not removed:
            return

        self.sync_topic_tags([self.current_topic_key])

    def open_path(self, path_value: str) -> None:
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.statusBar().showMessage(f"Could not open {path}", 5000)

    def open_link(self, url: str) -> None:
        cleaned = url.strip()
        if cleaned.lower().startswith("file://"):
            QDesktopServices.openUrl(QUrl(cleaned))
            return
        if "://" in cleaned or cleaned.lower().startswith("mailto:"):
            QDesktopServices.openUrl(QUrl(cleaned))
            return
        self.open_path(cleaned)

    def hub_for_topic(self, topic: Topic | None) -> HubConfig:
        if topic and topic.hub_key:
            hub = self.hubs_by_project_key.get(topic.hub_key)
            if hub:
                return hub
        return self.config.root_hub

    def current_hub(self) -> HubConfig:
        topic = (
            self.topics_by_key.get(self.current_topic_key)
            if self.current_topic_key
            else None
        )
        return self.hub_for_topic(topic)

    def display_source_path(self, topic: Topic, path: str) -> str:
        """Prefix a source path with the owning sub-hub project's name for display.

        A sub-hub's own OneNote/Outlook/PLM lists only know paths
        relative to that sub-hub (e.g. "01 - Projects / 010 Memory Testing"),
        so the parent project name is prepended to show the full hierarchy.
        """
        if not topic.hub_key:
            return path
        project_topic = self.topics_by_key.get(topic.hub_key)
        project_name = project_topic.name if project_topic else topic.hub_key
        if not path:
            return project_name
        return f"{project_name} / {path}"

    def open_onenote_link(self, _path_value: str) -> None:
        override = _path_value.strip()
        if override:
            self.open_link(override)
            return
        base_url = self.current_hub().onenote_base_url
        if not base_url:
            QMessageBox.warning(
                self, "OneNote", "Set ONENOTE_BASE_URL in .env to open OneNote links."
            )
            return
        QDesktopServices.openUrl(QUrl(base_url))

    def open_outlook_link(self, _path_value: str) -> None:
        override = _path_value.strip()
        if override:
            self.open_link(override)
            return
        base_url = self.current_hub().outlook_base_url
        if not base_url:
            QMessageBox.warning(
                self, "Outlook", "Set OUTLOOK_BASE_URL in .env to open Outlook links."
            )
            return
        QDesktopServices.openUrl(QUrl(base_url))

    def open_plm_link(self, _path_value: str) -> None:
        override = _path_value.strip()
        if override:
            self.open_link(override)
            return
        base_url = self.current_hub().plm_base_url
        if not base_url:
            QMessageBox.warning(
                self, "PLM", "Set PLM_BASE_URL in .env to open PLM links."
            )
            return
        QDesktopServices.openUrl(QUrl(base_url))

    def find_source_override(self, topic: Topic, name: str) -> ManualLink | None:
        """The bookmark named after a source; its URL is that source's direct link."""
        target = name.casefold()
        for link in topic.manual_links:
            if link.link_name.strip().casefold() == target:
                return link if link.url.strip() else None
        return None

    def find_manual_link_url(self, topic: Topic, name: str) -> str | None:
        link = self.find_source_override(topic, name)
        return link.url.strip() if link is not None else None

    def collect_tag_name_suggestions(self) -> list[str]:
        return self.link_store.list_tag_names()

    def obsidian_vault_for(self, path: Path) -> tuple[Path, str] | None:
        """Root and registered name of the vault holding a path.

        Looked up from the path itself, not from the selected topic's hub, so
        a context menu on another topic still names the right vault. Nested
        vaults win over the vault around them. A sub-hub folder inside the
        root vault has no name of its own and resolves to the root vault.
        """
        vaults: dict[Path, str] = {}
        for hub in (self.config.root_hub, *self.hubs_by_project_key.values()):
            if hub.obsidian_root and hub.obsidian_vault_name:
                vaults.setdefault(hub.obsidian_root.resolve(), hub.obsidian_vault_name)
        for root in sorted(vaults, key=lambda item: len(item.parts), reverse=True):
            if path.is_relative_to(root):
                return root, vaults[root]
        return None

    def open_obsidian_path(self, path_value: str) -> None:
        path = Path(path_value).expanduser().resolve()
        vault = self.obsidian_vault_for(path)

        if path.is_dir():
            selected = self.select_markdown_file(path)
            if selected is None:
                if vault is None:
                    # obsidian://open?path=<folder> would add the folder as a
                    # new vault and open it in a window of its own.
                    self.open_path(str(path))
                else:
                    self.send_obsidian_uri("open", {"vault": vault[1]})
                return
            path = selected

        if vault is None:
            # Obsidian matches the path against its registered vaults itself.
            self.send_obsidian_uri("open", {"path": str(path)})
            return

        root, vault_name = vault
        relative = path.relative_to(root).as_posix()
        command_id = self.config.obsidian_reveal_command.strip()
        if self.config.obsidian_reveal_active and command_id:
            # Advanced URI opens the file and then runs the command in one
            # call. A second URI sent while Obsidian is still starting would
            # launch a second instance.
            self.send_obsidian_uri(
                "advanced-uri",
                {"vault": vault_name, "filepath": relative, "commandid": command_id},
            )
        else:
            self.send_obsidian_uri("open", {"vault": vault_name, "file": relative})

    def send_obsidian_uri(self, action: str, params: dict[str, str]) -> None:
        # Until a cold-started Obsidian claims its single-instance lock, every
        # further URI launches another instance, so repeat clicks made while
        # it starts are dropped.
        now = time.monotonic()
        if now - self._last_obsidian_uri_at < OBSIDIAN_URI_COOLDOWN_S:
            self.statusBar().showMessage("Obsidian is still opening…", 3000)
            return
        self._last_obsidian_uri_at = now
        url = QUrl(f"obsidian://{action}")
        query = QUrlQuery()
        for key, value in params.items():
            query.addQueryItem(key, value)
        url.setQuery(query)
        QDesktopServices.openUrl(url)

    def select_markdown_file(self, folder: Path) -> Path | None:
        candidates = [
            folder / f"{folder.name}.md",
            folder / "index.md",
            folder / "_index.md",
            folder / "README.md",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate

        markdown_files = []
        for item in folder.rglob("*.md"):
            relative = item.relative_to(folder)
            if any(part.startswith(".") for part in relative.parts):
                continue
            markdown_files.append(item)

        if not markdown_files:
            return None

        markdown_files.sort(
            key=lambda item: (
                len(item.relative_to(folder).parts),
                str(item).casefold(),
            )
        )
        return markdown_files[0]

    def center_on_screen(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if not screen:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())
