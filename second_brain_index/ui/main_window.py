from __future__ import annotations

import json
import os
import sqlite3
import threading
from functools import partial
from pathlib import Path

from PyQt6.QtCore import (
    QEventLoop,
    QFileSystemWatcher,
    QObject,
    QSettings,
    Qt,
    QThread,
    QTimer,
    QUrl,
    QUrlQuery,
    pyqtSignal,
)
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
    QProgressDialog,
    QScrollArea,
    QSplitter,
    QToolTip,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, DEFAULT_TOPIC_LIST_NAMES, HubConfig, MARKER_FILENAME
from ..constants import (
    OTHER_STATE,
    STATE_FOLDERS,
    display_name_from_key,
    hub_key_prefix,
    is_hub_key,
    is_other_key,
    make_hub_key,
    split_hub_key,
)
from ..db import ManualLinkStore
from ..filename_index import FileEntry, FilenameIndexWorker
from ..indexer import SOURCE_SPECS, ScanResult, SourceSpec, run_scan
from ..json_lists import empty_topics_document
from ..location_filter import (
    ALL_LOCATIONS,
    LocationFilter,
    LocationOptions,
    allowed_index_roots,
    build_location_options,
    filter_file_entries,
)
from ..models import ManualLink, ManualLocation, Topic, TopicLocation
from ..resources import app_icon
from ..search import SearchToken, rank_filename_hits
from ..utils import infer_link_type
from .dialogs.manual_link_dialog import ManualLinkDialog
from .dialogs.operation_confirm import OperationConfirmDialog
from .dialogs.rename_topic_dialog import RenameTopicDialog
from .dialogs.source_entry_dialog import SourceEntryDialog
from .dialogs.subhub_dialog import InitializeSubHubDialog
from .dialogs.tag_dialog import TopicTagDialog
from .dialogs.topic_picker_dialog import TopicPickerDialog
from .operations import (
    KEEP_SECTIONS,
    TopicOperationsMixin,
    UndoAborted,
    clean_manual_action_text,
    merge_conflict_dirname,
)
from .widgets.bookmarks_panel import BookmarksPanel
from .widgets.bulk_panel import BulkPanel
from .widgets.common import current_theme
from .widgets.header_bar import HeaderBarMixin
from .dialogs.direct_link_dialog import DirectLinkDialog
from .dialogs.location_dialog import LocationDialog
from .widgets.locations_list import LocationRow, LocationsList
from .widgets.matching_files_panel import MatchingFilesPanel
from .widgets.status_bar import StatusBarMixin
from .widgets.topic_header import TopicHeaderMixin
from .widgets.topic_tree import TopicTreeMixin


MAX_FILENAME_HITS = 50
# Scans faster than this never show the progress dialog.
SCAN_PROGRESS_DELAY_MS = 800

class _ScanRelay(QObject):
    """Carries a scan's outcome from its worker thread to the GUI thread."""

    finished = pyqtSignal(object, object)

    def __init__(self, loop: QEventLoop) -> None:
        super().__init__()
        self.loop: QEventLoop | None = loop
        self.delivered = False
        self.result: ScanResult | None = None
        self.error: Exception | None = None
        self.finished.connect(self._on_finished)

    def _on_finished(self, result, error) -> None:
        # A cancelled scan can report long after its refresh returned.
        if self.loop is None:
            return
        self.result = result
        self.error = error
        self.delivered = True
        self.loop.quit()

class MainWindow(
    HeaderBarMixin,
    TopicTreeMixin,
    TopicHeaderMixin,
    StatusBarMixin,
    TopicOperationsMixin,
    QMainWindow,
):
    def __init__(self, config: AppConfig, link_store: ManualLinkStore) -> None:
        super().__init__()
        self.config = config
        self.link_store = link_store
        self.topics_by_key: dict[str, Topic] = {}
        self.topic_keys_by_normalized: dict[str, str] = {}
        self.hubs_by_project_key: dict[str, HubConfig] = {}
        self._subtopics_by_hub: dict[str, dict[str, list[tuple[str, Topic]]]] = {}
        self.tree_items_by_key: dict[str, QTreeWidgetItem] = {}
        self.current_topic_key: str | None = None
        self.filename_index_by_key: dict[str, str] = {}
        self.filename_entries_by_key: dict[str, tuple[FileEntry, ...]] = {}
        self._active_tokens: list[SearchToken] = []
        # Session-only: every launch starts at All locations.
        self.location_filter: LocationFilter = ALL_LOCATIONS
        # Filename blobs limited to the active location, built on demand.
        self._filtered_filename_blobs: dict[str, str] = {}
        # Roots the last scan found missing; the Location menu marks them.
        self.missing_roots: frozenset[Path] = frozenset()
        self.match_fields_by_key: dict[str, set[str]] = {}
        self._filename_index_generation = 0
        self._filename_index_worker: FilenameIndexWorker | None = None
        self._filename_index_threads: list[QThread] = []
        self._refresh_in_progress = False
        self._refresh_pending = False
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
            populate_json_menu=self._populate_locations_json_menu,
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

    def _setup_file_watcher(self) -> None:
        self._fs_watcher: QFileSystemWatcher | None = None
        if not self.config.auto_refresh:
            return
        self._fs_watcher = QFileSystemWatcher(self)
        self._watch_debounce = QTimer(self)
        self._watch_debounce.setSingleShot(True)
        self._watch_debounce.setInterval(1500)
        self._watch_debounce.timeout.connect(self._on_watched_change_settled)
        self._fs_watcher.directoryChanged.connect(self._on_watched_change)
        self._fs_watcher.fileChanged.connect(self._on_watched_change)

    def _on_watched_change(self, _path: str) -> None:
        self._watch_debounce.start()

    def _on_watched_change_settled(self) -> None:
        self.statusBar().showMessage("Change detected — refreshing…", 3000)
        self.refresh_index(interactive=False)

    def _watch_candidate_paths(self) -> set[str]:
        paths: set[str] = set()
        roots = list(self.config.root_hub.para_roots)
        if self.config.root_hub.obsidian_root:
            roots.append(self.config.root_hub.obsidian_root)
        for root in roots:
            for folder_name in STATE_FOLDERS.values():
                state_dir = root / folder_name
                if state_dir.is_dir():
                    paths.add(str(state_dir))
        for other_root in self.config.other_folders:
            if other_root.is_dir():
                paths.add(str(other_root))
        json_sources = []
        for spec in SOURCE_SPECS:
            if spec.enabled(self.config.root_hub):
                json_sources.append(spec.topics_path(self.config.root_hub))
        for hub in self.hubs_by_project_key.values():
            hub_roots = list(hub.para_roots)
            if hub.obsidian_root:
                hub_roots.append(hub.obsidian_root)
            for root in hub_roots:
                for folder_name in STATE_FOLDERS.values():
                    state_dir = root / folder_name
                    if state_dir.is_dir():
                        paths.add(str(state_dir))
            if hub.marker_path and hub.marker_path.is_file():
                paths.add(str(hub.marker_path))
            for spec in SOURCE_SPECS:
                if spec.enabled(hub):
                    json_sources.append(spec.topics_path(hub))
        for source in json_sources:
            if source.is_file():
                paths.add(str(source))
        return paths

    def _update_watch_paths(self) -> None:
        if self._fs_watcher is None:
            return
        desired = self._watch_candidate_paths()
        current = set(self._fs_watcher.directories()) | set(self._fs_watcher.files())
        stale = current - desired
        if stale:
            self._fs_watcher.removePaths(sorted(stale))
        missing = desired - current
        if missing:
            self._fs_watcher.addPaths(sorted(missing))

    def _setup_shortcuts(self) -> None:
        for sequence in ("Ctrl+F", "Ctrl+K"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(self.focus_search)
        for sequence in ("F5", "Ctrl+R"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(self.refresh_index)

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

    def refresh_index(self, interactive: bool = True) -> None:
        """Rescan every root and rebuild the view.

        Blocks the caller until the scan is done, but the scan itself runs on
        a worker thread while a local event loop keeps the window painting.
        """
        if self._closed:
            return
        if self._refresh_in_progress:
            self._refresh_pending = True
            return
        self._refresh_in_progress = True
        try:
            self._run_refresh(interactive)
        finally:
            self._refresh_in_progress = False
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh_index(interactive=False)

    def _run_refresh(self, interactive: bool) -> None:
        if (
            not self.config.root_hub.para_roots
            and not self.config.root_hub.obsidian_root
            and not self.config.other_folders
        ):
            self._report_refresh_error(
                "Missing configuration",
                "Set PARA_ROOTS, OBSIDIAN_VAULT, or OTHER_FOLDERS in .env before scanning.",
                interactive,
            )

        # The store's SQLite connection belongs to this thread, so everything
        # the scan needs from it is read up front.
        manual_links = self.link_store.list_links()
        topic_tags = self.link_store.list_topic_tags()

        cancelled = threading.Event()
        loop = QEventLoop(self)
        relay = _ScanRelay(loop)
        config = self.config

        def work() -> None:
            result: ScanResult | None = None
            error: Exception | None = None
            try:
                result = run_scan(config, manual_links, cancelled.is_set)
            except Exception as exc:  # reported to the user, never raised here
                error = exc
            try:
                relay.finished.emit(result, error)
            except RuntimeError:
                pass  # the application went away while a dead mount held us

        self._active_scan = (cancelled, loop)
        holder, timer = self._make_scan_progress(cancelled, loop)
        threading.Thread(target=work, name="topic-scan", daemon=True).start()
        try:
            # Clicks are held back until the modal dialog is up, so nothing can
            # start a rename or move against a half-refreshed window.
            loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            if not relay.delivered and not cancelled.is_set():
                loop.exec()
        finally:
            self._active_scan = None
            relay.loop = None
            timer.stop()
            timer.deleteLater()
            dialog = holder.get("dialog")
            if dialog is not None:
                # closeEvent emits canceled(); that must not look like the user
                # pressing Cancel on a scan that already finished.
                dialog.canceled.disconnect()
                dialog.close()
                dialog.deleteLater()

        if cancelled.is_set() or not relay.delivered:
            cancelled.set()
            self.statusBar().showMessage("Refresh cancelled", 5000)
            return
        if relay.error is not None or relay.result is None:
            self._report_refresh_error(
                "Refresh failed", str(relay.error), interactive
            )
            return

        result = relay.result
        self.topics_by_key = result.topics_by_key
        self.hubs_by_project_key = result.hubs_by_project_key
        self.missing_roots = result.missing_roots
        for title, message in result.errors:
            self._report_refresh_error(title, message, interactive)
        self.rebuild_normalized_topic_keys()
        self.sources_button.setVisible(self._any_sources_enabled())
        self.add_topic_tags(topic_tags)
        self.add_pinned_ranks()
        self.add_manual_locations(self.link_store.list_locations())
        if self.focused_hub_key and self.focused_hub_key not in self.hubs_by_project_key:
            self.focused_hub_key = None
        location_notice = self._revalidate_location_filter()
        self.apply_filter()
        self.update_status_summary()
        self.start_filename_indexing()
        if location_notice:
            # After indexing starts, so its own message does not replace this one.
            self.statusBar().showMessage(location_notice, 8000)
        self._update_watch_paths()

    def _make_scan_progress(
        self, cancelled: threading.Event, loop: QEventLoop
    ) -> tuple[dict[str, QProgressDialog], QTimer]:
        """Arm the delayed "Scanning topics…" dialog; fast scans never see it."""
        holder: dict[str, QProgressDialog] = {}
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(SCAN_PROGRESS_DELAY_MS)

        def reveal() -> None:
            dialog = QProgressDialog("Scanning topics…", "Cancel", 0, 0, self)
            dialog.setWindowTitle("Refreshing")
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.setAutoClose(False)
            dialog.setAutoReset(False)
            dialog.canceled.connect(partial(self._cancel_scan, cancelled, loop))
            dialog.show()
            holder["dialog"] = dialog
            # Leave the input-excluding wait; the modal dialog guards from here.
            loop.quit()

        timer.timeout.connect(reveal)
        timer.start()
        return holder, timer

    def _cancel_scan(self, cancelled: threading.Event, loop: QEventLoop) -> None:
        cancelled.set()
        loop.quit()

    def _report_refresh_error(
        self, title: str, message: str, interactive: bool
    ) -> None:
        if interactive:
            QMessageBox.warning(self, title, message)
        else:
            self.statusBar().showMessage(f"{title}: {message}", 8000)

    def start_filename_indexing(self) -> None:
        self._filename_index_generation += 1
        self.cancel_filename_indexing()
        paths_by_key = {
            key: paths
            for key, topic in self.topics_by_key.items()
            if (paths := self.topic_index_paths(topic))
        }
        if not paths_by_key:
            # Nothing left to walk: drop what an earlier walk found.
            self.filename_index_by_key = {}
            self.filename_entries_by_key = {}
            self._filtered_filename_blobs = {}
            return

        thread = QThread(self)
        worker = FilenameIndexWorker(self._filename_index_generation, paths_by_key)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.on_filename_index_progress)
        worker.finished.connect(self.on_filename_index_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(
            partial(self._discard_filename_thread, thread)
        )
        self._filename_index_worker = worker
        self._filename_index_threads.append(thread)
        self.statusBar().showMessage("Indexing filenames…")
        thread.start()

    @staticmethod
    def topic_index_paths(topic: Topic) -> list[Path]:
        """Folders whose filenames are searched: discovered ones, then added ones.

        An added folder that is, or sits inside, one already listed is skipped
        so its files are not found twice.
        """
        paths = [location.path for location in topic.locations]
        for location in topic.manual_locations:
            if location.kind != "file":
                continue
            path = Path(location.target).expanduser()
            if any(path == known or known in path.parents for known in paths):
                continue
            paths.append(path)
        return paths

    def cancel_filename_indexing(self) -> None:
        if self._filename_index_worker is not None:
            self._filename_index_worker.cancel()
            self._filename_index_worker = None
        for thread in self._filename_index_threads:
            thread.quit()

    def _discard_filename_thread(self, thread: QThread) -> None:
        if thread in self._filename_index_threads:
            self._filename_index_threads.remove(thread)
        thread.deleteLater()

    def on_filename_index_progress(self, generation: int, done: int, total: int) -> None:
        if generation != self._filename_index_generation:
            return
        self.statusBar().showMessage(f"Indexing filenames… ({done}/{total})")

    def on_filename_index_finished(self, generation: int, indexes: dict) -> None:
        if generation != self._filename_index_generation:
            return
        # Replaced wholesale, not cleared on refresh: the previous index keeps
        # answering searches until the new walk lands.
        self.filename_index_by_key = {
            key: index.blob for key, index in indexes.items()
        }
        self.filename_entries_by_key = {
            key: index.entries for key, index in indexes.items()
        }
        self._filtered_filename_blobs = {}
        self.statusBar().showMessage("Filename index ready", 3000)
        if self.search_input.text().strip() and self.filename_search_toggle.isChecked():
            self.apply_filter()

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
        settings = QSettings()
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

    def _restore_settings(self) -> bool:
        settings = QSettings()
        restored_geometry = False
        geometry = settings.value("window/geometry")
        if geometry is not None:
            restored_geometry = bool(self.restoreGeometry(geometry))
        splitter_state = settings.value("window/splitter")
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)
        include_filenames = settings.value("search/include_filenames")
        if include_filenames is not None:
            self.filename_search_toggle.setChecked(
                include_filenames in (True, "true", "1", 1)
            )
        saved_topic = settings.value("session/current_topic", "")
        if saved_topic:
            self.current_topic_key = str(saved_topic)
        group_others = settings.value("view/others_group_by_parent")
        if group_others is not None:
            self._group_others_by_parent = group_others in (True, "true", "1", 1)
        raw_sections = settings.value("session/expanded_sections", "")
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

    def add_topic_tags(self, topic_tags: dict[str, list[str]]) -> None:
        for topic_key, tags in topic_tags.items():
            topic = self.topics_by_key.get(topic_key)
            if not topic:
                continue
            topic.tags = sorted(tags, key=lambda value: (value.casefold(), value))

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

    def add_manual_locations(self, locations: list[ManualLocation]) -> None:
        """Attach user-added links and folders to the topics the scan found."""
        for location in locations:
            topic = self.topics_by_key.get(location.topic_name)
            if topic:
                topic.manual_locations.append(location)
        for topic in self.topics_by_key.values():
            topic.manual_locations.sort(
                key=lambda loc: (loc.display_label.casefold(), loc.display_label)
            )

    def add_pinned_ranks(self) -> None:
        for topic_key, rank in self.link_store.list_pinned().items():
            topic = self.topics_by_key.get(topic_key)
            if topic:
                topic.pinned_rank = rank

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

    def get_topic_filename_blob(self, topic_key: str) -> str:
        if self.location_filter.is_all():
            return self.filename_index_by_key.get(topic_key, "")
        blob = self._filtered_filename_blobs.get(topic_key)
        if blob is None:
            entries = self.location_filtered_entries(topic_key)
            blob = "\n".join(sorted({entry.name.casefold() for entry in entries}))
            self._filtered_filename_blobs[topic_key] = blob
        return blob

    def location_filtered_entries(self, topic_key: str) -> tuple[FileEntry, ...]:
        """The topic's indexed files that lie in the active location."""
        entries = self.filename_entries_by_key.get(topic_key, ())
        topic = self.topics_by_key.get(topic_key)
        if topic is None or self.location_filter.is_all():
            return entries
        return filter_file_entries(
            entries, allowed_index_roots(topic, self.location_filter)
        )

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

    def _update_matching_files(self, topic_key: str | None) -> None:
        entries = self.location_filtered_entries(topic_key or "")
        if (
            not entries
            or not self._active_tokens
            or not self.filename_search_toggle.isChecked()
        ):
            self.matching_files_panel.set_hits([], 0)
            return
        hits = rank_filename_hits(
            [entry.name for entry in entries], self._active_tokens
        )
        # Folders first, then files, each alphabetical.
        matches = sorted(
            (entries[index] for index in hits),
            key=lambda entry: (
                not entry.is_dir,
                entry.name.casefold(),
                entry.rel_path.casefold(),
            ),
        )
        self.matching_files_panel.set_hits(
            matches[:MAX_FILENAME_HITS],
            len(hits),
            self._active_tokens,
        )

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
        header.set_notice(
            self.inconsistency_notice(topic) if topic.has_inconsistency else None
        )
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
        confirmation = QMessageBox.question(
            self,
            "Remove location",
            f"Remove '{location.display_label}' from this topic?\n\n"
            "The folder or page itself is not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        self.link_store.delete_location(location.id)
        topic = self.topics_by_key.get(location.topic_name)
        if topic:
            topic.manual_locations = [
                item for item in topic.manual_locations if item.id != location.id
            ]
        self.apply_filter()
        if location.kind == "file":
            self.start_filename_indexing()
        self.show_topic_details(self.current_topic_key)

    def _populate_locations_json_menu(self, menu: QMenu) -> None:
        """Offer the managed JSON lists the current topic is not in yet."""
        topic = self.topics_by_key.get(self.current_topic_key or "")
        if topic is not None:
            self.populate_json_source_menu(menu, topic)

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

    def subhub_destination_root(self) -> Path | None:
        if self.config.root_hub.para_roots:
            return self.config.root_hub.para_roots[0]
        return self.config.root_hub.obsidian_root

    def initialize_subhub(self) -> None:
        destination_root = self.subhub_destination_root()
        if destination_root is None:
            QMessageBox.warning(
                self,
                "Init Sub-Hub",
                "Set PARA_ROOTS or OBSIDIAN_VAULT before creating a Sub-Hub.",
            )
            return

        dialog = InitializeSubHubDialog(
            self,
            destination_root,
            lambda name: self.validate_subhub_project_name(name, destination_root),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        name = dialog.name()
        project_path = dialog.destination_path()
        try:
            self.create_subhub_structure(project_path)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Init Sub-Hub",
                f"Unable to create Sub-Hub at {project_path}: {exc}",
            )
            return

        self.current_topic_key = name
        self.refresh_index()
        if name in self.topics_by_key:
            item = self.tree_items_by_key.get(name)
            if item:
                self.tree.setCurrentItem(item)
            self.show_topic_details(name)
        self.statusBar().showMessage(f"Initialized Sub-Hub '{name}'", 4000)

    def validate_subhub_project_name(
        self, name: str, destination_root: Path
    ) -> str | None:
        error = self.validate_topic_name(name)
        if error:
            return error
        project_path = destination_root / STATE_FOLDERS["Projects"] / name
        if project_path.exists():
            return f"Destination already exists: {project_path}"
        existing = self.find_colliding_topic_key(name, "")
        if existing is not None:
            return f"A topic named '{display_name_from_key(existing)}' already exists."
        return None

    def create_subhub_structure(self, project_path: Path) -> None:
        project_path.mkdir(parents=True, exist_ok=False)
        for folder_name in STATE_FOLDERS.values():
            (project_path / folder_name).mkdir()
        marker_payload = json.dumps(
            self.build_subhub_marker_template(project_path.name),
            indent=2,
        )
        (project_path / MARKER_FILENAME).write_text(
            f"{marker_payload}\n", encoding="utf-8"
        )
        payload = json.dumps(empty_topics_document(), indent=2)
        for filename in DEFAULT_TOPIC_LIST_NAMES.values():
            (project_path / filename).write_text(payload, encoding="utf-8")

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
        elsewhere = (
            topic.locations
            or topic.manual_locations
            or any(other.paths_of(topic) for other in SOURCE_SPECS if other is not spec)
        )
        if not elsewhere:
            lines.append("This topic exists only in this list and will disappear.")
        confirmation = QMessageBox.question(
            self,
            f"Remove from {spec.name}",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
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
        self.refresh_index()

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

    def rename_topic(self) -> None:
        if not self.current_topic_key:
            return
        if is_other_key(self.current_topic_key):
            return
        topic = self.topics_by_key.get(self.current_topic_key)
        if not topic:
            return

        old_key = self.current_topic_key
        old_name = topic.name

        dialog = RenameTopicDialog(
            self,
            old_name,
            lambda value, section: self.build_rename_preview(
                old_key, topic, old_name, value, section
            ),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        preview = dialog.preview()
        if preview is None or preview.error:
            return

        new_name = preview.new_name
        new_key = preview.new_key
        operations = preview.operations
        merging = preview.is_merge
        verb = "Merge" if merging else "Rename"

        topic_hub = self.hub_for_topic(topic)
        json_targets, json_errors = self.prepare_json_rename_targets(topic_hub)
        if json_errors:
            message = [
                f"{verb} aborted because JSON sources could not be read:",
                *json_errors,
            ]
            QMessageBox.warning(self, f"{verb} failed", "\n".join(message))
            return

        rename_errors, completed, moves = self.execute_rename_operations(
            operations, merge_conflict_dirname(old_name)
        )
        if rename_errors:
            QMessageBox.warning(self, f"{verb} failed", "\n".join(rename_errors))
            return

        landing_state = preview.target_state
        if landing_state == KEEP_SECTIONS:
            landing_state = None
        json_errors, written_targets = self.apply_json_renames(
            json_targets, old_name, new_name, landing_state
        )
        if json_errors:
            rollback_errors = self.rollback_rename_operations(completed)
            for error in rollback_errors:
                json_errors.append(f"Rollback failed: {error}")
            message = [f"{verb} aborted; changes were rolled back.", *json_errors]
            QMessageBox.warning(self, f"{verb} failed", "\n".join(message))
            return

        migrated_link_ids = self.link_store.link_ids_for_topic(old_key)
        migrated_location_ids = self.link_store.location_ids_for_topic(old_key)
        pin_ranks = self.link_store.list_pinned()
        tags_before = self.link_store.list_topic_tags()
        old_tags = list(tags_before.get(old_key, []))
        new_tags = list(tags_before.get(new_key, []))
        try:
            # One transaction: a failure part-way must not leave the bookmarks
            # under the new key while the folders go back to the old name.
            with self.link_store.transaction():
                self.link_store.reassign_topic_key(old_key, new_key)
                if old_key in self.hubs_by_project_key:
                    # Renaming a sub-hub project shifts every child topic key.
                    self.link_store.reassign_topic_prefix(
                        hub_key_prefix(old_key), hub_key_prefix(new_key)
                    )
                removed_links = self.link_store.dedupe_manual_links(
                    new_key, migrated_link_ids
                )
                self.link_store.reassign_locations_by_id(
                    migrated_location_ids, new_key
                )
                removed_locations = self.link_store.dedupe_manual_locations(
                    new_key, migrated_location_ids
                )
        except Exception as exc:
            rollback_errors = self.restore_json_targets(written_targets)
            rollback_errors.extend(self.rollback_rename_operations(completed))
            message = [f"Topic metadata update failed: {exc}"]
            for error in rollback_errors:
                message.append(f"Rollback failed: {error}")
            QMessageBox.warning(self, f"{verb} failed", "\n".join(message))
            return

        # Folders are already renamed on disk; a failure here must not roll back.
        tagged_previous_name = False
        # Undo must not strip a tag that was there before this operation.
        already_tagged = old_name.casefold() in {
            tag.casefold() for tag in (*old_tags, *new_tags)
        }
        if self.should_tag_previous_name(old_name, new_name):
            tag_error = self.record_previous_name_tag(new_key, old_name)
            if tag_error:
                QMessageBox.warning(
                    self,
                    verb,
                    f"{verb}d to '{new_name}', but the previous name was not "
                    f"saved as a tag.\n{tag_error}",
                )
            else:
                tagged_previous_name = not already_tagged

        journalled = self.journal_rename(
            kind="merge" if merging else "rename",
            old_key=old_key,
            new_key=new_key,
            old_name=old_name,
            new_name=new_name,
            moves=moves,
            boundaries=[op.target for op in completed if op.kind == "merge"],
            json_targets=written_targets,
            migrated_link_ids=migrated_link_ids,
            removed_links=removed_links,
            migrated_location_ids=migrated_location_ids,
            removed_locations=removed_locations,
            tagged_previous_name=tagged_previous_name,
            old_pin_rank=pin_ranks.get(old_key),
            new_pin_rank=pin_ranks.get(new_key),
            old_tags=old_tags,
            new_tags=new_tags,
            hub_renamed=old_key in self.hubs_by_project_key,
        )

        self.current_topic_key = new_key
        self.refresh_index()
        if journalled:
            action = (
                f"Merged '{old_name}' into '{new_name}'"
                if merging
                else f"Renamed '{old_name}' to '{new_name}'"
            )
            self.show_undo_prompt(action, "merge" if merging else "rename")

    def undo_last_operation(self) -> None:
        record = self.link_store.latest_operation()
        if record is None:
            self.clear_undo_prompt()
            self.statusBar().showMessage("Nothing to undo", 4000)
            return
        _record_id, kind, payload = record
        label = "merge" if kind == "merge" else "rename"
        old_name = str(payload.get("old_name", ""))
        new_name = str(payload.get("new_name", ""))
        blockers = self.undo_blockers(payload)
        if blockers:
            self.link_store.clear_operations()
            self.clear_undo_prompt()
            QMessageBox.warning(
                self,
                f"Undo {label} unavailable",
                "\n".join(
                    [
                        f"The {label} of '{old_name}' into '{new_name}' can no "
                        "longer be undone; nothing was changed.",
                        *blockers,
                    ]
                ),
            )
            return
        if not self.confirm_destructive_operation(
            f"Undo {label}",
            f"Undo the {label} of '{old_name}' into '{new_name}'?",
            details=[
                f"{len(payload.get('moves', []))} file/folder move(s) will be reversed.",
                "JSON source lists and bookmarks will be restored.",
            ],
            accept_label="Undo",
        ):
            return

        try:
            errors = self.apply_undo(payload)
        except UndoAborted as aborted:
            # The journal stays: once whatever holds the folders lets go, the
            # same undo can be tried again.
            QMessageBox.warning(
                self,
                f"Undo {label} failed",
                "\n".join(
                    [
                        "No folder could be moved back, so nothing else was "
                        "changed. Undo is still available.",
                        *aborted.errors,
                    ]
                ),
            )
            return
        self.link_store.clear_operations()
        self.clear_undo_prompt()
        self.current_topic_key = str(payload.get("old_key") or "") or None
        self.refresh_index()
        if errors:
            QMessageBox.warning(
                self,
                f"Undo {label} completed with warnings",
                "\n".join(errors),
            )
        else:
            self.statusBar().showMessage(f"Undid {label} of '{old_name}'", 5000)

    def archive_selected_topics(self) -> None:
        topics = self.get_selected_topics()
        if topics:
            self.archive_topics(topics)

    def archive_topics(self, topics: list[tuple[str, Topic]]) -> None:
        self.move_topics(topics, "Archive")

    def confirm_destructive_operation(
        self,
        title: str,
        text: str,
        details: list[str],
        manual_actions: list[str] | None = None,
        merge_conflicts: list[str] | None = None,
        accept_label: str = "Continue",
    ) -> bool:
        dialog = OperationConfirmDialog(
            self,
            title,
            text,
            details=details,
            manual_actions=manual_actions or [],
            merge_conflicts=merge_conflicts or [],
            accept_label=accept_label,
        )
        return dialog.exec() == QDialog.DialogCode.Accepted

    def report_operation_warnings(
        self,
        verb: str,
        resolve_errors: list[str],
        move_errors: list[str],
        merge_conflicts: list[str],
        json_update_errors: list[str],
    ) -> None:
        if not (resolve_errors or move_errors or merge_conflicts or json_update_errors):
            return
        message_parts = []
        if resolve_errors:
            message_parts.append(
                "Could not resolve destination for:\n" + "\n".join(resolve_errors)
            )
        if move_errors:
            message_parts.append("Failed to move:\n" + "\n".join(move_errors))
        if merge_conflicts:
            message_parts.append(
                "Conflicting items kept in a 'Merged from …' subfolder:\n"
                + "\n".join(merge_conflicts)
            )
        if json_update_errors:
            message_parts.append(
                "Failed to update JSON sources:\n" + "\n".join(json_update_errors)
            )
        QMessageBox.warning(
            self,
            f"{verb} completed with warnings",
            "\n\n".join(message_parts),
        )

    def move_topics(
        self, topics: list[tuple[str, Topic]], target_state: str
    ) -> None:
        target_label = STATE_FOLDERS[target_state]
        archiving = target_state == "Archive"
        action_word = "Archive" if archiving else "Move"
        eligible: list[Topic] = []
        for key, topic in topics:
            if is_other_key(key):
                continue
            has_active_locations = any(
                loc.state not in {target_state, OTHER_STATE}
                for loc in topic.locations
            )
            # A source whose state cannot be inferred still needs updating.
            has_json_active = any(
                not states or states != {target_state}
                for _name, states in self.get_json_source_states(topic)
            )
            if has_active_locations or has_json_active:
                eligible.append(topic)
        if not eligible:
            QMessageBox.information(
                self,
                f"Nothing to {action_word.lower()}",
                f"The selected topic(s) are already in '{target_label}'.",
            )
            return

        manual_actions: list[str] = []
        for topic in eligible:
            actions = self.collect_manual_move_actions(topic, target_state)
            if actions:
                if len(eligible) > 1:
                    manual_actions.extend(
                        f"{topic.name}: {clean_manual_action_text(action)}"
                        for action in actions
                    )
                else:
                    manual_actions.extend(actions)

        json_targets, json_errors = self.prepare_json_archive_targets(eligible)
        if json_errors:
            message = [
                f"{action_word} aborted because JSON sources could not be read:",
                *json_errors,
            ]
            QMessageBox.warning(self, f"{action_word} failed", "\n".join(message))
            return

        operations: list[tuple[TopicLocation, Path]] = []
        operation_topics: list[Topic] = []
        resolve_errors: list[str] = []
        for topic in eligible:
            for location in topic.locations:
                if location.state in {target_state, OTHER_STATE}:
                    continue
                destination = self.resolve_move_destination(location, target_state)
                if not destination:
                    resolve_errors.append(str(location.path))
                    continue
                operations.append((location, destination))
                operation_topics.append(topic)

        if not operations and not json_targets:
            QMessageBox.warning(
                self,
                f"{action_word} failed",
                "No valid move destinations were found for the selected topic(s).",
            )
            return

        topic_names = [topic.name for topic in eligible]
        if operations:
            confirm_text = (
                f"{action_word} {len(topic_names)} topic(s): move {len(operations)} "
                f"folder(s) to '{target_label}'? This will move data on disk."
            )
            if json_targets:
                confirm_text += " JSON sources will be updated."
        else:
            confirm_text = (
                f"Move {len(topic_names)} topic(s) to '{target_label}' "
                "in the JSON sources?"
            )

        details = self.describe_operations(operations, json_targets, topic_names)
        conflicts = [op for op in operations if op[1].exists()]
        if not self.confirm_destructive_operation(
            f"{action_word} topic(s)",
            confirm_text,
            details,
            manual_actions=manual_actions,
            merge_conflicts=[str(destination) for _loc, destination in conflicts],
            accept_label=action_word,
        ):
            return

        move_errors, merge_conflicts = self.execute_move_operations(operations)

        # A folder that is still in place was not moved; listing its topic
        # under the new section anyway would leave the two disagreeing.
        stuck = {
            id(topic): topic
            for topic, (location, _destination) in zip(operation_topics, operations)
            if os.path.lexists(location.path)
        }
        moved = [topic for topic in eligible if id(topic) not in stuck]

        json_update_errors = [
            f"Left unchanged for '{topic.name}': its folder could not be moved."
            for topic in stuck.values()
            if any(spec.paths_of(topic) for spec in SOURCE_SPECS)
        ]
        # Read again rather than reusing what the confirmation dialog showed:
        # the lists may have been edited while it was open.
        json_targets, json_errors = self.prepare_json_archive_targets(moved)
        json_update_errors.extend(json_errors)
        if json_targets:
            update_errors, _ = self.apply_json_moves(json_targets, target_state)
            json_update_errors.extend(update_errors)

        self.report_operation_warnings(
            action_word, resolve_errors, move_errors, merge_conflicts,
            json_update_errors,
        )
        self.refresh_index()
        if archiving:
            status_message = f"Archived {len(moved)} topic(s)"
        else:
            status_message = f"Moved {len(moved)} topic(s) to '{target_label}'"
        self.statusBar().showMessage(status_message, 4000)

    def _resolve_location_root(self, location: TopicLocation) -> Path | None:
        state_folder_names = set(STATE_FOLDERS.values())
        for parent in (location.path.parent, *location.path.parents):
            if parent.name in state_folder_names:
                return parent.parent

        if location.source == "obsidian":
            for hub in self.hubs_by_project_key.values():
                if not hub.obsidian_root:
                    continue
                try:
                    location.path.relative_to(hub.obsidian_root)
                except ValueError:
                    continue
                return hub.obsidian_root
            return self.config.root_hub.obsidian_root

        candidates = list(self.config.root_hub.para_roots)
        for hub in self.hubs_by_project_key.values():
            candidates.extend(hub.para_roots)
        for candidate in candidates:
            try:
                location.path.relative_to(candidate)
            except ValueError:
                continue
            return candidate
        return None

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

    def collect_link_name_suggestions(self) -> list[str]:
        names = {
            link.link_name
            for topic in self.topics_by_key.values()
            for link in topic.manual_links
            if link.link_name.strip()
        }
        return sorted(names, key=str.casefold)

    def collect_tag_name_suggestions(self) -> list[str]:
        return self.link_store.list_tag_names()

    def obsidian_vault_pair(self, hub: HubConfig) -> tuple[Path, str] | None:
        """Vault root and registered vault name to build obsidian:// URIs with.

        A sub-hub folder inside the root vault is not a registered vault of
        its own, so the root hub's pair is used and file paths stay relative
        to the root vault.
        """
        if hub.obsidian_root and hub.obsidian_vault_name:
            return hub.obsidian_root, hub.obsidian_vault_name
        root = self.config.root_hub
        if root.obsidian_root and root.obsidian_vault_name:
            return root.obsidian_root, root.obsidian_vault_name
        return None

    def open_obsidian_path(self, path_value: str) -> None:
        path = Path(path_value).expanduser().resolve()
        hub = self.current_hub()
        pair = self.obsidian_vault_pair(hub)
        vault_root = pair[0] if pair else hub.obsidian_root
        vault_name = pair[1] if pair else None

        if path.is_dir():
            selected = self.select_markdown_file(path)
            if selected:
                path = selected
            else:
                url = QUrl("obsidian://open")
                query = QUrlQuery()
                if vault_name:
                    query.addQueryItem("vault", vault_name)
                elif vault_root:
                    query.addQueryItem("path", str(vault_root.resolve()))
                else:
                    query.addQueryItem("path", str(path))
                url.setQuery(query)
                QDesktopServices.openUrl(url)
                return

        if vault_root and vault_name:
            try:
                relative = path.relative_to(vault_root.resolve())
            except ValueError:
                relative = None

            if relative is not None:
                url = QUrl("obsidian://open")
                query = QUrlQuery()
                query.addQueryItem("vault", vault_name)
                query.addQueryItem("file", relative.as_posix())
                url.setQuery(query)
                QDesktopServices.openUrl(url)
                self.maybe_reveal_in_obsidian()
                return

        url = QUrl("obsidian://open")
        query = QUrlQuery()
        query.addQueryItem("path", str(path))
        url.setQuery(query)
        QDesktopServices.openUrl(url)
        self.maybe_reveal_in_obsidian()

    def maybe_reveal_in_obsidian(self) -> None:
        if not self.config.obsidian_reveal_active:
            return
        pair = self.obsidian_vault_pair(self.current_hub())
        vault_name = pair[1] if pair else None
        if not vault_name:
            return
        command_id = self.config.obsidian_reveal_command.strip()
        if not command_id:
            return
        delay_ms = max(self.config.obsidian_reveal_delay_ms, 0)

        def trigger() -> None:
            url = QUrl("obsidian://advanced-uri")
            query = QUrlQuery()
            query.addQueryItem("vault", vault_name)
            query.addQueryItem("commandid", command_id)
            url.setQuery(query)
            QDesktopServices.openUrl(url)

        if delay_ms:
            QTimer.singleShot(delay_ms, trigger)
        else:
            trigger()

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
