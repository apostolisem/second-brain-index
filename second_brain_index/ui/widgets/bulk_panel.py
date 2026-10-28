from __future__ import annotations

from html import escape

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...constants import STATE_FOLDERS
from ...theme import BlueprintFrame, FlowLayout, set_prop
from .common import current_theme, role_label, secondary_button, tracked

MOVE_STATES = ("Projects", "Areas", "Resources", "Archive")


class StateButton(QPushButton):
    """Secondary button with the state on the left and its folder on the right."""

    def __init__(self, state: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)
        name = QLabel(state)
        name.setStyleSheet("font-family: inherit; background: transparent;")
        name.setFont(self.font())
        folder = role_label(STATE_FOLDERS[state], "count")
        for label in (name, folder):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(name)
        layout.addStretch()
        layout.addWidget(folder)
        self.setMinimumHeight(36)
        self.setAccessibleName(f"Move to {state}")


class BulkPanel(QWidget):
    """Main-area view while two or more topics are selected."""

    clear_requested = pyqtSignal()
    move_requested = pyqtSignal(str)
    tag_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outset = BlueprintFrame.OUTSET
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(28 - outset)

        top = QVBoxLayout()
        top.setSpacing(12)
        top.addWidget(
            tracked(role_label("SELECTION · CTRL/SHIFT-CLICK TO ADJUST", "kicker"))
        )
        title_row = QHBoxLayout()
        title_row.setSpacing(16)
        self.title_label = role_label("", "title", wrap=True)
        self.clear_button = secondary_button("Clear selection")
        self.clear_button.clicked.connect(self.clear_requested)
        title_row.addWidget(self.title_label, 1)
        title_row.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(title_row)
        self._chips_host = QWidget()
        policy = self._chips_host.sizePolicy()
        policy.setHeightForWidth(True)
        self._chips_host.setSizePolicy(policy)
        self._chips = FlowLayout(self._chips_host, spacing=6)
        top.addWidget(self._chips_host)
        outer.addLayout(top)

        cards = QGridLayout()
        cards.setContentsMargins(-outset, 0, -outset, -outset)
        cards.setHorizontalSpacing(20 - 2 * outset)
        cards.setColumnStretch(0, 1)
        cards.setColumnStretch(1, 1)

        move_card = BlueprintFrame()
        move_layout = QVBoxLayout(move_card)
        move_layout.setContentsMargins(16 + outset, 16 + outset, 16 + outset, 16 + outset)
        move_layout.setSpacing(12)
        self.move_title = role_label("", "card-title")
        move_layout.addWidget(self.move_title)
        buttons = QGridLayout()
        buttons.setSpacing(8)
        self.move_buttons: dict[str, StateButton] = {}
        for index, state in enumerate(MOVE_STATES):
            button = StateButton(state)
            button.clicked.connect(
                lambda _checked=False, target=state: self.move_requested.emit(target)
            )
            self.move_buttons[state] = button
            buttons.addWidget(button, index // 2, index % 2)
        move_layout.addLayout(buttons)
        move_layout.addWidget(
            role_label(
                "Existing destination folders are merged. Conflicts are kept in a"
                " “Merged from …” subfolder. Move isn’t undoable.",
                "body-muted",
                wrap=True,
            )
        )
        self.other_note = role_label("", "accent-note", wrap=True)
        move_layout.addWidget(self.other_note)
        move_layout.addStretch()
        cards.addWidget(move_card, 0, 0)

        tag_card = BlueprintFrame()
        tag_layout = QVBoxLayout(tag_card)
        tag_layout.setContentsMargins(16 + outset, 16 + outset, 16 + outset, 16 + outset)
        tag_layout.setSpacing(12)
        self.tag_title = role_label("", "card-title")
        tag_layout.addWidget(self.tag_title)
        tag_layout.addWidget(
            role_label(
                "Adds the tag to every selected topic that doesn’t have it yet.",
                "body-muted",
                wrap=True,
            )
        )
        self.tag_button = secondary_button("Add tag", "plus")
        self.tag_button.clicked.connect(self.tag_requested)
        tag_layout.addWidget(self.tag_button, 0, Qt.AlignmentFlag.AlignLeft)
        tag_layout.addStretch()
        cards.addWidget(tag_card, 0, 1)
        outer.addLayout(cards)
        outer.addStretch()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_selection(self, items: list[tuple[str, str]], other_count: int) -> None:
        """``items`` are (topic name, state) pairs for every selected topic."""
        count = len(items)
        eligible = count - other_count
        self.title_label.setText(f"{count} topics selected")
        self.move_title.setText(f"Move {eligible} topics to")
        self.tag_title.setText(f"Tag all {count} topics")
        for button in self.move_buttons.values():
            button.setEnabled(eligible > 0)
        if other_count:
            noun = "topic" if other_count == 1 else "topics"
            self.other_note.setText(
                f"{other_count} {noun} in Others will be skipped — they can’t be"
                " moved or archived."
            )
        self.other_note.setVisible(bool(other_count))

        subtle = current_theme().palette.subtle
        while self._chips.count():
            widget = self._chips.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        for name, state in items:
            chip = QLabel(
                f"{escape(name)}&nbsp;&nbsp;"
                f"<span style='color:{subtle}'>{escape(state)}</span>"
            )
            chip.setTextFormat(Qt.TextFormat.RichText)
            set_prop(chip, "role", "chip-neutral")
            self._chips.addWidget(chip)
        self._chips_host.updateGeometry()
