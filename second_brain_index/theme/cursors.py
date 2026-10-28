"""Pointing-hand cursor for every clickable control, applied in one place.

Qt style sheets cannot set a cursor, so each control would otherwise need its
own ``setCursor`` call; doing that by hand is how some controls ended up with
the hand and others with the arrow. ``PointerCursorFilter`` gives the hand to
clickable widgets as Qt styles them.

Item views (trees, lists) are deliberately left alone: a row is a selection,
not a button. The details tables are the exception, since each of their rows
opens something; ``RowHoverTable`` sets the hand on its own viewport.
"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QMenu,
    QProxyStyle,
    QTabBar,
    QWidget,
)

# Opt-in for widgets that are clickable but not one of the types below.
CLICKABLE_PROPERTY = "clickable"
# Marks widgets whose cursor this filter set, so it only undoes its own work.
_OWNED_PROPERTY = "_pointerCursor"


def is_clickable(widget: QWidget) -> bool:
    if isinstance(widget, (QAbstractButton, QTabBar, QMenu)):
        return True
    if isinstance(widget, QComboBox):
        return not widget.isEditable()
    return widget.property(CLICKABLE_PROPERTY) is True


class _PolishStyle(QProxyStyle):
    """Tells the filter about each widget once, when Qt styles it."""

    def __init__(self, on_polish) -> None:
        super().__init__()
        self._on_polish = on_polish

    def polish(self, target):
        result = super().polish(target)
        if isinstance(target, QWidget):
            self._on_polish(target)
        return result


class PointerCursorFilter(QObject):
    """Gives enabled clickable widgets the pointing-hand cursor.

    Widgets are found through the application style's ``polish`` hook, which
    runs once per widget, and only the clickable ones are then watched for
    being enabled or disabled. Nothing here runs for every event in the app.
    """

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self._style = _PolishStyle(self.sync)
        app.setStyle(self._style)
        # Widgets styled before this existed never get another polish.
        for widget in app.allWidgets():
            self.sync(widget)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # Installed only on the clickable widgets themselves.
        if event.type() == QEvent.Type.EnabledChange and isinstance(obj, QWidget):
            self.sync(obj)
        return False

    def sync(self, widget: QWidget) -> None:
        """Set, keep or remove the hand on one widget."""
        if not is_clickable(widget):
            return
        owned = widget.property(_OWNED_PROPERTY) is True
        # A cursor someone else chose on purpose is not ours to replace.
        if widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor) and not owned:
            return
        if widget.isEnabled():
            widget.setCursor(Qt.CursorShape.PointingHandCursor)
            if not owned:
                widget.setProperty(_OWNED_PROPERTY, True)
                widget.installEventFilter(self)
            if isinstance(widget, QComboBox):
                # The popup list is a separate window with its own cursor.
                widget.view().setCursor(Qt.CursorShape.PointingHandCursor)
        elif owned:
            widget.unsetCursor()
