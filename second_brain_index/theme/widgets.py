"""Reusable themed widgets for the 1b layout."""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLayout,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from .manager import MODE_LABELS, MODES, ThemeManager


def set_prop(widget: QWidget, name: str, value) -> None:
    """Set a dynamic property used by the stylesheet and re-polish the widget."""
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class BlueprintFrame(QFrame):
    """Square hairline frame with "+" registration marks at each corner.

    Marks extend 6px outside the box, so the frame reserves that much margin;
    put content in a layout as usual. ``fill_role`` names a Palette attribute
    (e.g. ``"accent_tint"`` for notices) or None for a transparent card.
    """

    OUTSET = 6
    ARM = 5

    def __init__(self, parent: QWidget | None = None, fill_role: str | None = None) -> None:
        super().__init__(parent)
        self._fill_role = fill_role
        self.setContentsMargins(self.OUTSET, self.OUTSET, self.OUTSET, self.OUTSET)
        # A bound method, not a lambda: Qt drops the connection when the frame
        # is deleted, where a lambda would be called on a dead widget.
        ThemeManager.instance().changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, _palette) -> None:
        self.update()

    def set_fill_role(self, role: str | None) -> None:
        self._fill_role = role
        self.update()

    def paintEvent(self, event) -> None:
        p = ThemeManager.instance().palette
        o = self.OUTSET
        box = QRectF(self.rect()).adjusted(o + 0.5, o + 0.5, -o - 0.5, -o - 0.5)
        painter = QPainter(self)
        if self._fill_role:
            painter.fillRect(box, QColor(getattr(p, self._fill_role)))
        painter.setPen(QPen(QColor(p.divider), 1))
        painter.drawRect(box)
        painter.setPen(QPen(QColor(p.corner), 1))
        a = self.ARM
        for pt in (box.topLeft(), box.topRight(), box.bottomLeft(), box.bottomRight()):
            painter.drawLine(QPointF(pt.x() - a, pt.y()), QPointF(pt.x() + a, pt.y()))
            painter.drawLine(QPointF(pt.x(), pt.y() - a), QPointF(pt.x(), pt.y() + a))
        painter.end()
        super().paintEvent(event)


class FlowLayout(QLayout):
    """Left-to-right wrapping layout for tag chips."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), dry_run=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, dry_run=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, dry_run: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > area.right() + 1 and line_height > 0:
                x = area.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not dry_run:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class TagChip(QWidget):
    """Accent-tinted tag: click the label to filter, the × to remove."""

    filter_requested = pyqtSignal(str)
    remove_requested = pyqtSignal(str)

    def __init__(self, tag: str, parent: QWidget | None = None, removable: bool = True) -> None:
        super().__init__(parent)
        self.tag = tag
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        set_prop(self, "role", "tag")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 2, 4 if removable else 10, 2)
        layout.setSpacing(4)
        label = QPushButton(tag)
        label.setToolTip("Filter topics by this tag")
        set_prop(label, "variant", "tag")
        label.clicked.connect(lambda: self.filter_requested.emit(self.tag))
        layout.addWidget(label)
        if removable:
            remove = QToolButton()
            remove.setToolTip("Remove tag")
            remove.setAutoRaise(True)
            remove.setStyleSheet("border: none; padding: 2px; min-height: 0;")
            ThemeManager.instance().bind_icon(remove, "x", "accent_text", 12)
            remove.clicked.connect(lambda: self.remove_requested.emit(self.tag))
            layout.addWidget(remove)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class ThemeToggleButton(QToolButton):
    """Header control: click flips light/dark; the arrow menu offers Follow system."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = ThemeManager.instance()
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        set_prop(self, "variant", "ghost")
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)
        self._actions: dict[str, QAction] = {}
        for mode in MODES:
            action = menu.addAction(MODE_LABELS[mode])
            action.setCheckable(True)
            action.triggered.connect(lambda _c=False, m=mode: self._theme.set_mode(m))
            group.addAction(action)
            self._actions[mode] = action
        self.setMenu(menu)
        self.clicked.connect(self._theme.toggle)
        self._theme.changed.connect(self._sync)
        self._sync()

    def _sync(self, _palette=None) -> None:
        effective = self._theme.effective()
        target = "dark" if effective == "light" else "light"
        self.setIcon(self._theme.icon("moon" if target == "dark" else "sun", "muted"))
        self.setIconSize(QSize(17, 17))
        mode_label = MODE_LABELS[self._theme.mode]
        self.setToolTip(f"Switch to {target} theme  ·  Current: {mode_label}")
        self._actions[self._theme.mode].setChecked(True)
