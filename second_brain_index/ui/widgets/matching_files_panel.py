from __future__ import annotations

import os
from collections.abc import Sequence

from PyQt6.QtCore import QModelIndex, QRect, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHeaderView,
    QMenu,
    QStyle,
    QStyleOptionViewItem,
    QTableWidgetItem,
    QVBoxLayout,
)

from ...filename_index import FileEntry
from ...search import SearchToken, filename_match_spans
from .common import (
    RowHoverDelegate,
    RowHoverTable,
    calculate_table_display_height,
    current_theme,
)

NAME_COLUMN = 0
FOLDER_COLUMN = 1


class MatchingFileDelegate(RowHoverDelegate):
    """Type icon and name with the matched text emphasised; a quiet folder path."""

    PAD = 8
    ICON_SIZE = 14
    ICON_GAP = 6

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tokens: tuple[SearchToken, ...] = ()
        # Tinting renders a pixmap per size, too slow to repeat for every cell.
        self._icons: dict[str, QIcon] = {}

    def set_tokens(self, tokens: Sequence[SearchToken]) -> None:
        self._tokens = tuple(tokens)

    def tokens(self) -> tuple[SearchToken, ...]:
        return self._tokens

    def clear_icons(self) -> None:
        self._icons.clear()

    def _icon(self, name: str, role: str) -> QIcon:
        if name not in self._icons:
            self._icons[name] = current_theme().icon(name, role)
        return self._icons[name]

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        painter.save()
        self.paint_row_background(painter, option, index)
        font = QFont(current_theme().fonts.body)
        font.setPixelSize(13)
        if index.column() == NAME_COLUMN:
            self._paint_name(painter, option, index, font)
        else:
            self._paint_folder(painter, option, index, font)
        painter.restore()

    def _paint_name(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
        font: QFont,
    ) -> None:
        palette = current_theme().palette
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_dir = bool(index.data(Qt.ItemDataRole.UserRole))
        x = rect.left() + self.PAD
        icon = (
            self._icon("folder", "accent_deep")
            if is_dir
            else self._icon("file-text", "muted")
        )
        icon.paint(
            painter,
            QRect(
                x,
                rect.center().y() - self.ICON_SIZE // 2 + 1,
                self.ICON_SIZE,
                self.ICON_SIZE,
            ),
        )
        x += self.ICON_SIZE + self.ICON_GAP

        match_font = QFont(font)
        match_font.setWeight(QFont.Weight.DemiBold)
        # Elided at the heavier weight, so emphasised runs can never overflow.
        available = max(rect.right() - self.PAD - x, 0)
        text = QFontMetrics(match_font).elidedText(
            index.data(Qt.ItemDataRole.DisplayRole) or "",
            Qt.TextElideMode.ElideMiddle,
            available,
        )
        # Matched on what is shown, so eliding cannot misplace the emphasis.
        spans = filename_match_spans(text, self._tokens)
        extension_start = len(text)
        if not is_dir:
            stem, extension = os.path.splitext(text)
            if stem and extension:
                extension_start = len(stem)

        normal = QColor(palette.accent_text if selected else palette.text)
        styles = {
            "normal": (font, normal),
            "extension": (font, QColor(palette.subtle)),
            "match": (match_font, QColor(palette.accent_deep)),
        }
        flags = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        for run, style in self._runs(text, spans, extension_start):
            run_font, color = styles[style]
            painter.setFont(run_font)
            painter.setPen(color)
            width = painter.fontMetrics().horizontalAdvance(run)
            painter.drawText(QRect(x, rect.top(), width, rect.height()), flags, run)
            x += width

    @staticmethod
    def _runs(
        text: str, spans: list[tuple[int, int]], extension_start: int
    ) -> list[tuple[str, str]]:
        """Split ``text`` into consecutive ``(run, style)`` pieces."""
        cuts = sorted(
            {0, len(text), extension_start, *(edge for span in spans for edge in span)}
        )
        runs: list[tuple[str, str]] = []
        for start, end in zip(cuts, cuts[1:]):
            if any(low <= start < high for low, high in spans):
                style = "match"
            elif start >= extension_start:
                style = "extension"
            else:
                style = "normal"
            runs.append((text[start:end], style))
        return runs

    def _paint_folder(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
        font: QFont,
    ) -> None:
        rect = option.rect.adjusted(self.PAD, 0, -self.PAD, 0)
        painter.setFont(font)
        painter.setPen(QColor(current_theme().palette.subtle))
        # The end of the path, the file's own folder, is the part worth keeping.
        text = painter.fontMetrics().elidedText(
            index.data(Qt.ItemDataRole.DisplayRole) or "",
            Qt.TextElideMode.ElideLeft,
            max(rect.width(), 0),
        )
        painter.drawText(
            rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text
        )


class MatchingFilesPanel(QGroupBox):
    """Lists the files under a topic that match the active search."""

    def __init__(self, on_open) -> None:
        super().__init__("Matching files")
        self._name_column_index = NAME_COLUMN
        self._folder_column_index = FOLDER_COLUMN
        # Share of the table's width given to the folder column.
        self._folder_width_ratio = 0.38
        self._max_visible_rows = 8
        self._row_height = 24
        self._on_open = on_open
        self._entries: list[FileEntry] = []

        layout = QVBoxLayout(self)
        self.table = RowHoverTable(0, 2)
        self.table.setHorizontalHeaderLabels(["NAME", "FOLDER"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self._delegate = MatchingFileDelegate(self.table)
        self.table.setItemDelegate(self._delegate)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.horizontalHeader().setSectionResizeMode(
            self._name_column_index, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            self._folder_column_index, QHeaderView.ResizeMode.Fixed
        )
        self.table.verticalHeader().setDefaultSectionSize(self._row_height)
        self.table.verticalHeader().setMinimumSectionSize(self._row_height)
        self.table.row_activated.connect(self._open)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        for sequence in ("Return", "Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self._open_current_row)
        layout.addWidget(self.table)
        current_theme().changed.connect(self._on_theme_changed)
        self.setVisible(False)

    def _on_theme_changed(self, _palette) -> None:
        self._delegate.clear_icons()
        self.table.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_folder_column()

    def _fit_folder_column(self) -> None:
        width = self.table.viewport().width()
        self.table.setColumnWidth(
            self._folder_column_index, int(width * self._folder_width_ratio)
        )

    def set_hits(
        self,
        entries: list[FileEntry],
        total: int,
        tokens: Sequence[SearchToken] = (),
    ) -> None:
        self._entries = entries
        self._delegate.set_tokens(tokens)
        if not entries:
            self.table.setRowCount(0)
            self.setVisible(False)
            return
        if total > len(entries):
            self.setTitle(f"Matching files ({len(entries)} of {total})")
        else:
            self.setTitle(f"Matching files ({len(entries)})")
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            full_path = str(entry.path)
            name_item = QTableWidgetItem(entry.name)
            name_item.setData(Qt.ItemDataRole.UserRole, entry.is_dir)
            name_item.setToolTip(full_path)
            folder_item = QTableWidgetItem(os.path.dirname(entry.rel_path))
            folder_item.setToolTip(full_path)
            self.table.setItem(row, self._name_column_index, name_item)
            self.table.setItem(row, self._folder_column_index, folder_item)
            self.table.setRowHeight(row, self._row_height)
        self.setVisible(True)
        height = calculate_table_display_height(
            self.table, list(range(len(entries))), self._max_visible_rows
        )
        header = self.table.horizontalHeader()
        if not header.isVisible():
            # Sized while the panel was still hidden: the header is not laid
            # out yet, so the helper left it out.
            height += header.sizeHint().height()
        self.table.setFixedHeight(height)
        self._fit_folder_column()

    def _show_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None or not 0 <= item.row() < len(self._entries):
            return
        self.table.selectRow(item.row())
        menu = self._build_context_menu(self._entries[item.row()])
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _build_context_menu(self, entry: FileEntry) -> QMenu:
        menu = QMenu(self.table)
        path = entry.path
        open_action = menu.addAction("Open")
        open_action.triggered.connect(
            lambda _checked=False, value=str(path): self._on_open(value)
        )
        folder_action = menu.addAction("Open containing folder")
        folder_action.triggered.connect(
            lambda _checked=False, value=str(path.parent): self._on_open(value)
        )
        copy_action = menu.addAction("Copy path")
        copy_action.triggered.connect(
            lambda _checked=False, value=str(path): QGuiApplication.clipboard().setText(
                value
            )
        )
        return menu

    def _open(self, row: int) -> None:
        if 0 <= row < len(self._entries):
            self._on_open(str(self._entries[row].path))

    def _open_current_row(self) -> None:
        self._open(self.table.currentRow())
