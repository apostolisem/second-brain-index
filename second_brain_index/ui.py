from __future__ import annotations

import json
import re
import shutil
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from PyQt6.QtCore import (
    QFileSystemWatcher,
    QSettings,
    Qt,
    QThread,
    QTimer,
    QUrl,
    QUrlQuery,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QComboBox,
    QCompleter,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QInputDialog,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSizePolicy,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QMenu,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from .aggregation import build_topic_index, choose_display_state
from .config import AppConfig, DEFAULT_TOPIC_LIST_NAMES, HubConfig, MARKER_FILENAME
from .constants import (
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
from .db import ManualLinkStore
from .filename_index import FilenameIndexWorker
from .fsops import MergeReport, merge_tree, paths_are_same_entry, safe_rename
from .naming import normalize_key, validate_portable_name
from .models import ManualLink, Topic, TopicLocation
from .resources import app_icon
from .search import (
    FIELD_BOOKMARKS,
    FIELD_FILENAMES,
    FIELD_NAME,
    FIELD_TAGS,
    SearchToken,
    format_match_fields,
    match_topic,
    parse_search_query,
)
from .onenote import OneNoteTopicsError, infer_states_from_paths as infer_onenote_states
from .onenote import load_onenote_topics
from .outlook import OutlookTopicsError, infer_states_from_paths as infer_outlook_states
from .outlook import load_outlook_topics
from .plm import (
    PLMTopicsError,
    infer_states_from_paths as infer_plm_states,
    load_plm_topics,
)
from .scanner import scan_other_folders, scan_para_roots
from .subhub import discover_subhubs
from .utils import infer_link_type

HUB_ROOT_EXPANSION_PREFIX = "__hubroot__::"


def topic_sort_key(item: tuple[str, Topic]) -> tuple[bool, int, str, str]:
    topic = item[1]
    return (
        topic.pinned_rank is None,
        topic.pinned_rank or 0,
        topic.name.casefold(),
        topic.name,
    )


@dataclass(frozen=True)
class SourceSpec:
    name: str
    attr: str
    loader: Callable[[Path], dict[str, list[str]]]
    error_class: type[Exception]
    infer_states: Callable[[list[str], str], set[str]]

    def enabled(self, hub: HubConfig) -> bool:
        return getattr(hub, f"{self.attr}_enabled")

    def topics_path(self, hub: HubConfig) -> Path:
        return getattr(hub, f"{self.attr}_topics_path")

    def paths_of(self, topic: Topic) -> list[str]:
        return getattr(topic, f"{self.attr}_paths")

    def set_paths(self, topic: Topic, paths: list[str]) -> None:
        setattr(topic, f"{self.attr}_paths", paths)


SOURCE_SPECS = (
    SourceSpec(
        "OneNote",
        "onenote",
        load_onenote_topics,
        OneNoteTopicsError,
        infer_onenote_states,
    ),
    SourceSpec(
        "Outlook",
        "outlook",
        load_outlook_topics,
        OutlookTopicsError,
        infer_outlook_states,
    ),
    SourceSpec(
        "PLM",
        "plm",
        load_plm_topics,
        PLMTopicsError,
        infer_plm_states,
    ),
)


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


def merge_conflict_dirname(old_name: str) -> str:
    return f"Merged from {old_name}"


def calculate_table_display_height(
    table: QTableWidget, row_indexes: list[int], max_visible_rows: int
) -> int:
    header = table.horizontalHeader()
    header_height = header.height() if header.isVisible() else 0
    frame_height = table.frameWidth() * 2
    default_row_height = table.verticalHeader().defaultSectionSize()
    visible_rows = row_indexes[:max_visible_rows]
    row_count = max(1, min(len(row_indexes), max_visible_rows))
    rows_height = sum(
        table.rowHeight(row) if table.rowHeight(row) > 0 else default_row_height
        for row in visible_rows
    )
    scrollbar_height = 0
    horizontal_scrollbar = table.horizontalScrollBar()
    if horizontal_scrollbar.isVisible():
        scrollbar_height = horizontal_scrollbar.height()
    if len(visible_rows) < row_count:
        rows_height += default_row_height * (row_count - len(visible_rows))
    return header_height + frame_height + rows_height + scrollbar_height


def clean_manual_action_text(action: str) -> str:
    cleaned = action.strip()
    while cleaned.startswith("-"):
        cleaned = cleaned[1:].strip()
    return cleaned


class LinkGroup(QGroupBox):
    def __init__(self, title: str, open_callback) -> None:
        super().__init__(title)
        self._open_callback = open_callback
        self._layout = QVBoxLayout(self)

    def set_links(self, links: list[str | tuple[str, str]]) -> None:
        self._clear_layout()
        if not links:
            self._layout.addWidget(QLabel("None"))
            return
        for link in links:
            if isinstance(link, tuple):
                label, target = link
            else:
                label, target = link, link
            display_label = label.replace("&", "&&")
            button = QPushButton(display_label)
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet("text-align: left;")
            button.clicked.connect(
                lambda _checked=False, value=target: self._open_callback(value)
            )
            self._layout.addWidget(button)

    def _clear_layout(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ManualLinkDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        title: str,
        name: str = "",
        url: str = "",
        name_suggestions: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        self.name_edit = QComboBox()
        self.name_edit.setEditable(True)
        self.name_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if name_suggestions:
            self.name_edit.addItems(name_suggestions)
            completer = QCompleter(name_suggestions, self)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            self.name_edit.setCompleter(completer)
        self.name_edit.setCurrentText(name)

        self.url_edit = QLineEdit(url)
        self.name_edit.setMinimumWidth(420)
        self.url_edit.setMinimumWidth(420)
        form_layout.addRow("Name", self.name_edit)
        form_layout.addRow("URL", self.url_edit)
        layout.addLayout(form_layout)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(520)
        self.resize(560, self.sizeHint().height())

    def accept(self) -> None:
        if not self.name_edit.currentText().strip() or not self.url_edit.text().strip():
            QMessageBox.warning(self, "Missing data", "Name and URL are required.")
            return
        super().accept()

    def values(self) -> tuple[str, str]:
        return self.name_edit.currentText().strip(), self.url_edit.text().strip()


class TopicPickerDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        title: str,
        entries: list[tuple[str, str]],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Type to filter topics…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_input)

        self.topic_list = QListWidget()
        self.topic_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        for key, label in entries:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.topic_list.addItem(item)
        self.topic_list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.topic_list.itemSelectionChanged.connect(self._update_ok_state)
        layout.addWidget(self.topic_list)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        for sequence in ("Return", "Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self.search_input)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self._accept_current)
        down_shortcut = QShortcut(QKeySequence("Down"), self.search_input)
        down_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        down_shortcut.activated.connect(lambda: self._step_selection(1))
        up_shortcut = QShortcut(QKeySequence("Up"), self.search_input)
        up_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        up_shortcut.activated.connect(lambda: self._step_selection(-1))

        self.setMinimumWidth(480)
        self.resize(520, 420)
        self._select_first_visible()
        self.search_input.setFocus()

    def _visible_rows(self) -> list[int]:
        return [
            row
            for row in range(self.topic_list.count())
            if not self.topic_list.item(row).isHidden()
        ]

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for row in range(self.topic_list.count()):
            item = self.topic_list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().casefold())
        self._select_first_visible()

    def _select_first_visible(self) -> None:
        visible = self._visible_rows()
        if visible:
            self.topic_list.setCurrentRow(visible[0])
        else:
            self.topic_list.setCurrentRow(-1)
        self._update_ok_state()

    def _step_selection(self, delta: int) -> None:
        visible = self._visible_rows()
        if not visible:
            return
        current = self.topic_list.currentRow()
        if current in visible:
            index = visible.index(current) + delta
            index = max(0, min(index, len(visible) - 1))
        else:
            index = 0
        self.topic_list.setCurrentRow(visible[index])

    def _update_ok_state(self) -> None:
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setEnabled(self.selected_key() is not None)

    def _accept_current(self) -> None:
        if self.selected_key() is not None:
            self.accept()

    def accept(self) -> None:
        if self.selected_key() is None:
            return
        super().accept()

    def selected_key(self) -> str | None:
        item = self.topic_list.currentItem()
        if item is None or item.isHidden():
            return None
        return item.data(Qt.ItemDataRole.UserRole)


class OperationConfirmDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        title: str,
        summary: str,
        details: list[str] | None = None,
        manual_actions: list[str] | None = None,
        merge_conflicts: list[str] | None = None,
        accept_label: str = "Continue",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._manual_items: list[QListWidgetItem] = []

        layout = QVBoxLayout(self)
        summary_label = QLabel(summary)
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        self._add_manual_checklist(layout, manual_actions or [])

        detail_lines = list(details or [])
        if merge_conflicts:
            detail_lines.append("")
            detail_lines.append("Existing destination folders will be merged.")
            detail_lines.append(
                "Conflicting files are kept in a 'Merged from …' subfolder."
            )
            detail_lines.append("")
            detail_lines.append("Existing folders:")
            detail_lines.extend(f"  {path}" for path in merge_conflicts)

        if detail_lines:
            detail_box = QTextEdit()
            detail_box.setReadOnly(True)
            detail_box.setPlainText("\n".join(detail_lines))
            detail_box.setMinimumHeight(160)
            layout.addWidget(detail_box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setText(accept_label)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.setMinimumWidth(620)
        self.resize(700, self.sizeHint().height())
        self._update_ok_state()

    def _add_manual_checklist(
        self, layout: QVBoxLayout, manual_actions: list[str]
    ) -> None:
        if not manual_actions:
            return
        label = QLabel("Complete these manual steps before continuing:")
        label.setWordWrap(True)
        layout.addWidget(label)

        checklist = QListWidget()
        checklist.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for action in manual_actions:
            item = QListWidgetItem(clean_manual_action_text(action))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            checklist.addItem(item)
            self._manual_items.append(item)
        checklist.itemChanged.connect(lambda _item: self._update_ok_state())
        layout.addWidget(checklist)

    def _manual_complete(self) -> bool:
        return all(
            item.checkState() == Qt.CheckState.Checked for item in self._manual_items
        )

    def _update_ok_state(self) -> None:
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setEnabled(self._manual_complete())


class RenameTopicDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        old_name: str,
        preview_callback: Callable[[str, str | None], RenamePreview],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rename topic")
        self._old_name = old_name
        self._preview_callback = preview_callback
        self._preview: RenamePreview | None = None
        self._manual_items: list[QListWidgetItem] = []
        self._refreshing = False

        layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        self.name_edit = QLineEdit(old_name)
        self.name_edit.setMinimumWidth(420)
        form_layout.addRow("New topic name", self.name_edit)

        self.section_combo = QComboBox()
        self.section_label = QLabel("Land merged topic in")
        form_layout.addRow(self.section_label, self.section_combo)
        self.section_label.setVisible(False)
        self.section_combo.setVisible(False)
        layout.addLayout(form_layout)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #b00020;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        self.merge_label = QLabel("")
        self.merge_label.setStyleSheet("color: #8a5300; font-weight: bold;")
        self.merge_label.setWordWrap(True)
        self.merge_label.setVisible(False)
        layout.addWidget(self.merge_label)

        self.merge_confirm = QCheckBox("")
        self.merge_confirm.setVisible(False)
        self.merge_confirm.toggled.connect(lambda _checked: self._update_ok_state())
        layout.addWidget(self.merge_confirm)

        self.manual_label = QLabel("Complete these manual steps before renaming:")
        self.manual_label.setWordWrap(True)
        self.manual_list = QListWidget()
        self.manual_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.manual_list.itemChanged.connect(lambda _item: self._update_ok_state())
        layout.addWidget(self.manual_label)
        layout.addWidget(self.manual_list)

        self.details_box = QTextEdit()
        self.details_box.setReadOnly(True)
        self.details_box.setMinimumHeight(180)
        layout.addWidget(self.details_box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setText("Rename")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.name_edit.textChanged.connect(self._refresh_preview)
        self.section_combo.currentTextChanged.connect(
            lambda _text: self._refresh_preview()
        )
        self.setMinimumWidth(640)
        self.resize(720, self.sizeHint().height())
        self._refresh_preview()
        self.name_edit.selectAll()
        self.name_edit.setFocus()

    def selected_section(self) -> str | None:
        # isHidden reflects the explicit setVisible call; isVisible would also
        # report False while the dialog itself has not been shown yet.
        if self.section_combo.isHidden():
            return None
        # KEEP_SECTIONS is returned verbatim rather than as None: None means
        # "no choice made yet, use the default", which is a different answer.
        return self.section_combo.currentText() or None

    def _refresh_preview(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._preview = self._preview_callback(
                self.name_edit.text().strip(), self.selected_section()
            )
            self.error_label.setText(self._preview.error or "")
            self.error_label.setVisible(bool(self._preview.error))
            self._populate_section_choices(self._preview)
            self._populate_merge_prompt(self._preview)
            self._populate_manual_items(self._preview.manual_actions)
            self.details_box.setPlainText("\n".join(self._preview.details))
            ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
            if ok_button:
                ok_button.setText("Merge" if self._preview.is_merge else "Rename")
        finally:
            self._refreshing = False
        self._update_ok_state()

    def _populate_section_choices(self, preview: RenamePreview) -> None:
        choices = list(preview.section_choices)
        visible = bool(choices)
        self.section_label.setVisible(visible)
        self.section_combo.setVisible(visible)
        if not visible:
            self.section_combo.clear()
            return
        current = preview.target_state or KEEP_SECTIONS
        if [self.section_combo.itemText(i) for i in range(self.section_combo.count())] != choices:
            self.section_combo.clear()
            self.section_combo.addItems(choices)
        index = self.section_combo.findText(current)
        if index >= 0:
            self.section_combo.setCurrentIndex(index)

    def _populate_merge_prompt(self, preview: RenamePreview) -> None:
        if not preview.is_merge:
            self.merge_label.setVisible(False)
            self.merge_confirm.setVisible(False)
            self.merge_confirm.setChecked(False)
            return
        conflicts = preview.conflicts
        summary = f"'{preview.new_name}' already exists — this will merge the two topics."
        if conflicts:
            summary += (
                f" {len(conflicts)} conflicting file(s) will be kept in a"
                f" 'Merged from {self._old_name}' subfolder."
            )
        self.merge_label.setText(summary)
        self.merge_label.setVisible(True)
        self.merge_confirm.setText(
            f"Merge '{self._old_name}' into '{preview.new_name}'"
        )
        self.merge_confirm.setVisible(True)

    def _populate_manual_items(self, actions: list[str]) -> None:
        self._manual_items = []
        self.manual_list.clear()
        self.manual_label.setVisible(bool(actions))
        self.manual_list.setVisible(bool(actions))
        for action in actions:
            item = QListWidgetItem(clean_manual_action_text(action))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.manual_list.addItem(item)
            self._manual_items.append(item)

    def _manual_complete(self) -> bool:
        return all(
            item.checkState() == Qt.CheckState.Checked for item in self._manual_items
        )

    def _merge_confirmed(self) -> bool:
        if self._preview is None or not self._preview.is_merge:
            return True
        return self.merge_confirm.isChecked()

    def _update_ok_state(self) -> None:
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setEnabled(
                self._preview is not None
                and self._preview.error is None
                and self._manual_complete()
                and self._merge_confirmed()
            )

    def preview(self) -> RenamePreview | None:
        return self._preview


class InitializeSubHubDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        destination_root: Path,
        validate_callback: Callable[[str], str | None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Init Sub-Hub")
        self.destination_root = destination_root
        self._validate_callback = validate_callback

        layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setMinimumWidth(420)
        form_layout.addRow("Project folder name", self.name_edit)
        layout.addLayout(form_layout)

        self.destination_label = QLabel("")
        self.destination_label.setWordWrap(True)
        layout.addWidget(self.destination_label)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #b00020;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setText("Initialize")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.name_edit.textChanged.connect(lambda _text: self._refresh())
        self.setMinimumWidth(580)
        self._refresh()
        self.name_edit.setFocus()

    def _refresh(self) -> None:
        name = self.name()
        destination = self.destination_path()
        self.destination_label.setText(f"Destination: {destination}")
        error = self._validate_callback(name)
        self.error_label.setText(error or "")
        self.error_label.setVisible(bool(error))
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setEnabled(error is None)

    def name(self) -> str:
        return self.name_edit.text().strip()

    def destination_path(self) -> Path:
        return self.destination_root / STATE_FOLDERS["Projects"] / self.name()


class TopicTagDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        title: str,
        tag_name: str = "",
        tag_suggestions: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        self.tag_edit = QComboBox()
        self.tag_edit.setEditable(True)
        self.tag_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if tag_suggestions:
            self.tag_edit.addItems(tag_suggestions)
            completer = QCompleter(tag_suggestions, self)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            self.tag_edit.setCompleter(completer)
        self.tag_edit.setCurrentText(tag_name)
        self.tag_edit.setMinimumWidth(320)
        form_layout.addRow("Tag", self.tag_edit)
        layout.addLayout(form_layout)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(420)
        self.resize(440, self.sizeHint().height())

    def accept(self) -> None:
        if not self.tag_edit.currentText().strip():
            QMessageBox.warning(self, "Missing data", "Tag name is required.")
            return
        super().accept()

    def value(self) -> str:
        return self.tag_edit.currentText().strip()


class TopicTagsPanel(QGroupBox):
    def __init__(self, on_add, on_edit, on_delete, on_filter=None) -> None:
        super().__init__("Tags")
        self._actions_column_index = 1
        self._actions_column_min_width = 96
        self._max_visible_rows = 5
        self._row_height = 24
        self._on_add = on_add
        self._on_edit = on_edit
        self._on_delete = on_delete
        self._on_filter = on_filter
        self._tags: list[str] = []

        layout = QVBoxLayout(self)
        button_row = QHBoxLayout()
        self.add_button = QPushButton("Add Tag")
        self.add_button.clicked.connect(self._on_add)
        button_row.addWidget(self.add_button)
        button_row.addStretch()
        layout.addLayout(button_row)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Tag", ""])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setStyleSheet(
            "QTableWidget::item { padding-left: 8px; padding-right: 8px; }"
        )
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            self._actions_column_index, QHeaderView.ResizeMode.Fixed
        )
        self.table.setColumnWidth(
            self._actions_column_index, self._actions_column_min_width
        )
        self.table.verticalHeader().setDefaultSectionSize(self._row_height)
        self.table.verticalHeader().setMinimumSectionSize(self._row_height)
        self.table.cellDoubleClicked.connect(self._handle_filter)
        layout.addWidget(self.table)

    def set_enabled(self, enabled: bool) -> None:
        self.add_button.setEnabled(enabled)
        self.table.setEnabled(enabled)

    def _handle_filter(self, row: int, column: int) -> None:
        if column != 0 or self._on_filter is None:
            return
        if 0 <= row < len(self._tags):
            self._on_filter(self._tags[row])

    def set_tags(self, tags: list[str]) -> None:
        self._tags = tags
        self.table.setRowCount(len(tags))
        self.table.setColumnWidth(
            self._actions_column_index, self._actions_column_min_width
        )
        for row, tag_name in enumerate(tags):
            self.table.removeCellWidget(row, self._actions_column_index)
            tag_item = QTableWidgetItem(tag_name)
            if self._on_filter is not None:
                tag_item.setToolTip("Double-click to filter by this tag")
            self.table.setItem(row, 0, tag_item)

            edit_button = QPushButton()
            edit_button.setIcon(
                self.style().standardIcon(
                    QStyle.StandardPixmap.SP_FileDialogDetailedView
                )
            )
            edit_button.setToolTip("Edit")
            edit_button.clicked.connect(
                lambda _checked=False, value=tag_name: self._on_edit(value)
            )
            delete_button = QPushButton()
            delete_button.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
            )
            delete_button.setToolTip("Delete")
            delete_button.clicked.connect(
                lambda _checked=False, value=tag_name: self._on_delete(value)
            )

            action_container = QWidget()
            action_layout = QHBoxLayout(action_container)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.addWidget(edit_button)
            action_layout.addWidget(delete_button)
            self.table.setCellWidget(row, self._actions_column_index, action_container)
            required_width = action_container.sizeHint().width()
            if required_width > self.table.columnWidth(self._actions_column_index):
                self.table.setColumnWidth(self._actions_column_index, required_width)
            self._row_height = max(self._row_height, action_container.sizeHint().height())
        self._apply_uniform_row_height()
        self._update_table_height()

    def _update_table_height(self) -> None:
        row_indexes = list(range(self.table.rowCount()))
        self.table.setFixedHeight(
            calculate_table_display_height(
                self.table, row_indexes, self._max_visible_rows
            )
        )

    def _apply_uniform_row_height(self) -> None:
        self.table.verticalHeader().setDefaultSectionSize(self._row_height)
        self.table.verticalHeader().setMinimumSectionSize(self._row_height)
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, self._row_height)


class ManualLinksPanel(QGroupBox):
    def __init__(
        self, on_add, on_edit, on_delete, on_open, on_move=None, on_delete_many=None
    ) -> None:
        super().__init__("Bookmarks")
        self._name_column_index = 0
        self._url_column_index = 1
        self._type_column_index = 2
        self._actions_column_index = 3
        self._actions_column_min_width = 96
        self._max_visible_rows = 10
        self._row_height = 24
        self._on_add = on_add
        self._on_edit = on_edit
        self._on_delete = on_delete
        self._on_open = on_open
        self._on_move = on_move
        self._on_delete_many = on_delete_many
        self._links: list[ManualLink] = []
        self._active_filter = ""

        layout = QVBoxLayout(self)
        button_row = QHBoxLayout()
        self.add_button = QPushButton("Add Bookmark")
        self.add_button.clicked.connect(self._on_add)
        button_row.addWidget(self.add_button)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search bookmarks")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(260)
        self.search_input.textChanged.connect(self._on_filter_text_changed)
        button_row.addWidget(self.search_input)
        button_row.addStretch()
        layout.addLayout(button_row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "URL", "Type", ""])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setStyleSheet("QTableWidget::item { padding-left: 8px; padding-right: 8px; }")
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(
            self._name_column_index, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            self._url_column_index, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            self._type_column_index, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            self._actions_column_index, QHeaderView.ResizeMode.Fixed
        )
        self.table.setColumnHidden(self._url_column_index, True)
        self.table.setColumnWidth(self._actions_column_index, self._actions_column_min_width)
        self.table.verticalHeader().setDefaultSectionSize(self._row_height)
        self.table.verticalHeader().setMinimumSectionSize(self._row_height)
        self.table.cellDoubleClicked.connect(self._handle_open)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        for sequence in ("Return", "Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self._open_current_row)
        layout.addWidget(self.table)

    def set_enabled(self, enabled: bool) -> None:
        self.add_button.setEnabled(enabled)
        self.search_input.setEnabled(enabled)
        self.table.setEnabled(enabled)

    def set_links(self, links: list[ManualLink]) -> None:
        self._links = links
        self.table.setRowCount(len(links))
        self.table.setColumnWidth(self._actions_column_index, self._actions_column_min_width)
        for row, link in enumerate(links):
            self.table.removeCellWidget(row, self._actions_column_index)
            self.table.setItem(row, self._name_column_index, QTableWidgetItem(link.link_name))
            self.table.setItem(row, self._url_column_index, QTableWidgetItem(link.url))
            self.table.setItem(row, self._type_column_index, QTableWidgetItem(link.link_type))

            edit_button = QPushButton()
            edit_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
            edit_button.setToolTip("Edit")
            edit_button.clicked.connect(lambda _checked=False, link_item=link: self._on_edit(link_item))
            delete_button = QPushButton()
            delete_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
            delete_button.setToolTip("Delete")
            delete_button.clicked.connect(
                lambda _checked=False, link_item=link: self._on_delete(link_item)
            )

            action_container = QWidget()
            action_layout = QHBoxLayout(action_container)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.addWidget(edit_button)
            action_layout.addWidget(delete_button)
            self.table.setCellWidget(row, self._actions_column_index, action_container)
            required_width = action_container.sizeHint().width()
            if required_width > self.table.columnWidth(self._actions_column_index):
                self.table.setColumnWidth(self._actions_column_index, required_width)
            self._row_height = max(self._row_height, action_container.sizeHint().height())
        self._apply_uniform_row_height()
        self._apply_filter()

    def _on_filter_text_changed(self, text: str) -> None:
        self._active_filter = text.strip().casefold()
        self._apply_filter()

    def _apply_filter(self) -> None:
        if not self._active_filter:
            for row in range(len(self._links)):
                self.table.setRowHidden(row, False)
            self._update_table_height()
            return
        for row, link in enumerate(self._links):
            name_match = self._active_filter in link.link_name.casefold()
            url_match = self._active_filter in link.url.casefold()
            self.table.setRowHidden(row, not (name_match or url_match))
        self._update_table_height()

    def selected_links(self) -> list[ManualLink]:
        rows = sorted(
            {
                index.row()
                for index in self.table.selectionModel().selectedRows()
                if not self.table.isRowHidden(index.row())
            }
        )
        return [self._links[row] for row in rows if 0 <= row < len(self._links)]

    def _show_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        selected_rows = {
            index.row() for index in self.table.selectionModel().selectedRows()
        }
        if row not in selected_rows:
            self.table.selectRow(row)
        links = self.selected_links()
        if not links:
            return
        menu = self._build_links_context_menu(links)
        if not menu.isEmpty():
            menu.exec(self.table.viewport().mapToGlobal(pos))

    def _build_links_context_menu(self, links: list[ManualLink]) -> QMenu:
        menu = QMenu(self.table)
        if len(links) == 1:
            link = links[0]
            open_action = menu.addAction("Open")
            open_action.triggered.connect(
                lambda _checked=False, url=link.url: self._on_open(url)
            )
            edit_action = menu.addAction("Edit")
            edit_action.triggered.connect(
                lambda _checked=False, link_item=link: self._on_edit(link_item)
            )
            move_label = "Move to topic…"
            delete_label = "Delete"
        else:
            move_label = f"Move {len(links)} bookmarks to topic…"
            delete_label = f"Delete {len(links)} bookmarks…"
        if self._on_move is not None:
            move_action = menu.addAction(move_label)
            move_action.triggered.connect(
                lambda _checked=False, items=list(links): self._on_move(items)
            )
        if len(links) == 1:
            delete_action = menu.addAction(delete_label)
            delete_action.triggered.connect(
                lambda _checked=False, link_item=links[0]: self._on_delete(link_item)
            )
        elif self._on_delete_many is not None:
            delete_action = menu.addAction(delete_label)
            delete_action.triggered.connect(
                lambda _checked=False, items=list(links): self._on_delete_many(items)
            )
        return menu

    def _handle_open(self, row: int, _column: int) -> None:
        if 0 <= row < len(self._links):
            self._on_open(self._links[row].url)

    def _open_current_row(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._links) and not self.table.isRowHidden(row):
            self._on_open(self._links[row].url)

    def _update_table_height(self) -> None:
        visible_rows = [
            row for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)
        ]
        self.table.setFixedHeight(
            calculate_table_display_height(
                self.table, visible_rows, self._max_visible_rows
            )
        )

    def _apply_uniform_row_height(self) -> None:
        self.table.verticalHeader().setDefaultSectionSize(self._row_height)
        self.table.verticalHeader().setMinimumSectionSize(self._row_height)
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, self._row_height)


class MainWindow(QMainWindow):
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
        self.match_fields_by_key: dict[str, set[str]] = {}
        self._filename_index_generation = 0
        self._filename_index_worker: FilenameIndexWorker | None = None
        self._filename_index_threads: list[QThread] = []
        self._section_expanded: dict[str, bool] = {}
        self._rebuilding_tree = False
        self._group_others_by_parent = False
        self.focused_hub_key: str | None = None
        self._pre_focus_topic_key: str | None = None

        self.setWindowTitle("Second Brain Hub")
        self.setWindowIcon(app_icon())
        self._build_ui()
        restored_geometry = self._restore_settings()
        self.refresh_index()
        if not restored_geometry:
            self.center_on_screen()

    def _build_ui(self) -> None:
        central = QWidget()
        outer_layout = QVBoxLayout(central)

        self.resize(1200, 640)
        self.setMinimumSize(960, 640)
        self._configure_tooltips()

        top_bar = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Search topics, tags, bookmark names/titles, URLs"
        )
        self.search_input.setClearButtonEnabled(True)
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(250)
        self._search_debounce.timeout.connect(self.apply_filter)
        self.search_input.textChanged.connect(self._schedule_filter)
        self.filename_search_toggle = QCheckBox("Include filenames")
        self.filename_search_toggle.setChecked(True)
        self.filename_search_toggle.setToolTip("Search inside filenames under each topic folder")
        self.filename_search_toggle.toggled.connect(self.apply_filter)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(
            lambda _checked=False: self.refresh_index()
        )
        self.sources_button = QToolButton()
        self.sources_button.setText("Sources")
        self.sources_button.setToolTip(
            "Open the JSON topic lists backing the managed sources"
        )
        self.sources_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sources_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.sources_menu = QMenu(self.sources_button)
        self.sources_menu.setToolTipsVisible(True)
        self.sources_menu.aboutToShow.connect(self.populate_sources_menu)
        self.sources_button.setMenu(self.sources_menu)
        self.initialize_subhub_button = QPushButton("Init Sub-Hub")
        self.initialize_subhub_button.clicked.connect(self.initialize_subhub)
        self.exit_focus_button = QPushButton("Exit Focus")
        self.exit_focus_button.setVisible(False)
        self.exit_focus_button.clicked.connect(self.exit_subhub_focus)
        top_bar.addWidget(self.search_input)
        top_bar.addWidget(self.filename_search_toggle)
        top_bar.addWidget(self.refresh_button)
        top_bar.addWidget(self.sources_button)
        top_bar.addWidget(self.initialize_subhub_button)
        top_bar.addWidget(self.exit_focus_button)
        outer_layout.addLayout(top_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter = splitter
        outer_layout.addWidget(splitter)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.tree.setMinimumWidth(360)
        self.tree.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self.tree.itemSelectionChanged.connect(self.on_tree_selection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_tree_context_menu)
        self.tree.itemExpanded.connect(
            lambda item: self.on_section_expansion_changed(item, True)
        )
        self.tree.itemCollapsed.connect(
            lambda item: self.on_section_expansion_changed(item, False)
        )
        splitter.addWidget(self.tree)

        details_container = QWidget()
        details_layout = QVBoxLayout(details_container)
        details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.topic_title = ClickableLabel("Select a topic")
        self.topic_title.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.topic_title.setCursor(Qt.CursorShape.PointingHandCursor)
        self.topic_title.setToolTip("Click to copy topic title")
        self.topic_title.clicked.connect(self.copy_topic_title_to_clipboard)
        self.move_button = QPushButton("Move")
        self.move_button.setEnabled(False)
        self.move_button.clicked.connect(self.move_selected_topics)
        self.rename_button = QPushButton("Rename")
        self.rename_button.setEnabled(False)
        self.rename_button.clicked.connect(self.rename_topic)
        self.pin_button = QPushButton("Pin")
        self.pin_button.setEnabled(False)
        self.pin_button.clicked.connect(self.toggle_pin_current_topic)
        self.focus_button = QPushButton("Focus")
        self.focus_button.setEnabled(False)
        self.focus_button.setVisible(False)
        self.focus_button.clicked.connect(self.focus_current_subhub)
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #d17c00;")
        self.warning_label.setVisible(False)

        title_row = QHBoxLayout()
        title_row.addWidget(self.topic_title)
        title_row.addStretch()
        title_row.addWidget(self.focus_button)
        title_row.addWidget(self.pin_button)
        title_row.addWidget(self.move_button)
        title_row.addWidget(self.rename_button)

        self.json_add_button = QToolButton()
        self.json_add_button.setText("Add to Source")
        self.json_add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.json_add_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.json_add_menu = QMenu(self.json_add_button)
        self.json_add_button.setMenu(self.json_add_menu)
        self.json_add_button.setVisible(False)
        title_row.addWidget(self.json_add_button)
        details_layout.addLayout(title_row)
        details_layout.addWidget(self.warning_label)

        self.filesystem_group = LinkGroup("Filesystem", self.open_path)
        self.obsidian_group = LinkGroup("Obsidian", self.open_obsidian_path)
        # Always created: sub-hubs may enable a tool the root hub has disabled.
        self.onenote_group: LinkGroup | None = LinkGroup(
            "OneNote", self.open_onenote_link
        )
        self.onenote_group.setVisible(False)
        self.outlook_group: LinkGroup | None = LinkGroup(
            "Outlook", self.open_outlook_link
        )
        self.outlook_group.setVisible(False)
        self.plm_group: LinkGroup | None = LinkGroup(
            "PLM", self.open_plm_link
        )
        self.plm_group.setVisible(False)
        self.tags_panel = TopicTagsPanel(
            on_add=self.add_topic_tag,
            on_edit=self.edit_topic_tag,
            on_delete=self.delete_topic_tag,
            on_filter=self.filter_by_tag,
        )
        self.manual_links_panel = ManualLinksPanel(
            on_add=self.add_manual_link,
            on_edit=self.edit_manual_link,
            on_delete=self.delete_manual_link,
            on_open=self.open_link,
            on_move=self.move_manual_links,
            on_delete_many=self.delete_manual_links,
        )

        details_layout.addWidget(self.filesystem_group)
        details_layout.addWidget(self.obsidian_group)
        if self.onenote_group:
            details_layout.addWidget(self.onenote_group)
        if self.outlook_group:
            details_layout.addWidget(self.outlook_group)
        if self.plm_group:
            details_layout.addWidget(self.plm_group)
        details_layout.addWidget(self.manual_links_panel)
        details_layout.addWidget(self.tags_panel)
        details_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(details_container)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 840])

        self.setCentralWidget(central)

        self.undo_button = QPushButton("Undo merge")
        self.undo_button.setVisible(False)
        self.undo_button.clicked.connect(self.undo_last_operation)
        self.statusBar().addPermanentWidget(self.undo_button)

        self.status_summary_label = QLabel("")
        self.statusBar().addPermanentWidget(self.status_summary_label)

        self._setup_shortcuts()
        self._setup_file_watcher()

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

    def focus_search(self) -> None:
        self.search_input.setFocus()
        self.search_input.selectAll()

    def focus_first_result(self) -> None:
        if self._search_debounce.isActive():
            self._search_debounce.stop()
            self.apply_filter()
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            if item.data(0, Qt.ItemDataRole.UserRole):
                self.tree.setCurrentItem(item)
                self.tree.setFocus()
                return
            iterator += 1

    def _schedule_filter(self) -> None:
        self._search_debounce.start()

    def _configure_tooltips(self) -> None:
        base_font = self.font()
        tooltip_font = QFont(base_font)
        tooltip_font.setPointSize(max(base_font.pointSize() - 4, 7))
        QToolTip.setFont(tooltip_font)
        app = QApplication.instance()
        if app is None:
            return
        tooltip_style = (
            "QToolTip {"
            " color: #555;"
            " background-color: #f0f0f0;"
            " border: 1px solid #cfcfcf;"
            " padding: 2px 4px;"
            " border-radius: 2px;"
            " }"
        )
        existing_style = app.styleSheet()
        app.setStyleSheet(
            f"{existing_style}\n{tooltip_style}" if existing_style else tooltip_style
        )

    def refresh_index(self, interactive: bool = True) -> None:
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

        filesystem_topics = scan_para_roots(self.config.root_hub.para_roots, "filesystem")
        obsidian_topics: dict[str, list] = {}
        if self.config.root_hub.obsidian_root:
            obsidian_topics = scan_para_roots([self.config.root_hub.obsidian_root], "obsidian")
        other_topics = scan_other_folders(self.config.other_folders, "filesystem")

        manual_links = self.link_store.list_links()
        topic_tags = self.link_store.list_topic_tags()
        self.topics_by_key = build_topic_index(
            filesystem_topics=filesystem_topics,
            obsidian_topics=obsidian_topics,
            other_topics=other_topics,
            manual_links=manual_links,
        )
        self.filename_index_by_key.clear()
        for spec in SOURCE_SPECS:
            self.add_source_topics(spec, manual_links, interactive)
        self.hubs_by_project_key, hub_errors = discover_subhubs(
            self.topics_by_key, self.config.root_hub
        )
        for project_key, message in hub_errors:
            self._report_refresh_error(
                f"Sub-hub '{project_key}'", message, interactive
            )
        for project_key, hub in self.hubs_by_project_key.items():
            hub_filesystem = scan_para_roots(hub.para_roots, "filesystem")
            hub_obsidian: dict[str, list] = {}
            if hub.obsidian_root:
                hub_obsidian = scan_para_roots([hub.obsidian_root], "obsidian")
            hub_topics = build_topic_index(
                filesystem_topics=hub_filesystem,
                obsidian_topics=hub_obsidian,
                other_topics={},
                manual_links=manual_links,
                key_prefix=hub_key_prefix(project_key),
                hub_key=project_key,
            )
            self.topics_by_key.update(hub_topics)
            for spec in SOURCE_SPECS:
                self.add_source_topics(
                    spec, manual_links, interactive, hub=hub, hub_key=project_key
                )
        self.rebuild_normalized_topic_keys()
        self.sources_button.setVisible(self._any_sources_enabled())
        self.add_topic_tags(topic_tags)
        self.add_pinned_ranks()
        if self.focused_hub_key and self.focused_hub_key not in self.hubs_by_project_key:
            self.focused_hub_key = None
            self.exit_focus_button.setVisible(False)
        self.export_topics(interactive)
        self.apply_filter()
        self.update_status_summary()
        self.start_filename_indexing()
        self._update_watch_paths()

    def _report_refresh_error(
        self, title: str, message: str, interactive: bool
    ) -> None:
        if interactive:
            QMessageBox.warning(self, title, message)
        else:
            self.statusBar().showMessage(f"{title}: {message}", 8000)

    def update_status_summary(self) -> None:
        topic_count = len(self.topics_by_key)
        subtopic_count = sum(
            1 for topic in self.topics_by_key.values() if topic.hub_key is not None
        )
        inconsistency_count = sum(
            1 for topic in self.topics_by_key.values() if topic.has_inconsistency
        )
        parts = [f"{topic_count} topics"]
        if self.focused_hub_key:
            topic = self.topics_by_key.get(self.focused_hub_key)
            label = topic.name if topic else self.focused_hub_key
            parts.append(f"focused on {label}")
        if subtopic_count:
            parts.append(
                f"{subtopic_count} in {len(self.hubs_by_project_key)} sub-hub(s)"
            )
        if inconsistency_count:
            parts.append(f"{inconsistency_count} inconsistencies")
        parts.append(f"refreshed {datetime.now().strftime('%H:%M')}")
        self.status_summary_label.setText(" · ".join(parts))

    def start_filename_indexing(self) -> None:
        self._filename_index_generation += 1
        self.cancel_filename_indexing()
        paths_by_key = {
            key: [location.path for location in topic.locations]
            for key, topic in self.topics_by_key.items()
            if topic.locations
        }
        if not paths_by_key:
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

    def on_filename_index_finished(self, generation: int, blobs: dict) -> None:
        if generation != self._filename_index_generation:
            return
        self.filename_index_by_key.update(blobs)
        self.statusBar().showMessage("Filename index ready", 3000)
        if self.search_input.text().strip() and self.filename_search_toggle.isChecked():
            self.apply_filter()

    def closeEvent(self, event) -> None:
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

    def export_topics(self, interactive: bool = True) -> None:
        root_export_path = (
            self.config.root_hub.onenote_topics_path.parent / "Topics_export.json"
        )
        self._write_topics_export(root_export_path, None, interactive)
        for project_key, hub in self.hubs_by_project_key.items():
            export_dir = (
                hub.marker_path.parent if hub.marker_path else hub.para_roots[0]
            )
            self._write_topics_export(
                export_dir / "Topics_export.json", project_key, interactive
            )

    def _write_topics_export(
        self, export_path: Path, hub_key: str | None, interactive: bool
    ) -> None:
        sections = []
        for state in STATE_ORDER:
            if state == OTHER_STATE:
                continue
            topics = [
                topic.name
                for topic in self.topics_by_key.values()
                if topic.display_state == state and topic.hub_key == hub_key
            ]
            topics.sort(key=lambda name: (name.casefold(), name))
            sections.append({"name": STATE_FOLDERS[state], "topics": topics})

        payload = {"sections": sections}
        try:
            export_path.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self._report_refresh_error(
                "Export failed",
                f"Failed to write topics export: {exc}",
                interactive,
            )

    def add_source_topics(
        self,
        spec: SourceSpec,
        manual_links: list[ManualLink],
        interactive: bool = True,
        hub: HubConfig | None = None,
        hub_key: str | None = None,
    ) -> None:
        hub = hub or self.config.root_hub
        if not spec.enabled(hub):
            return

        try:
            source_topics = spec.loader(spec.topics_path(hub))
        except spec.error_class as exc:
            source_label = (
                spec.name if hub_key is None else f"{spec.name} ({hub_key})"
            )
            self._report_refresh_error(source_label, str(exc), interactive)
            return

        if not source_topics:
            return

        links_by_topic: dict[str, list[ManualLink]] = defaultdict(list)
        for link in manual_links:
            links_by_topic[link.topic_name].append(link)

        for name, paths in source_topics.items():
            key = make_hub_key(hub_key, name) if hub_key else name
            source_states = spec.infer_states(paths, name)
            topic = self.topics_by_key.get(key)
            if topic:
                spec.set_paths(topic, paths)
                combined_states = topic.states | source_states
                has_inconsistency = len(combined_states) > 1
                topic.states = combined_states
                topic.has_inconsistency = has_inconsistency
                topic.display_state = choose_display_state(
                    topic.name, combined_states, has_inconsistency
                )
                continue

            states = source_states
            has_inconsistency = len(states) > 1
            display_state = choose_display_state(name, states, has_inconsistency)
            topic_links = links_by_topic.get(key, [])
            topic_links.sort(key=lambda item: item.link_name.casefold())
            new_topic = Topic(
                name=name,
                locations=[],
                manual_links=topic_links,
                states=states,
                display_state=display_state,
                has_inconsistency=has_inconsistency,
                hub_key=hub_key,
            )
            spec.set_paths(new_topic, paths)
            self.topics_by_key[key] = new_topic

    def apply_filter(self) -> None:
        query = self.search_input.text().strip()
        tokens = parse_search_query(query)
        include_filenames = self.filename_search_toggle.isChecked()

        self.match_fields_by_key = {}
        topics = list(self.topics_by_key.items())
        if tokens:
            filtered: list[tuple[str, Topic]] = []
            for key, topic in topics:
                matched_fields = self.topic_matches(
                    key, topic, tokens, include_filenames
                )
                if matched_fields is None:
                    continue
                self.match_fields_by_key[key] = matched_fields
                filtered.append((key, topic))
            topics = filtered

        main_topics = [(key, topic) for key, topic in topics if topic.hub_key is None]
        sub_topics = [(key, topic) for key, topic in topics if topic.hub_key is not None]

        if self.focused_hub_key:
            sub_topics = [
                (key, topic)
                for key, topic in sub_topics
                if topic.hub_key == self.focused_hub_key
            ]
            focused_main = [
                (key, topic)
                for key, topic in main_topics
                if key == self.focused_hub_key
            ]
            if not focused_main and (
                not tokens or sub_topics
            ) and self.focused_hub_key in self.topics_by_key:
                focused_main = [
                    (
                        self.focused_hub_key,
                        self.topics_by_key[self.focused_hub_key],
                    )
                ]
            main_topics = focused_main

        if tokens and sub_topics:
            # A matched sub-topic needs its owning project visible to host it.
            present = {key for key, _topic in main_topics}
            for _key, topic in sub_topics:
                project_key = topic.hub_key
                if project_key not in present and project_key in self.topics_by_key:
                    main_topics.append(
                        (project_key, self.topics_by_key[project_key])
                    )
                    present.add(project_key)

        topics_by_state: dict[str, list[tuple[str, Topic]]] = {
            state: [] for state in STATE_ORDER
        }
        for key, topic in main_topics:
            topics_by_state[topic.display_state].append((key, topic))

        for state in topics_by_state:
            if state == OTHER_STATE:
                topics_by_state[state].sort(
                    key=lambda item: self.other_sort_key(item[1].name)
                )
            else:
                topics_by_state[state].sort(key=topic_sort_key)

        subtopics_by_hub: dict[str, dict[str, list[tuple[str, Topic]]]] = {}
        for key, topic in sub_topics:
            by_state = subtopics_by_hub.setdefault(topic.hub_key, {})
            by_state.setdefault(topic.display_state, []).append((key, topic))
        for by_state in subtopics_by_hub.values():
            for entries in by_state.values():
                entries.sort(key=topic_sort_key)
        self._subtopics_by_hub = subtopics_by_hub

        self.rebuild_tree(topics_by_state)

    def other_sort_key(self, name: str) -> tuple[int, int, str, str]:
        stripped = name.strip()
        if stripped:
            match = re.search(r"\d+", stripped)
            if match:
                try:
                    numeric = int(match.group(0))
                    return (0, -numeric, stripped.casefold(), stripped)
                except ValueError:
                    pass
        return (1, 0, stripped.casefold(), stripped)

    def topic_matches(
        self,
        topic_key: str,
        topic: Topic,
        tokens: list[SearchToken],
        include_filenames: bool,
    ) -> set[str] | None:
        bookmark_fields: list[str] = []
        for link in topic.manual_links:
            bookmark_fields.append(link.link_name)
            bookmark_fields.append(link.searchable_title)
            bookmark_fields.append(link.url)
        field_blobs = {
            FIELD_NAME: topic.name.casefold(),
            FIELD_TAGS: " ".join(topic.tags).casefold(),
            FIELD_BOOKMARKS: " ".join(bookmark_fields).casefold(),
        }
        if include_filenames:
            field_blobs[FIELD_FILENAMES] = self.get_topic_filename_blob(topic_key)
        return match_topic(field_blobs, tokens)

    def get_topic_filename_blob(self, topic_key: str) -> str:
        return self.filename_index_by_key.get(topic_key, "")

    def rebuild_tree(self, topics_by_state: dict[str, list[tuple[str, Topic]]]) -> None:
        query_active = bool(self.search_input.text().strip())
        self._rebuilding_tree = True
        try:
            self.tree.clear()
            self.tree_items_by_key = {}

            if self.focused_hub_key:
                self._rebuild_focused_tree(topics_by_state)
                if (
                    self.current_topic_key
                    and self.current_topic_key in self.tree_items_by_key
                ):
                    self.tree.setCurrentItem(
                        self.tree_items_by_key[self.current_topic_key]
                    )
                    self.show_topic_details(self.current_topic_key)
                else:
                    self.current_topic_key = self.focused_hub_key
                    if self.current_topic_key in self.tree_items_by_key:
                        self.tree.setCurrentItem(
                            self.tree_items_by_key[self.current_topic_key]
                        )
                    self.show_topic_details(self.current_topic_key)
                self.tree.expandAll()
                return

            for state in STATE_ORDER:
                state_topics = topics_by_state.get(state, [])
                parent = QTreeWidgetItem([f"{state} ({len(state_topics)})"])
                parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                parent.setData(0, Qt.ItemDataRole.UserRole + 1, state)
                self.tree.addTopLevelItem(parent)

                if (
                    state == OTHER_STATE
                    and self._group_others_by_parent
                    and state_topics
                ):
                    for label, parent_path, items in self.group_other_topics(
                        state_topics
                    ):
                        group_item = QTreeWidgetItem([f"{label} ({len(items)})"])
                        group_item.setFlags(
                            group_item.flags() & ~Qt.ItemFlag.ItemIsSelectable
                        )
                        group_item.setToolTip(0, parent_path)
                        group_item.setData(
                            0,
                            Qt.ItemDataRole.UserRole + 1,
                            f"{OTHER_STATE}::{parent_path}",
                        )
                        parent.addChild(group_item)
                        for key, topic in items:
                            group_item.addChild(self._make_topic_item(key, topic))
                else:
                    for key, topic in state_topics:
                        item = self._make_topic_item(key, topic)
                        parent.addChild(item)
                        self._add_subhub_sections(item, key)

            self.tree.resizeColumnToContents(0)

            if (
                self.current_topic_key
                and self.current_topic_key in self.tree_items_by_key
            ):
                # setCurrentItem auto-expands collapsed ancestors, so the saved
                # expansion must be applied after it, inside the rebuild guard.
                self.tree.setCurrentItem(
                    self.tree_items_by_key[self.current_topic_key]
                )
                self.show_topic_details(self.current_topic_key)
            else:
                self.current_topic_key = None
                self.show_topic_details(None)

            if query_active:
                self.tree.expandAll()
            else:
                self._apply_saved_expansion()
        finally:
            self._rebuilding_tree = False

    def _rebuild_focused_tree(
        self, topics_by_state: dict[str, list[tuple[str, Topic]]]
    ) -> None:
        if not self.focused_hub_key:
            return
        focused_entry: tuple[str, Topic] | None = None
        for entries in topics_by_state.values():
            for key, topic in entries:
                if key == self.focused_hub_key:
                    focused_entry = (key, topic)
                    break
            if focused_entry:
                break
        if not focused_entry:
            return
        key, topic = focused_entry
        root_item = self._make_topic_item(key, topic)
        self.tree.addTopLevelItem(root_item)
        self._add_subhub_sections(root_item, key)

    def _add_subhub_sections(self, item: QTreeWidgetItem, project_key: str) -> None:
        by_state = self._subtopics_by_hub.get(project_key)
        if not by_state:
            return
        item.setData(
            0,
            Qt.ItemDataRole.UserRole + 1,
            f"{HUB_ROOT_EXPANSION_PREFIX}{project_key}",
        )
        for state in STATE_ORDER:
            if state == OTHER_STATE:
                continue
            entries = by_state.get(state, [])
            if not entries:
                continue
            section_item = QTreeWidgetItem([f"{state} ({len(entries)})"])
            section_item.setFlags(
                section_item.flags() & ~Qt.ItemFlag.ItemIsSelectable
            )
            section_item.setData(
                0, Qt.ItemDataRole.UserRole + 1, f"{project_key}::{state}"
            )
            item.addChild(section_item)
            for sub_key, sub_topic in entries:
                section_item.addChild(self._make_topic_item(sub_key, sub_topic))

    def _apply_saved_expansion(self) -> None:
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            expansion_key = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if expansion_key:
                key = str(expansion_key)
                # Sub-hub project nodes start collapsed to keep sections tidy.
                default = not key.startswith(HUB_ROOT_EXPANSION_PREFIX)
                item.setExpanded(self._section_expanded.get(key, default))
            iterator += 1

    def on_section_expansion_changed(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if self._rebuilding_tree:
            return
        if self.search_input.text().strip():
            return
        section = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if section:
            self._section_expanded[str(section)] = expanded
            self._save_expanded_sections()

    def _save_expanded_sections(self) -> None:
        QSettings().setValue(
            "session/expanded_sections", json.dumps(self._section_expanded)
        )

    def _make_topic_item(self, key: str, topic: Topic) -> QTreeWidgetItem:
        label = topic.name
        if topic.has_inconsistency:
            label = f"{label} [!]"
        if topic.pinned_rank is not None:
            label = f"📌 {label}"
        item = QTreeWidgetItem([label])
        if topic.has_inconsistency:
            item.setForeground(0, QBrush(QColor("#d17c00")))
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        matched_fields = self.match_fields_by_key.get(key)
        if matched_fields:
            item.setToolTip(0, f"Matched in: {format_match_fields(matched_fields)}")
        self.tree_items_by_key[key] = item
        return item

    def group_other_topics(
        self, state_topics: list[tuple[str, Topic]]
    ) -> list[tuple[str, str, list[tuple[str, Topic]]]]:
        items_by_parent: dict[str, list[tuple[str, Topic]]] = {}
        for key, topic in state_topics:
            parents = sorted(
                {
                    str(location.path.parent)
                    for location in topic.locations
                    if location.state == OTHER_STATE
                }
            )
            if not parents:
                parents = [""]
            for parent_path in parents:
                items_by_parent.setdefault(parent_path, []).append((key, topic))

        config_order = {
            str(root): index for index, root in enumerate(self.config.other_folders)
        }
        ordered_paths = sorted(
            items_by_parent,
            key=lambda path: (
                config_order.get(path, len(config_order)),
                path.casefold(),
            ),
        )

        base_labels = {
            path: (Path(path).name or path or "Unknown") for path in ordered_paths
        }
        label_counts = Counter(base_labels.values())
        groups: list[tuple[str, str, list[tuple[str, Topic]]]] = []
        for path in ordered_paths:
            label = base_labels[path]
            if label_counts[label] > 1 and path:
                parent_dir = Path(path)
                label = f"{parent_dir.parent.name}/{parent_dir.name}"
            groups.append((label, path, items_by_parent[path]))
        return groups

    def on_tree_selection(self) -> None:
        item = self.tree.currentItem()
        if not item:
            return
        topic_key = item.data(0, Qt.ItemDataRole.UserRole)
        if not topic_key:
            return
        self.current_topic_key = topic_key
        self.show_topic_details(topic_key)

    def selected_topic_keys(self) -> list[str]:
        keys: list[str] = []
        for item in self.tree.selectedItems():
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if key:
                keys.append(key)
        return keys

    def get_selected_topics(self) -> list[tuple[str, Topic]]:
        topics: list[tuple[str, Topic]] = []
        for key in self.selected_topic_keys():
            topic = self.topics_by_key.get(key)
            if topic:
                topics.append((key, topic))
        return topics

    def add_tag_to_selected_topics(self) -> None:
        keys = self.selected_topic_keys()
        if not keys:
            return
        dialog = TopicTagDialog(
            self,
            "Add Tag",
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

    def show_tree_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        topic_key = item.data(0, Qt.ItemDataRole.UserRole)
        if not topic_key:
            if item.data(0, Qt.ItemDataRole.UserRole + 1) == OTHER_STATE:
                self._show_others_header_menu(pos)
            return
        if topic_key not in self.selected_topic_keys():
            self.tree.setCurrentItem(item)

        menu = QMenu(self.tree)
        selected = self.get_selected_topics()
        if len(selected) > 1:
            self._populate_multi_topic_menu(menu, selected)
        else:
            self._populate_topic_menu(menu, topic_key)
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _show_others_header_menu(self, pos) -> None:
        menu = QMenu(self.tree)
        group_action = menu.addAction("Group by parent folder")
        group_action.setCheckable(True)
        group_action.setChecked(self._group_others_by_parent)
        group_action.toggled.connect(self.set_group_others_by_parent)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def set_group_others_by_parent(self, enabled: bool) -> None:
        if enabled == self._group_others_by_parent:
            return
        self._group_others_by_parent = enabled
        QSettings().setValue("view/others_group_by_parent", enabled)
        self.apply_filter()

    def _populate_multi_topic_menu(
        self, menu: QMenu, selected: list[tuple[str, Topic]]
    ) -> None:
        count = len(selected)
        move_action = menu.addAction(f"Move {count} topics…")
        move_action.setEnabled(
            any(self.topic_action_flags(key, topic)[0] for key, topic in selected)
        )
        move_action.triggered.connect(self.move_selected_topics)
        archive_action = menu.addAction(f"Archive {count} topics…")
        archive_action.setEnabled(
            any(self.topic_action_flags(key, topic)[1] for key, topic in selected)
        )
        archive_action.triggered.connect(self.archive_selected_topics)
        menu.addSeparator()
        tag_action = menu.addAction(f"Add Tag to {count} topics…")
        tag_action.triggered.connect(self.add_tag_to_selected_topics)

    def _populate_topic_menu(self, menu: QMenu, topic_key: str) -> None:
        topic = self.topics_by_key.get(topic_key)
        if not topic:
            return

        filesystem_paths = sorted(
            (str(loc.path) for loc in topic.locations if loc.source == "filesystem"),
            key=str.casefold,
        )
        obsidian_paths = sorted(
            (str(loc.path) for loc in topic.locations if loc.source == "obsidian"),
            key=str.casefold,
        )
        for path in filesystem_paths:
            label = "Open folder" if len(filesystem_paths) == 1 else f"Open folder: {path}"
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=path: self.open_path(value)
            )
        for path in obsidian_paths:
            label = (
                "Open in Obsidian"
                if len(obsidian_paths) == 1
                else f"Open in Obsidian: {path}"
            )
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=path: self.open_obsidian_path(value)
            )
        copy_action = menu.addAction("Copy title")
        copy_action.triggered.connect(self.copy_topic_title_to_clipboard)

        if not is_other_key(topic_key):
            pin_action = menu.addAction(
                "Unpin" if topic.pinned_rank is not None else "Pin"
            )
            pin_action.triggered.connect(
                lambda _checked=False, key=topic_key: self.toggle_pin_topic(key)
            )
            if topic_key in self.hubs_by_project_key:
                focus_action = menu.addAction("Focus")
                focus_action.triggered.connect(
                    lambda _checked=False, key=topic_key: self.open_subhub_focus(key)
                )

        menu.addSeparator()
        can_move, can_archive, can_rename = self.topic_action_flags(topic_key, topic)
        move_action = menu.addAction("Move…")
        move_action.setEnabled(can_move)
        move_action.triggered.connect(self.move_selected_topics)
        archive_action = menu.addAction("Archive…")
        archive_action.setEnabled(can_archive)
        archive_action.triggered.connect(self.archive_selected_topics)
        rename_action = menu.addAction("Rename…")
        rename_action.setEnabled(can_rename)
        rename_action.triggered.connect(self.rename_topic)
        tag_action = menu.addAction("Add Tag…")
        tag_action.triggered.connect(self.add_topic_tag)

        source_menu = QMenu("Add to Source", menu)
        if self.populate_json_source_menu(source_menu, topic):
            menu.addSeparator()
            menu.addMenu(source_menu)
            self._add_open_source_actions(menu, self.hub_for_topic(topic))

    def show_topic_details(self, topic_key: str | None) -> None:
        if not topic_key or topic_key not in self.topics_by_key:
            self.topic_title.setText("Select a topic")
            self.warning_label.setVisible(False)
            self.filesystem_group.set_links([])
            self.obsidian_group.set_links([])
            if self.onenote_group:
                self.onenote_group.set_links([])
                self.onenote_group.setVisible(False)
            if self.outlook_group:
                self.outlook_group.set_links([])
                self.outlook_group.setVisible(False)
            if self.plm_group:
                self.plm_group.set_links([])
                self.plm_group.setVisible(False)
            self.tags_panel.set_tags([])
            self.tags_panel.set_enabled(False)
            self.manual_links_panel.set_links([])
            self.manual_links_panel.set_enabled(False)
            self.move_button.setEnabled(False)
            self.rename_button.setEnabled(False)
            self.pin_button.setEnabled(False)
            self.pin_button.setText("Pin")
            self.focus_button.setEnabled(False)
            self.focus_button.setVisible(False)
            self.clear_json_add_menu()
            return

        topic = self.topics_by_key[topic_key]
        self.topic_title.setText(topic.name)
        if topic.has_inconsistency:
            self.warning_label.setText(
                "Inconsistency detected: topic appears in multiple lifecycle states."
            )
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        filesystem_paths = [
            str(loc.path) for loc in topic.locations if loc.source == "filesystem"
        ]
        obsidian_paths = [
            str(loc.path) for loc in topic.locations if loc.source == "obsidian"
        ]
        filesystem_paths.sort(key=str.casefold)
        obsidian_paths.sort(key=str.casefold)
        self.filesystem_group.set_links(filesystem_paths)
        self.obsidian_group.set_links(obsidian_paths)
        for spec in SOURCE_SPECS:
            group = getattr(self, f"{spec.attr}_group", None)
            if group is None:
                continue
            paths = spec.paths_of(topic)
            if paths:
                display_paths = [
                    self.display_source_path(topic, path) for path in paths
                ]
                override_url = self.find_manual_link_url(topic, spec.attr)
                if override_url:
                    links = [
                        (f"{path} (direct link)", override_url)
                        for path in display_paths
                    ]
                else:
                    links = [(path, "") for path in display_paths]
                group.set_links(links)
                group.setVisible(True)
            else:
                group.set_links([])
                group.setVisible(False)
        self.tags_panel.set_tags(topic.tags)
        self.tags_panel.set_enabled(True)
        self.manual_links_panel.set_links(topic.manual_links)
        self.manual_links_panel.set_enabled(True)
        selected = self.get_selected_topics()
        if len(selected) > 1:
            self.topic_title.setText(f"{topic.name} ({len(selected)} selected)")
            can_move = any(self.topic_action_flags(k, t)[0] for k, t in selected)
            can_rename = False
        else:
            can_move, _can_archive, can_rename = self.topic_action_flags(
                topic_key, topic
            )
        self.move_button.setEnabled(can_move)
        self.rename_button.setEnabled(can_rename)
        self.pin_button.setEnabled(not is_other_key(topic_key))
        self.pin_button.setText("Unpin" if topic.pinned_rank is not None else "Pin")
        is_subhub_project = topic_key in self.hubs_by_project_key
        self.focus_button.setEnabled(is_subhub_project)
        self.focus_button.setVisible(is_subhub_project)
        if self.focused_hub_key == topic_key:
            self.focus_button.setText("Focus Active")
            self.focus_button.setEnabled(False)
        else:
            self.focus_button.setText("Focus")
        self.update_json_add_menu(topic)

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
        self.exit_focus_button.setVisible(True)
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
        self.exit_focus_button.setVisible(False)
        if preferred_key and preferred_key in self.topics_by_key:
            self.current_topic_key = preferred_key
        elif self._pre_focus_topic_key and self._pre_focus_topic_key in self.topics_by_key:
            self.current_topic_key = self._pre_focus_topic_key
        self.apply_filter()
        self.update_status_summary()
        self.statusBar().showMessage("Exited Sub-Hub Focus", 3000)

    def topic_action_flags(
        self, topic_key: str, topic: Topic
    ) -> tuple[bool, bool, bool]:
        """Return (can_move, can_archive, can_rename) for a topic."""
        if is_other_key(topic_key):
            return False, False, False
        has_active_locations = any(
            loc.state not in {"Archive", OTHER_STATE} for loc in topic.locations
        )
        has_json_active = self.topic_has_unarchived_json(topic)
        has_any_locations = any(loc.state != OTHER_STATE for loc in topic.locations)
        has_json_source = bool(self.get_json_source_states(topic))
        return (
            has_any_locations or has_json_source,
            has_active_locations or has_json_active,
            True,
        )

    def filter_by_tag(self, tag_name: str) -> None:
        self.search_input.setText(f'tag:"{tag_name}"')

    def copy_topic_title_to_clipboard(self) -> None:
        if not self.current_topic_key:
            return
        topic = self.topics_by_key.get(self.current_topic_key)
        if not topic:
            return
        text = topic.name.strip()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"Copied '{text}' to clipboard", 2000)
        tooltip_pos = self.topic_title.mapToGlobal(self.topic_title.rect().bottomLeft())
        QTimer.singleShot(
            0,
            lambda: QToolTip.showText(
                tooltip_pos,
                "Copied to clipboard",
                self.topic_title,
                self.topic_title.rect(),
                1500,
            ),
        )

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
        empty_sections = {
            "sections": [
                {"name": folder_name, "topics": []}
                for folder_name in STATE_FOLDERS.values()
            ]
        }
        payload = json.dumps(empty_sections, indent=2)
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

    def clear_json_add_menu(self) -> None:
        self.json_add_menu.clear()
        self.json_add_button.setVisible(False)

    def update_json_add_menu(self, topic: Topic) -> None:
        self.json_add_menu.clear()
        has_sources = self.populate_json_source_menu(self.json_add_menu, topic)
        if has_sources:
            self.json_add_menu.addSeparator()
            self._add_open_source_actions(
                self.json_add_menu, self.hub_for_topic(topic)
            )
        self.json_add_button.setVisible(has_sources)

    def populate_json_source_menu(self, menu: QMenu, topic: Topic) -> bool:
        hub = self.hub_for_topic(topic)
        sources = [
            (spec.name, spec.enabled(hub), spec.paths_of(topic))
            for spec in SOURCE_SPECS
        ]
        enabled_sources = [source for source in sources if source[1]]
        for name, _enabled, paths in enabled_sources:
            if paths:
                action = menu.addAction(f"{name} (already added)")
                action.setEnabled(False)
                continue
            action = menu.addAction(f"Add to {name} list")
            action.triggered.connect(
                lambda _checked=False, source=name: self.add_topic_to_json_source(
                    source
                )
            )
        return bool(enabled_sources)

    def open_source_json(self, spec: SourceSpec, hub: HubConfig) -> None:
        self.open_path(str(spec.topics_path(hub)))

    def _add_open_source_actions(
        self, menu: QMenu, hub: HubConfig, label_suffix: str = ""
    ) -> int:
        menu.setToolTipsVisible(True)
        count = 0
        for spec in SOURCE_SPECS:
            if not spec.enabled(hub):
                continue
            path = spec.topics_path(hub)
            label = f"Open {spec.name} list{label_suffix}"
            if path.is_file():
                action = menu.addAction(label)
                action.triggered.connect(
                    lambda _checked=False, s=spec, h=hub: self.open_source_json(
                        s, h
                    )
                )
            else:
                action = menu.addAction(f"{label} (not found)")
                action.setEnabled(False)
            action.setToolTip(str(path))
            count += 1
        return count

    def _active_subhub_key(self) -> str | None:
        topic = (
            self.topics_by_key.get(self.current_topic_key)
            if self.current_topic_key
            else None
        )
        if topic and topic.hub_key:
            return topic.hub_key
        if self.current_topic_key in self.hubs_by_project_key:
            return self.current_topic_key
        return self.focused_hub_key

    def _any_sources_enabled(self) -> bool:
        hubs = [self.config.root_hub, *self.hubs_by_project_key.values()]
        return any(spec.enabled(hub) for hub in hubs for spec in SOURCE_SPECS)

    def populate_sources_menu(self) -> None:
        self.sources_menu.clear()
        count = self._add_open_source_actions(
            self.sources_menu, self.config.root_hub
        )
        subhub_key = self._active_subhub_key()
        if subhub_key:
            hub = self.hubs_by_project_key.get(subhub_key)
            if hub is not None:
                subhub_topic = self.topics_by_key.get(subhub_key)
                name = subhub_topic.name if subhub_topic else subhub_key
                if count:
                    self.sources_menu.addSeparator()
                count += self._add_open_source_actions(
                    self.sources_menu, hub, label_suffix=f" — {name}"
                )
        if count == 0:
            placeholder = self.sources_menu.addAction("No source lists enabled")
            placeholder.setEnabled(False)

    def add_topic_to_json_source(self, source: str) -> None:
        if not self.current_topic_key:
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

        if not self.confirm_add_json_topic(source, path, topic.name):
            return

        data, _original, error = self.load_json_target(path)
        if error:
            QMessageBox.warning(self, source, error)
            return
        if data is None:
            QMessageBox.warning(self, source, f"{path.name} is empty or invalid.")
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

        try:
            payload = json.dumps(data, indent=2, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(
                self, source, f"Unable to serialize {path.name}: {exc}"
            )
            return
        try:
            path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, source, f"Unable to write {path.name}: {exc}")
            return

        self.refresh_index()

    def confirm_add_json_topic(self, source: str, path: Path, topic_name: str) -> bool:
        dialog = OperationConfirmDialog(
            self,
            f"Add to {source}",
            f"Add '{topic_name}' to {path.name}?",
            details=[f"JSON source to update: {path}"],
            manual_actions=[
                f"Create the corresponding path for '{topic_name}' in {source}."
            ],
            accept_label="Add",
        )
        return dialog.exec() == QDialog.DialogCode.Accepted

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
            for key in list_keys:
                if key.strip().casefold() == candidate.casefold():
                    return self.add_topic_to_entries(data[key], topic_name)

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

    def topic_exists_in_json_data(self, data: object, topic_name: str) -> bool:
        if isinstance(data, list):
            return self.topic_in_entries(data, topic_name)
        if isinstance(data, dict):
            if self.topic_in_entries(data.get("topics"), topic_name):
                return True
            if self.topic_in_sections(data.get("sections"), topic_name):
                return True
            if "topics" in data or "sections" in data:
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

    def remove_topic_from_entries(self, entries: object, topic_name: str) -> int:
        if not isinstance(entries, list):
            return 0
        target = topic_name.strip().casefold()
        if not target:
            return 0
        removed = 0
        index = 0
        while index < len(entries):
            name = self.topic_entry_name(entries[index])
            if name and name.casefold() == target:
                entries.pop(index)
                removed += 1
                continue
            index += 1
        return removed

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

    def remove_topic_from_sections(self, sections: object, topic_name: str) -> int:
        if not isinstance(sections, list):
            return 0
        removed = 0
        for section in sections:
            if not isinstance(section, dict):
                continue
            removed += self.remove_topic_from_entries(section.get("topics"), topic_name)
            removed += self.remove_topic_from_sections(
                section.get("sections"), topic_name
            )
        return removed

    def add_manual_link(self) -> None:
        if not self.current_topic_key:
            return
        dialog = ManualLinkDialog(
            self,
            "Add Bookmark",
            name_suggestions=self.collect_link_name_suggestions(),
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
        dialog = TopicTagDialog(
            self,
            "Add Tag",
            tag_suggestions=self.collect_tag_name_suggestions(),
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
            "Edit Tag",
            tag_name=tag_name,
            tag_suggestions=self.collect_tag_name_suggestions(),
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
            "Edit Bookmark",
            link.link_name,
            link.url,
            name_suggestions=self.collect_link_name_suggestions(),
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

    def move_manual_links(self, links: list[ManualLink]) -> None:
        if not links:
            return
        source_keys = {link.topic_name for link in links}
        entries = self.collect_topic_picker_entries(source_keys)
        if not entries:
            QMessageBox.information(
                self, "Move bookmark", "There is no other topic to move to."
            )
            return
        if len(links) == 1:
            title = f"Move '{links[0].link_name}' to topic…"
        else:
            title = f"Move {len(links)} bookmarks to topic…"
        dialog = TopicPickerDialog(self, title, entries)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target_key = dialog.selected_key()
        if not target_key:
            return
        self._apply_bookmark_move(links, target_key)
        target_topic = self.topics_by_key.get(target_key)
        target_label = target_topic.name if target_topic else target_key
        self.statusBar().showMessage(
            f"Moved {len(links)} bookmark(s) to '{target_label}'", 4000
        )

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

        json_errors, written_targets = self.apply_json_renames(
            json_targets, old_name, new_name
        )
        if json_errors:
            rollback_errors = self.rollback_rename_operations(completed)
            for error in rollback_errors:
                json_errors.append(f"Rollback failed: {error}")
            message = [f"{verb} aborted; changes were rolled back.", *json_errors]
            QMessageBox.warning(self, f"{verb} failed", "\n".join(message))
            return

        migrated_link_ids = self.link_store.link_ids_for_topic(old_key)
        pin_ranks = self.link_store.list_pinned()
        tags_before = self.link_store.list_topic_tags()
        old_tags = list(tags_before.get(old_key, []))
        new_tags = list(tags_before.get(new_key, []))
        try:
            self.link_store.reassign_topic_key(old_key, new_key)
            if old_key in self.hubs_by_project_key:
                # Renaming a sub-hub project shifts every child topic key.
                self.link_store.reassign_topic_prefix(
                    hub_key_prefix(old_key), hub_key_prefix(new_key)
                )
            removed_links = self.link_store.dedupe_manual_links(
                new_key, migrated_link_ids
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
                tagged_previous_name = True

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
            tagged_previous_name=tagged_previous_name,
            old_pin_rank=pin_ranks.get(old_key),
            new_pin_rank=pin_ranks.get(new_key),
            old_tags=old_tags,
            new_tags=new_tags,
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
        serialisable["json_targets"] = [
            [label, str(path), original]
            for label, path, _data, original in payload["json_targets"]
        ]
        try:
            self.link_store.record_operation(str(payload["kind"]), serialisable)
        except Exception:
            # An unrecordable journal must not fail an operation that already
            # succeeded on disk; the user simply loses the undo affordance.
            self.clear_undo_prompt()
            return False
        return True

    def show_undo_prompt(self, message: str, kind: str) -> None:
        self.undo_button.setText(f"Undo {kind}")
        self.undo_button.setVisible(True)
        self.statusBar().showMessage(message, 15000)

    def clear_undo_prompt(self) -> None:
        self.undo_button.setVisible(False)

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

        errors = self.apply_undo(payload)
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

    def apply_undo(self, payload: dict[str, object]) -> list[str]:
        errors: list[str] = []
        moves = [
            (Path(source), Path(target))
            for source, target in payload.get("moves", [])
        ]
        boundaries = [Path(value) for value in payload.get("boundaries", [])]
        errors.extend(self.reverse_moves(moves, boundaries))

        for label, path_value, original in payload.get("json_targets", []):
            try:
                Path(path_value).write_text(original, encoding="utf-8")
            except OSError as exc:
                errors.append(f"{label}: Unable to restore {path_value}: {exc}")

        old_key = str(payload.get("old_key") or "")
        new_key = str(payload.get("new_key") or "")
        try:
            surviving = set(self.link_store.link_ids_for_topic(new_key))
            self.link_store.reassign_links_by_id(
                [
                    link_id
                    for link_id in payload.get("migrated_link_ids", [])
                    if link_id in surviving
                ],
                old_key,
            )
            self.link_store.restore_links(
                old_key, list(payload.get("removed_links", []))
            )
            if payload.get("tagged_previous_name"):
                self.link_store.remove_topic_tag(new_key, str(payload["old_name"]))
            self.restore_topic_tags(
                old_key,
                new_key,
                list(payload.get("old_tags", [])),
                list(payload.get("new_tags", [])),
            )
            self.link_store.set_pin_rank(new_key, payload.get("new_pin_rank"))
            self.link_store.set_pin_rank(old_key, payload.get("old_pin_rank"))
        except Exception as exc:
            errors.append(f"Topic metadata could not be restored: {exc}")
        return errors

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
                    operation.target.mkdir(parents=True, exist_ok=True)
                    report = merge_tree(
                        operation.source,
                        operation.target,
                        conflict_dirname,
                        apply=True,
                    )
                    operation.report = report
                    moves.extend(report.moves)
                else:
                    operation.target.parent.mkdir(parents=True, exist_ok=True)
                    safe_rename(operation.source, operation.target)
                    moves.append((operation.source, operation.target))
                completed.append(operation)
            except Exception as exc:
                errors.append(f"{operation.source} -> {operation.target}: {exc}")
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
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, None, f"Unable to read {path.name}: {exc}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            return None, None, f"Invalid JSON in {path.name}: {exc}"
        return data, content, None

    def apply_json_renames(
        self,
        targets: list[tuple[str, Path, object, str]],
        old_name: str,
        new_name: str,
    ) -> tuple[list[str], list[tuple[str, Path, object, str]]]:
        errors: list[str] = []
        written: list[tuple[str, Path, object, str]] = []
        for label, path, data, original in targets:
            updates = self.rename_topic_in_json_data(data, old_name, new_name)
            # A rename onto an existing name would otherwise leave the topic
            # listed twice in the same section.
            updates += self.dedupe_topic_in_json_data(data, new_name)
            if updates == 0:
                continue
            try:
                payload = json.dumps(data, indent=2, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                errors.append(f"{label}: Unable to serialize {path.name}: {exc}")
                errors.extend(self.restore_json_targets(written))
                return errors, []
            try:
                path.write_text(payload, encoding="utf-8")
            except OSError as exc:
                errors.append(f"{label}: Unable to write {path.name}: {exc}")
                written.append((label, path, data, original))
                errors.extend(self.restore_json_targets(written))
                return errors, []
            written.append((label, path, data, original))
        return errors, written

    def restore_json_targets(
        self, targets: list[tuple[str, Path, object, str]]
    ) -> list[str]:
        errors: list[str] = []
        for label, path, _data, original in targets:
            try:
                path.write_text(original, encoding="utf-8")
            except OSError as exc:
                errors.append(f"{label}: Unable to restore {path.name}: {exc}")
        return errors

    def dedupe_topic_in_json_data(self, data: object, name: str) -> int:
        if isinstance(data, list):
            return self.dedupe_topic_entries(data, name)
        if isinstance(data, dict):
            count = 0
            has_known_keys = "topics" in data or "sections" in data
            if "topics" in data:
                count += self.dedupe_topic_entries(data.get("topics"), name)
            if "sections" in data:
                count += self.dedupe_topic_sections(data.get("sections"), name)
            if not has_known_keys:
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
        """Collapse repeats of one topic within a single list, keeping the first.

        Entries in different sections are left alone: they mirror a topic that
        legitimately spans PARA sections.
        """
        if not isinstance(entries, list):
            return 0
        seen = False
        removed = 0
        index = 0
        while index < len(entries):
            if self.entry_names_topic(entries[index], name):
                if seen:
                    del entries[index]
                    removed += 1
                    continue
                seen = True
            index += 1
        return removed

    def entry_names_topic(self, entry: object, name: str) -> bool:
        if isinstance(entry, str):
            return entry.strip() == name
        if isinstance(entry, dict):
            for key in ("name", "topic", "title"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip() == name:
                    return True
        return False

    def rename_topic_in_json_data(
        self, data: object, old_name: str, new_name: str
    ) -> int:
        if isinstance(data, list):
            return self.rename_topic_entries(data, old_name, new_name)
        if isinstance(data, dict):
            count = 0
            has_known_keys = "topics" in data or "sections" in data
            if "topics" in data:
                count += self.rename_topic_entries(
                    data.get("topics"), old_name, new_name
                )
            if "sections" in data:
                count += self.rename_topic_sections(
                    data.get("sections"), old_name, new_name
                )
            if not has_known_keys:
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
            if isinstance(value, str) and value.strip() == old_name:
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

    def archive_selected_topics(self) -> None:
        topics = self.get_selected_topics()
        if topics:
            self.archive_topics(topics)

    def archive_topics(self, topics: list[tuple[str, Topic]]) -> None:
        self.move_topics(topics, "Archive")

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

    def move_selected_topics(self) -> None:
        topics = self.get_selected_topics()
        if not topics:
            return

        target_state, ok = QInputDialog.getItem(
            self,
            "Move topic",
            "Move to section:",
            ["Projects", "Areas", "Resources", "Archive"],
            0,
            False,
        )
        if not ok or not target_state:
            return
        self.move_topics(topics, target_state)

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

        json_update_errors: list[str] = []
        if json_targets:
            json_update_errors, _ = self.apply_json_moves(
                json_targets, target_state
            )

        self.report_operation_warnings(
            action_word, resolve_errors, move_errors, merge_conflicts,
            json_update_errors,
        )
        self.refresh_index()
        if archiving:
            status_message = f"Archived {len(topic_names)} topic(s)"
        else:
            status_message = f"Moved {len(topic_names)} topic(s) to '{target_label}'"
        self.statusBar().showMessage(status_message, 4000)

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

        removed = self.remove_topic_from_sections(sections, topic_name)
        target_names = self.section_names_for_state(target_state)
        if not target_names:
            return False, f"Unable to determine section name for '{target_state}'."

        target_section = None
        for candidate in target_names:
            target_section = self.find_section_by_name(sections, candidate)
            if target_section:
                break

        if target_section is None:
            target_section = {"name": target_names[0], "topics": []}
            sections.append(target_section)

        topics = target_section.get("topics")
        if topics is None:
            topics = []
            target_section["topics"] = topics
        if not isinstance(topics, list):
            return False, f"'{target_state}' section topics value is not a list."

        added = self.ensure_topic_in_entries(topics, topic_name)
        return (removed > 0 or added), None

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

        removed = 0
        for key in list_keys:
            removed += self.remove_topic_from_entries(data[key], topic_name)

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
        if target_key not in data:
            data[target_key] = []
        if not isinstance(data[target_key], list):
            return False, f"'{target_state}' list is not a list."

        added = self.ensure_topic_in_entries(data[target_key], topic_name)
        return (removed > 0 or added), None

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
                payload = json.dumps(data, indent=2, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                errors.append(f"{label}: Unable to serialize {path.name}: {exc}")
                errors.extend(self.restore_json_targets(written))
                return errors, []
            try:
                path.write_text(payload, encoding="utf-8")
            except OSError as exc:
                errors.append(f"{label}: Unable to write {path.name}: {exc}")
                written.append((label, path, data, original))
                errors.extend(self.restore_json_targets(written))
                return errors, []
            written.append((label, path, data, original))
        return errors, written

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

    def find_manual_link_url(self, topic: Topic, name: str) -> str | None:
        target = name.casefold()
        for link in topic.manual_links:
            if link.link_name.strip().casefold() == target:
                cleaned = link.url.strip()
                return cleaned if cleaned else None
        return None

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
