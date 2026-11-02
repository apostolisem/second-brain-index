from __future__ import annotations

import threading
from functools import partial
from pathlib import Path

from PyQt6.QtCore import QEventLoop, QFileSystemWatcher, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QMessageBox, QProgressDialog

from ..constants import STATE_FOLDERS, display_name_from_key, is_hub_key, split_hub_key
from ..indexer import SOURCE_SPECS, ScanResult, run_scan
from ..scanner import path_signature
from ..models import ManualLink, ManualLocation, Topic


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


class RefreshMixin:
    """Rescanning for MainWindow: the file watcher, the scan thread, and attaching
    what the store keeps to the topics the scan found."""

    def _setup_file_watcher(self) -> None:
        self._fs_watcher: QFileSystemWatcher | None = None
        if not self.config.auto_refresh:
            return
        self._fs_watcher = QFileSystemWatcher(self)
        # The paths that fired since the last check, and how the last scan
        # found every watched path.
        self._changed_paths: set[str] = set()
        self._watch_snapshot: dict[str, object] = {}
        self._watch_debounce = QTimer(self)
        self._watch_debounce.setSingleShot(True)
        self._watch_debounce.setInterval(1500)
        self._watch_debounce.timeout.connect(self._on_watched_change_settled)
        self._fs_watcher.directoryChanged.connect(self._on_watched_change)
        self._fs_watcher.fileChanged.connect(self._on_watched_change)

    def _on_watched_change(self, path: str) -> None:
        self._changed_paths.add(path)
        self._watch_debounce.start()

    def _on_watched_change_settled(self) -> None:
        if self._refresh_in_progress:
            # Its scan may already have seen the change; decide once it is done.
            self._watch_debounce.start()
            return
        changed, self._changed_paths = self._changed_paths, set()
        if not any(
            path not in self._watch_snapshot
            or path_signature(Path(path)) != self._watch_snapshot[path]
            for path in changed
        ):
            # The app's own move or rename, already refreshed for: the watcher
            # only reports it once the operation's refresh has begun.
            return
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

    def refresh_index(self, interactive: bool = True, reindex_files: bool = False) -> None:
        """Rescan every root and rebuild the view.

        Blocks the caller until the scan is done, but the scan itself runs on
        a worker thread while a local event loop keeps the window painting.
        Filenames are walked again only for topics whose folders changed,
        unless ``reindex_files`` asks for all of them (F5, the Refresh button).
        """
        if self._closed:
            return
        if self._refresh_in_progress:
            self._refresh_pending = True
            self._pending_reindex = self._pending_reindex or reindex_files
            return
        self._refresh_in_progress = True
        try:
            self._run_refresh(interactive, reindex_files)
        finally:
            self._refresh_in_progress = False
        if self._refresh_pending:
            self._refresh_pending = False
            reindex, self._pending_reindex = self._pending_reindex, False
            self.refresh_index(interactive=False, reindex_files=reindex)

    def is_configured(self) -> bool:
        """Whether there is anything at all to find topics in."""
        root_hub = self.config.root_hub
        return bool(
            root_hub.para_roots
            or root_hub.obsidian_root
            or self.config.other_folders
            or any(spec.enabled(root_hub) for spec in SOURCE_SPECS)
        )

    def _run_refresh(self, interactive: bool, reindex_files: bool = False) -> None:
        # Nothing configured is not an error: the sidebar says what to set up,
        # and the scan still runs for topics made of the user's own links.
        # The store's SQLite connection belongs to this thread, so everything
        # the scan needs from it is read up front.
        manual_links = self.link_store.list_links()
        topic_tags = self.link_store.list_topic_tags()

        cancelled = threading.Event()
        loop = QEventLoop(self)
        relay = _ScanRelay(loop)
        config = self.config
        watch_paths = self._watch_candidate_paths() if self._fs_watcher else set()

        def work() -> None:
            result: ScanResult | None = None
            error: Exception | None = None
            try:
                result = run_scan(config, manual_links, cancelled.is_set, watch_paths)
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
        if self._fs_watcher is not None:
            self._watch_snapshot = result.watch_snapshot
        self.topics_by_key = result.topics_by_key
        self.hubs_by_project_key = result.hubs_by_project_key
        self.missing_roots = result.missing_roots
        self.scan_errors = list(result.errors)
        for title, message in result.errors:
            self._report_refresh_error(title, message, interactive)
        manual_locations = self.link_store.list_locations()
        self.add_standalone_topics(manual_links, manual_locations)
        self.rebuild_normalized_topic_keys()
        self.add_topic_tags(topic_tags)
        self.add_pinned_ranks()
        self.add_manual_locations(manual_locations)
        # Also the way into the orphaned-data view, so it shows while there is any.
        self.sources_button.setVisible(
            self._any_sources_enabled() or bool(self.find_orphans())
        )
        if self.focused_hub_key and self.focused_hub_key not in self.hubs_by_project_key:
            self.focused_hub_key = None
        location_notice = self._revalidate_location_filter()
        self.apply_filter()
        self.update_status_summary()
        self.start_filename_indexing(reuse=not reindex_files)
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

    def add_topic_tags(self, topic_tags: dict[str, list[str]]) -> None:
        for topic_key, tags in topic_tags.items():
            topic = self.topics_by_key.get(topic_key)
            if not topic:
                continue
            topic.tags = sorted(tags, key=lambda value: (value.casefold(), value))

    def add_standalone_topics(
        self, manual_links: list[ManualLink], manual_locations: list[ManualLocation]
    ) -> None:
        """Bring in the topics that have no folder or list entry to be found by.

        A links-only topic that later got a folder or list entry keeps its
        entry here, following the section that place gives it, so that it
        falls back to its own links and folders if that place goes again.
        """
        with_locations = {location.topic_name for location in manual_locations}
        for key, state in self.link_store.list_standalone_topics().items():
            topic = self.topics_by_key.get(key)
            if topic is not None:
                # The folder or list entry decides the section while it exists.
                if topic.display_state in STATE_FOLDERS and topic.display_state != state:
                    self.link_store.set_standalone_topic(key, topic.display_state)
                continue
            if key not in with_locations:
                # None of its own places are left either: there is nothing to
                # show it by. The entry stays for the folder coming back, and
                # the orphaned-data view lists what it still carries.
                continue
            hub_key = split_hub_key(key)[0] if is_hub_key(key) else None
            if hub_key is not None and hub_key not in self.hubs_by_project_key:
                # Its sub-hub is gone for now (a drive not mounted, say);
                # keep the entry for when it is back.
                continue
            links = [link for link in manual_links if link.topic_name == key]
            links.sort(key=lambda link: link.link_name.casefold())
            self.topics_by_key[key] = Topic(
                name=display_name_from_key(key),
                manual_links=links,
                states={state},
                display_state=state,
                hub_key=hub_key,
                standalone=True,
            )

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
