from __future__ import annotations

from functools import partial
from pathlib import Path

from PyQt6.QtCore import QThread

from ..filename_index import FileEntry, FilenameIndexWorker, TopicFileIndex
from ..location_filter import allowed_index_roots, filter_file_entries
from ..models import Topic
from ..search import fold, rank_filename_hits


MAX_FILENAME_HITS = 50


class FilenameSearchMixin:
    """The filename index for MainWindow: walking topic folders off the UI thread
    and answering filename searches and the matching-files panel."""

    def start_filename_indexing(self, reuse: bool = True) -> None:
        """Index the filenames in every topic's folders, off the UI thread.

        With ``reuse``, a topic whose folders are the ones it was indexed
        from keeps that index; changes inside those folders then show up on
        the next full reindex (F5).
        """
        self._filename_index_generation += 1
        self.cancel_filename_indexing()
        paths_by_key = {
            key: paths
            for key, topic in self.topics_by_key.items()
            if (paths := self.topic_index_paths(topic))
        }
        if not paths_by_key:
            # Nothing left to walk: drop what an earlier walk found.
            self._set_filename_indexes({}, {})
            return
        reusable = {}
        if reuse:
            for key, paths in paths_by_key.items():
                kept = self._filename_indexes.get(key)
                if kept is not None and kept[0] == tuple(paths):
                    reusable[key] = kept[1]
        if len(reusable) == len(paths_by_key):
            self._set_filename_indexes(paths_by_key, reusable)
            return

        thread = QThread(self)
        worker = FilenameIndexWorker(
            self._filename_index_generation, paths_by_key, reusable
        )
        self._indexing_paths = paths_by_key
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

    def _set_filename_indexes(
        self, paths_by_key: dict[str, list[Path]], indexes: dict[str, TopicFileIndex]
    ) -> None:
        self._filename_indexes = {
            key: (tuple(paths_by_key[key]), index)
            for key, index in indexes.items()
            if key in paths_by_key
        }
        self.filename_index_by_key = {key: index.blob for key, index in indexes.items()}
        self.filename_entries_by_key = {key: index.entries for key, index in indexes.items()}
        self._filtered_filename_blobs = {}

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
        self._set_filename_indexes(self._indexing_paths, indexes)
        self.statusBar().showMessage("Filename index ready", 3000)
        if self.search_input.text().strip() and self.filename_search_toggle.isChecked():
            self.apply_filter()

    def get_topic_filename_blob(self, topic_key: str) -> str:
        if self.location_filter.is_all():
            return self.filename_index_by_key.get(topic_key, "")
        blob = self._filtered_filename_blobs.get(topic_key)
        if blob is None:
            entries = self.location_filtered_entries(topic_key)
            blob = "\n".join(sorted({fold(entry.name) for entry in entries}))
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
