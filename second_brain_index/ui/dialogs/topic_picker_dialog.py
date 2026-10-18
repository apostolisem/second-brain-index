from __future__ import annotations

from PyQt6.QtCore import QModelIndex, QRect, Qt
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from ...theme import METRICS, set_prop
from ..widgets.common import current_theme
from .common import add_actions, build_shell

STATE_ROLE = Qt.ItemDataRole.UserRole + 1


class PickerRowDelegate(QStyledItemDelegate):
    """Topic name on the left, its state in small muted text on the right."""

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        theme = current_theme()
        palette = theme.palette
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        if selected:
            painter.fillRect(rect, QColor(palette.accent_tint))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(rect, QColor(palette.hover))
        right = rect.right() - 10
        state = index.data(STATE_ROLE) or ""
        flags = Qt.AlignmentFlag.AlignVCenter
        if state:
            font = QFont(theme.fonts.body)
            font.setPixelSize(12)
            painter.setFont(font)
            width = painter.fontMetrics().horizontalAdvance(state)
            painter.setPen(QColor(palette.subtle))
            painter.drawText(
                QRect(right - width, rect.top(), width, rect.height()),
                flags | Qt.AlignmentFlag.AlignRight,
                state,
            )
            right -= width + 8
        font = QFont(theme.fonts.body)
        font.setPixelSize(14)
        painter.setFont(font)
        painter.setPen(QColor(palette.accent_text if selected else palette.text))
        left = rect.left() + 10
        text = painter.fontMetrics().elidedText(
            index.data(Qt.ItemDataRole.DisplayRole) or "",
            Qt.TextElideMode.ElideRight,
            max(right - left, 0),
        )
        painter.drawText(
            QRect(left, rect.top(), max(right - left, 0), rect.height()),
            flags | Qt.AlignmentFlag.AlignLeft,
            text,
        )
        painter.restore()


class TopicPickerDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        title: str,
        entries: list[tuple[str, str]],
        states: dict[str, str] | None = None,
        accept_label: str = "Move",
    ) -> None:
        super().__init__(parent)
        layout = build_shell(self, title)
        states = states or {}

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Type to filter topics…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumHeight(METRICS["control_height"])
        self.search_input.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_input)

        self.topic_list = QListWidget()
        set_prop(self.topic_list, "role", "picker")
        self.topic_list.setItemDelegate(PickerRowDelegate(self.topic_list))
        self.topic_list.setMouseTracking(True)
        self.topic_list.setMaximumHeight(240)
        self.topic_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        for key, label in entries:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(STATE_ROLE, states.get(key, ""))
            self.topic_list.addItem(item)
        self.topic_list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.topic_list.itemSelectionChanged.connect(self._update_ok_state)
        layout.addWidget(self.topic_list)

        self.buttons = add_actions(self, layout, accept_label)

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

        self.resize(self.minimumWidth() + 40, self.sizeHint().height())
        self._select_first_visible()
        self.search_input.setFocus()

    def _visible_rows(self) -> list[int]:
        return [
            row
            for row in range(self.topic_list.count())
            if not self.topic_list.item(row).isHidden()
        ]

    def _apply_filter(self, text: str) -> None:
        # Every typed term must appear somewhere in the label, in any order.
        terms = text.casefold().split()
        for row in range(self.topic_list.count()):
            item = self.topic_list.item(row)
            label = item.text().casefold()
            item.setHidden(not all(term in label for term in terms))
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
