from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QCompleter, QDialog, QLineEdit, QMessageBox, QWidget

from ...utils import infer_link_type
from .common import add_actions, add_field, build_shell, note_label


class ManualLinkDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        title: str,
        name: str = "",
        url: str = "",
        name_suggestions: list[str] | None = None,
        accept_label: str | None = None,
    ) -> None:
        super().__init__(parent)
        layout = build_shell(self, title)

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
        add_field(layout, "Name", self.name_edit)

        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("https://, mailto:, obsidian://, C:\\…")
        add_field(layout, "URL or path", self.url_edit)

        self.type_note = note_label()
        layout.addWidget(self.type_note)
        self.url_edit.textChanged.connect(self._update_type_note)
        self._update_type_note()

        # Editing an existing bookmark saves; a blank dialog adds one.
        default_label = "Save" if (name or url) else "Add bookmark"
        self.buttons = add_actions(self, layout, accept_label or default_label)
        self.resize(self.minimumWidth() + 80, self.sizeHint().height())

    def _update_type_note(self) -> None:
        url = self.url_edit.text().strip()
        self.type_note.setText(f"Detected type: {infer_link_type(url)}" if url else "")
        self.type_note.setVisible(bool(url))

    def accept(self) -> None:
        if not self.name_edit.currentText().strip() or not self.url_edit.text().strip():
            QMessageBox.warning(self, "Missing data", "Name and URL are required.")
            return
        super().accept()

    def values(self) -> tuple[str, str]:
        return self.name_edit.currentText().strip(), self.url_edit.text().strip()
