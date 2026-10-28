"""Shared shell for the app's dialogs: blueprint frame, title, fields, actions."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...theme import METRICS, BlueprintFrame, set_prop
from ..widgets.common import current_theme, role_label

DIALOG_WIDTH = 480


def build_shell(dialog: QDialog, title: str, width: int = DIALOG_WIDTH) -> QVBoxLayout:
    """Frame the dialog and return the layout its content goes into."""
    current_theme()  # dialogs can be built before any window exists
    dialog.setWindowTitle(title)
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(8, 8, 8, 8)
    frame = BlueprintFrame()
    outer.addWidget(frame)
    outset = BlueprintFrame.OUTSET
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14 + outset, 14 + outset, 14 + outset, 14 + outset)
    layout.setSpacing(10)
    dialog.title_label = role_label(title, "dialog-title", wrap=True)
    layout.addWidget(dialog.title_label)
    dialog.setMinimumWidth(width)
    return layout


def add_field(layout: QVBoxLayout, label: str, widget: QWidget) -> QLabel:
    """Add a 12px label with its input below it; returns the label."""
    field_label = role_label(label, "field-label")
    widget.setMinimumHeight(METRICS["control_height"])
    layout.addWidget(field_label)
    layout.addWidget(widget)
    return field_label


def note_label(text: str = "") -> QLabel:
    """Boxed 13px note under the fields."""
    label = role_label(text, "note", wrap=True)
    label.setVisible(bool(text))
    return label


def add_actions(
    dialog: QDialog, layout: QVBoxLayout, accept_label: str
) -> QDialogButtonBox:
    """Cancel (secondary) then the primary action, right-aligned."""
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
    ok_button.setText(accept_label)
    set_prop(ok_button, "variant", "primary")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    # Spare height goes above the actions, not between the fields.
    layout.addStretch(1)
    layout.addWidget(buttons)
    # Only once it is inside the dialog: set earlier, the dialog never learns
    # of it and Enter goes to whichever button the platform lays out first.
    ok_button.setDefault(True)
    return buttons
