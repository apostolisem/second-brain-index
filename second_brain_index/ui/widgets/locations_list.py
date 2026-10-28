from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QModelIndex, QRect, Qt
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QStyleOptionViewItem,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import ManualLocation
from ...theme import METRICS, set_prop
from .common import (
    RowHoverDelegate,
    RowHoverTable,
    current_theme,
    make_delete_button,
    make_edit_button,
    make_row_button,
    role_label,
    section_label,
)


@dataclass(frozen=True)
class LocationRow:
    source: str
    label: str
    tooltip: str
    open: Callable[[], None]
    # Set for folders on disk; enables the folder and copy actions.
    path: Path | None = None
    # Set for entries the user added by hand; enables edit and remove.
    manual: ManualLocation | None = None
    # Set for OneNote/Outlook/PLM rows; enables the direct-link actions.
    source_attr: str | None = None
    # What such a row opens instead of the source's home page, if set.
    direct_url: str = ""

    @property
    def head(self) -> str:
        """Everything up to the last segment, e.g. ``PARA / 01 - Projects / ``."""
        if self.manual is not None:
            return ""
        head, separator, _tail = self.label.rpartition(" / ")
        return f"{head}{separator}" if separator else ""

    @property
    def tail(self) -> str:
        if self.manual is not None:
            return self.label
        return self.label.rpartition(" / ")[2]


class LocationPathDelegate(RowHoverDelegate):
    """Muted parent path, emphasised last segment, elided in the middle."""

    PAD = 8

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        theme = current_theme()
        palette = theme.palette
        rect = option.rect
        head = index.data(Qt.ItemDataRole.UserRole) or ""
        tail = index.data(Qt.ItemDataRole.DisplayRole) or ""
        painter.save()
        self.paint_row_background(painter, option, index)

        head_font = QFont(theme.fonts.body)
        head_font.setPixelSize(13)
        tail_font = QFont(head_font)
        tail_font.setWeight(QFont.Weight.Medium)
        available = rect.width() - 2 * self.PAD
        painter.setFont(tail_font)
        tail_metrics = painter.fontMetrics()
        tail = tail_metrics.elidedText(tail, Qt.TextElideMode.ElideRight, max(available, 0))
        tail_width = tail_metrics.horizontalAdvance(tail)
        painter.setFont(head_font)
        head_metrics = painter.fontMetrics()
        head = head_metrics.elidedText(
            head, Qt.TextElideMode.ElideMiddle, max(available - tail_width, 0)
        )
        head_width = head_metrics.horizontalAdvance(head)

        x = rect.left() + self.PAD
        flags = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        painter.setFont(head_font)
        painter.setPen(QColor(palette.subtle))
        painter.drawText(QRect(x, rect.top(), head_width, rect.height()), flags, head)
        painter.setFont(tail_font)
        painter.setPen(QColor(palette.accent_deep))
        painter.drawText(
            QRect(x + head_width, rect.top(), tail_width, rect.height()), flags, tail
        )
        painter.restore()


class LocationsList(QWidget):
    """LOCATIONS section: one row per place the topic lives, plus manual entries."""

    SOURCE_COLUMN = 0
    LOCATION_COLUMN = 1
    ACTIONS_COLUMN = 2

    def __init__(
        self,
        on_open_folder,
        on_add_location=None,
        on_edit_location=None,
        on_delete_location=None,
        populate_json_menu=None,
        on_set_direct_link=None,
        on_clear_direct_link=None,
        on_remove_from_source=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_remove_from_source = on_remove_from_source
        self._on_set_direct_link = on_set_direct_link
        self._on_clear_direct_link = on_clear_direct_link
        self._on_open_folder = on_open_folder
        self._on_add_location = on_add_location
        self._on_edit_location = on_edit_location
        self._on_delete_location = on_delete_location
        self._populate_json_menu = populate_json_menu
        self._rows: list[LocationRow] = []
        self._row_height = METRICS["tree_row_height"]
        theme = current_theme()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        header_row.addWidget(section_label("Locations"))
        self.count_label = role_label("", "count")
        header_row.addWidget(self.count_label)
        header_row.addStretch()
        self.add_button = QToolButton()
        self.add_button.setText("Add location")
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        theme.bind_icon(self.add_button, "plus")
        self.add_menu = QMenu(self.add_button)
        self.add_menu.aboutToShow.connect(self._populate_add_menu)
        self.add_button.setMenu(self.add_menu)
        header_row.addWidget(self.add_button)
        layout.addLayout(header_row)

        self.table = RowHoverTable(0, 3)
        self.table.setHorizontalHeaderLabels(["SOURCE", "LOCATION", ""])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(
            self.SOURCE_COLUMN, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(self.LOCATION_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.ACTIONS_COLUMN, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.ACTIONS_COLUMN, 84)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        # Every row is shown; the details page does the scrolling.
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.verticalHeader().setDefaultSectionSize(self._row_height)
        self.table.verticalHeader().setMinimumSectionSize(self._row_height)
        self.table.setItemDelegateForColumn(
            self.LOCATION_COLUMN, LocationPathDelegate(self.table)
        )
        self.table.row_activated.connect(self._open_row)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        for sequence in ("Return", "Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self._open_current_row)
        layout.addWidget(self.table)

        self.empty_label = role_label("", "muted", wrap=True)
        self.empty_label.setContentsMargins(0, 8, 0, 8)
        layout.addWidget(self.empty_label)
        self._refresh_chrome()

    # -- public ------------------------------------------------------------
    def rows(self) -> list[LocationRow]:
        return list(self._rows)

    def set_rows(self, rows: list[LocationRow]) -> None:
        self._rows = list(rows)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            source_item = QTableWidgetItem(row.source)
            source_item.setToolTip(row.tooltip)
            location_item = QTableWidgetItem(row.tail)
            location_item.setData(Qt.ItemDataRole.UserRole, row.head)
            location_item.setToolTip(row.tooltip)
            self.table.setItem(index, self.SOURCE_COLUMN, source_item)
            self.table.setItem(index, self.LOCATION_COLUMN, location_item)
            self.table.setRowHeight(index, self._row_height)
            if row.manual is not None:
                self.table.setCellWidget(
                    index, self.ACTIONS_COLUMN, self._manual_actions(row.manual)
                )
            elif row.source_attr is not None:
                self.table.setCellWidget(
                    index, self.ACTIONS_COLUMN, self._source_actions(row)
                )
            else:
                self.table.removeCellWidget(index, self.ACTIONS_COLUMN)
        self._update_table_height()
        self._refresh_chrome()

    # -- internals ---------------------------------------------------------
    def _manual_actions(self, location: ManualLocation) -> QWidget:
        container = QWidget()
        actions = QHBoxLayout(container)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(2)
        actions.addStretch()
        edit_button = make_edit_button()
        edit_button.clicked.connect(
            lambda _checked=False, value=location: self._edit(value)
        )
        delete_button = make_delete_button()
        delete_button.setToolTip("Remove")
        delete_button.clicked.connect(
            lambda _checked=False, value=location: self._delete(value)
        )
        actions.addWidget(edit_button)
        actions.addWidget(delete_button)
        return container

    def _source_actions(self, row: LocationRow) -> QWidget:
        container = QWidget()
        actions = QHBoxLayout(container)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(2)
        actions.addStretch()
        link_button = make_row_button(
            "pencil", "Edit direct link" if row.direct_url else "Set direct link"
        )
        link_button.clicked.connect(
            lambda _checked=False, value=row: self._set_direct_link(value)
        )
        actions.addWidget(link_button)
        if self._on_remove_from_source is not None:
            remove_button = make_delete_button()
            remove_button.setToolTip(f"Remove from {row.source} list")
            remove_button.clicked.connect(
                lambda _checked=False, value=row: self._on_remove_from_source(value)
            )
            actions.addWidget(remove_button)
        return container

    def _refresh_chrome(self) -> None:
        self.count_label.setText(str(len(self._rows)) if self._rows else "")
        self.empty_label.setText("" if self._rows else "No locations")
        self.empty_label.setVisible(not self._rows)

    def _update_table_height(self) -> None:
        self.table.fit_height()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Rows set while hidden were measured before the header had its size.
        self._update_table_height()

    def _open_row(self, row: int) -> None:
        if 0 <= row < len(self._rows):
            self._rows[row].open()

    def _open_current_row(self) -> None:
        self._open_row(self.table.currentRow())

    def _edit(self, location: ManualLocation) -> None:
        if self._on_edit_location is not None:
            self._on_edit_location(location)

    def _delete(self, location: ManualLocation) -> None:
        if self._on_delete_location is not None:
            self._on_delete_location(location)

    def _set_direct_link(self, row: LocationRow) -> None:
        if self._on_set_direct_link is not None:
            self._on_set_direct_link(row)

    def _clear_direct_link(self, row: LocationRow) -> None:
        if self._on_clear_direct_link is not None:
            self._on_clear_direct_link(row)

    def _add(self) -> None:
        if self._on_add_location is not None:
            self._on_add_location()

    def _populate_add_menu(self) -> None:
        self.build_add_menu(self.add_menu)

    def build_add_menu(self, menu: QMenu | None = None) -> QMenu:
        """"Link or folder…" plus any managed lists the topic can be added to."""
        menu = menu if menu is not None else QMenu(self)
        menu.clear()
        add_action = menu.addAction("Link or folder…")
        add_action.triggered.connect(lambda _checked=False: self._add())
        if self._populate_json_menu is not None:
            separator = menu.addSeparator()
            before = len(menu.actions())
            self._populate_json_menu(menu)
            if len(menu.actions()) == before:
                menu.removeAction(separator)
        return menu

    def _show_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None or not 0 <= item.row() < len(self._rows):
            return
        self.table.selectRow(item.row())
        menu = self.build_context_menu(self._rows[item.row()])
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def build_context_menu(self, row: LocationRow) -> QMenu:
        menu = QMenu(self.table)
        open_action = menu.addAction("Open")
        open_action.triggered.connect(lambda _checked=False: row.open())
        if row.path is not None:
            folder_action = menu.addAction("Open containing folder")
            folder_action.triggered.connect(
                lambda _checked=False, value=str(row.path.parent): self._on_open_folder(
                    value
                )
            )
            copy_action = menu.addAction("Copy path")
            copy_action.triggered.connect(
                lambda _checked=False, value=str(row.path): QGuiApplication.clipboard().setText(
                    value
                )
            )
        elif row.manual is not None:
            copy_action = menu.addAction("Copy link")
            copy_action.triggered.connect(
                lambda _checked=False, value=row.manual.target: QGuiApplication.clipboard().setText(
                    value
                )
            )
        if row.source_attr is not None:
            if row.direct_url:
                copy_action = menu.addAction("Copy link")
                copy_action.triggered.connect(
                    lambda _checked=False, value=row.direct_url: QGuiApplication.clipboard().setText(
                        value
                    )
                )
            menu.addSeparator()
            link_action = menu.addAction(
                "Edit direct link…" if row.direct_url else "Set direct link…"
            )
            link_action.triggered.connect(
                lambda _checked=False, value=row: self._set_direct_link(value)
            )
            if row.direct_url:
                clear_action = menu.addAction("Remove direct link")
                clear_action.triggered.connect(
                    lambda _checked=False, value=row: self._clear_direct_link(value)
                )
            if self._on_remove_from_source is not None:
                menu.addSeparator()
                remove_action = menu.addAction(f"Remove from {row.source} list")
                remove_action.triggered.connect(
                    lambda _checked=False, value=row: self._on_remove_from_source(value)
                )
        if row.manual is not None:
            menu.addSeparator()
            edit_action = menu.addAction("Edit…")
            edit_action.triggered.connect(
                lambda _checked=False, value=row.manual: self._edit(value)
            )
            remove_action = menu.addAction("Remove")
            remove_action.triggered.connect(
                lambda _checked=False, value=row.manual: self._delete(value)
            )
        return menu
