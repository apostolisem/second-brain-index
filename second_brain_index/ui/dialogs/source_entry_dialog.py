from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QComboBox, QDialog, QLabel, QLineEdit, QMessageBox, QWidget

from ...constants import PREFIX_TO_STATE, STATE_FOLDERS
from ...naming import validate_portable_name
from .common import add_actions, add_field, build_shell, note_label


class SourceEntryDialog(QDialog):
    """Add a topic to a OneNote, Outlook or PLM list, with an optional direct link.

    With ``topic_name`` the entry is for that existing topic; without it the
    name and PARA section are asked for, creating an entry of its own.
    """

    def __init__(
        self,
        parent: QWidget | None,
        source_name: str,
        list_path: Path,
        topic_name: str | None = None,
        state: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._topic_name = topic_name
        self._state = state or ""
        self._section_chosen = False
        self.name_edit: QLineEdit | None = None
        self.section_combo: QComboBox | None = None

        if topic_name is not None:
            layout = build_shell(self, f"Add to {source_name}")
            summary = QLabel(f"Add '{topic_name}' to the {source_name} list?")
            summary.setWordWrap(True)
            layout.addWidget(summary)
        else:
            layout = build_shell(self, f"New {source_name} entry")
            self.name_edit = QLineEdit()
            self.name_edit.setPlaceholderText("P - , A - or R - followed by a name")
            add_field(layout, "Name", self.name_edit)
            self.section_combo = QComboBox()
            for section_state, folder_name in STATE_FOLDERS.items():
                self.section_combo.addItem(folder_name, section_state)
            add_field(layout, "Section", self.section_combo)
            self.name_edit.textChanged.connect(self._preselect_section)
            self.section_combo.activated.connect(self._mark_section_chosen)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://, onenote:, obsidian://")
        add_field(layout, "Direct link (optional)", self.url_edit)

        layout.addWidget(
            note_label(
                f"Updates {list_path.name}. Create the matching folder in"
                f" {source_name} yourself."
            )
        )

        self.buttons = add_actions(
            self, layout, "Add" if topic_name is not None else "Create entry"
        )
        self.resize(self.minimumWidth() + 80, self.sizeHint().height())
        (self.name_edit or self.url_edit).setFocus()

    def _mark_section_chosen(self, _index: int) -> None:
        self._section_chosen = True

    def _preselect_section(self, text: str) -> None:
        """Follow the name's prefix until the user picks a section themselves."""
        if self._section_chosen or self.section_combo is None:
            return
        cleaned = text.strip()
        for prefix, state in PREFIX_TO_STATE.items():
            if cleaned.startswith(prefix):
                self.section_combo.setCurrentIndex(self.section_combo.findData(state))
                return

    def accept(self) -> None:
        if self.name_edit is not None:
            name = self.name_edit.text().strip()
            if not name:
                QMessageBox.warning(self, "Missing data", "A name is required.")
                return
            problem = validate_portable_name(name)
            if problem:
                QMessageBox.warning(self, "Invalid name", problem)
                return
        super().accept()

    def values(self) -> tuple[str, str, str]:
        """Name, PARA state and direct link (empty when none was given)."""
        if self.name_edit is not None and self.section_combo is not None:
            name = self.name_edit.text().strip()
            state = self.section_combo.currentData()
        else:
            name = self._topic_name or ""
            state = self._state
        return name, state, self.url_edit.text().strip()
