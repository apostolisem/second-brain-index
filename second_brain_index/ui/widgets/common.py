from __future__ import annotations

import time

from PyQt6.QtCore import QEvent, QModelIndex, QObject, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QWidget,
)

from ...theme import ThemeManager, set_prop


def calculate_table_display_height(
    table: QTableWidget, row_indexes: list[int], max_visible_rows: int | None = None
) -> int:
    """Height that shows the header and the given rows, all of them unless capped."""
    header = table.horizontalHeader()
    # isHidden, not isVisible: this also runs before the window is first shown,
    # when nothing is visible yet but the header will be.
    header_height = (
        0 if header.isHidden() else max(header.height(), header.sizeHint().height())
    )
    frame_height = table.frameWidth() * 2
    default_row_height = table.verticalHeader().defaultSectionSize()
    if max_visible_rows is None:
        max_visible_rows = len(row_indexes)
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


class RowHoverDelegate(QStyledItemDelegate):
    """Tints every cell of the row under the pointer, not just the one cell."""

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        if self.is_hovered(option, index):
            painter.fillRect(option.rect, QColor(current_theme().palette.hover))
        super().paint(painter, option, index)

    def paint_row_background(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        """Selection or hover fill plus the rule under the row.

        For delegates that draw the whole cell themselves instead of calling
        ``super().paint``.
        """
        palette = current_theme().palette
        rect = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, QColor(palette.accent_tint))
        elif self.is_hovered(option, index):
            painter.fillRect(rect, QColor(palette.hover))
        painter.setPen(QColor(palette.rule))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

    def is_hovered(self, option: QStyleOptionViewItem, index: QModelIndex) -> bool:
        if option.state & QStyle.StateFlag.State_Selected:
            return False
        return index.row() == getattr(self.parent(), "hover_row", -1)


class RowHoverTable(QTableWidget):
    """A table that knows which row the pointer is over, cell widgets included.

    Every row is a link: the pointer shows the hand over it and one click
    anywhere on it emits ``row_activated``. Cell widgets (the row's buttons)
    keep their own clicks, and the space around them does nothing.
    """

    row_activated = pyqtSignal(int)

    def __init__(self, rows: int, columns: int, parent: QWidget | None = None) -> None:
        super().__init__(rows, columns, parent)
        self.hover_row = -1
        # Columns whose clicks mean something else, such as a checkbox.
        self.inert_columns: set[int] = set()
        self._last_activated_row = -1
        self._last_activated_at = 0.0
        self.setMouseTracking(True)
        self.setItemDelegate(RowHoverDelegate(self))
        self.cellClicked.connect(self._activate)

    def set_hover_row(self, row: int) -> None:
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        if row >= 0:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.viewport().unsetCursor()

    def _activate(self, row: int, column: int) -> None:
        if column in self.inert_columns:
            return
        # A double-click arrives as two clicks; the second must not act again.
        now = time.monotonic()
        interval = QApplication.doubleClickInterval() / 1000
        if row == self._last_activated_row and now - self._last_activated_at < interval:
            return
        self._last_activated_row = row
        self._last_activated_at = now
        self.row_activated.emit(row)

    def viewportEvent(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseMove:
            self.set_hover_row(self.rowAt(event.position().toPoint().y()))
        elif event.type() == QEvent.Type.Leave:
            self.set_hover_row(-1)
        return super().viewportEvent(event)

    def setCellWidget(self, row: int, column: int, widget: QWidget | None) -> None:
        # The widget takes the mouse events over its cell, so it reports them.
        if widget is not None:
            widget.installEventFilter(self)
            # Without this it would inherit the row's hand, promising a click
            # that the space around the buttons does not deliver.
            widget.setCursor(Qt.CursorShape.ArrowCursor)
        super().setCellWidget(row, column, widget)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Enter and isinstance(watched, QWidget):
            top = watched.mapTo(self.viewport(), QPoint(0, 0)).y()
            self.set_hover_row(self.rowAt(top + watched.height() // 2))
        return super().eventFilter(watched, event)

    def wheelEvent(self, event) -> None:
        # With nothing of its own to scroll, the wheel belongs to the page.
        bar = self.verticalScrollBar()
        if bar.minimum() == bar.maximum():
            event.ignore()
            return
        super().wheelEvent(event)

    def fit_height(self) -> None:
        """Grow or shrink to show every row; the page scrolls, not the table."""
        rows = [row for row in range(self.rowCount()) if not self.isRowHidden(row)]
        self.setFixedHeight(calculate_table_display_height(self, rows))


def current_theme() -> ThemeManager:
    """The application's theme, created on demand.

    ``main.pyw`` creates it at startup; anything else that builds these widgets
    (the tests, a script) gets one the first time it is needed.
    """
    try:
        return ThemeManager.instance()
    except RuntimeError:
        theme = ThemeManager(QApplication.instance())
        theme.apply()
        return theme


def make_row_button(icon_name: str, tooltip: str) -> QPushButton:
    button = QPushButton()
    button.setIcon(current_theme().icon(icon_name, "muted"))
    button.setToolTip(tooltip)
    set_prop(button, "variant", "ghost")
    return button


def make_edit_button() -> QPushButton:
    return make_row_button("pencil", "Edit")


def make_delete_button() -> QPushButton:
    return make_row_button("trash-2", "Delete")


def tracked(widget: QWidget, percent: int = 110) -> QWidget:
    """Apply the kicker letter-spacing; QSS has no property for it."""
    font = QFont(widget.font())
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, percent)
    widget.setFont(font)
    return widget


def role_label(text: str, role: str, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    set_prop(label, "role", role)
    return label


def section_label(text: str) -> QLabel:
    """Barlow Condensed 13 uppercase label above a details block."""
    return tracked(role_label(text.upper(), "section"), 104)


def ghost_button(text: str, icon: str | None = None, icon_size: int = 13) -> QPushButton:
    """Small text button: accent text, no border, 13px."""
    button = QPushButton(text)
    set_prop(button, "variant", "ghost")
    set_prop(button, "dense", "true")
    if icon:
        current_theme().bind_icon(button, icon, "accent_deep", icon_size)
    return button


def secondary_button(text: str, icon: str | None = None) -> QPushButton:
    button = QPushButton(text)
    if icon:
        current_theme().bind_icon(button, icon)
    return button
