from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QWidget,
)

from ...utils import infer_link_type
from ..widgets.common import secondary_button
from .common import add_actions, add_field, build_shell, note_label


class LocationDialog(QDialog):
    """Add or edit a manual location: a URL or a folder path, with an optional label."""

    def __init__(
        self,
        parent: QWidget,
        title: str,
        target: str = "",
        label: str = "",
        accept_label: str | None = None,
    ) -> None:
        super().__init__(parent)
        layout = build_shell(self, title)

        self.target_edit = QLineEdit(target)
        self.target_edit.setPlaceholderText("https://, obsidian://, or a folder path")
        self.browse_button = secondary_button("Browse…", "folder-open")
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(self._browse)
        target_row = QWidget()
        target_layout = QHBoxLayout(target_row)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setSpacing(8)
        target_layout.addWidget(self.target_edit, 1)
        target_layout.addWidget(self.browse_button)
        add_field(layout, "URL or folder path", target_row)

        self.label_edit = QLineEdit(label)
        add_field(layout, "Label (optional)", self.label_edit)

        self.type_note = note_label()
        layout.addWidget(self.type_note)
        self.target_edit.textChanged.connect(self._update_note)
        self.label_edit.textChanged.connect(self._update_note)
        self._update_note()

        default_label = "Save" if target else "Add location"
        self.buttons = add_actions(self, layout, accept_label or default_label)
        self.resize(self.minimumWidth() + 80, self.sizeHint().height())
        self.target_edit.setFocus()

    def kind(self) -> str:
        """"file" for a folder path, otherwise the link's scheme family."""
        return infer_link_type(self.target_edit.text())

    def default_label(self) -> str:
        """What the list shows when the label is left empty."""
        target = self.target_edit.text().strip()
        if not target:
            return ""
        if self.kind() == "file":
            return Path(target.rstrip("/\\")).name or target
        from urllib.parse import urlparse

        return urlparse(target).netloc or target

    def _browse(self) -> None:
        start = self.target_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose folder", start)
        if folder:
            self.target_edit.setText(folder)

    def _update_note(self) -> None:
        target = self.target_edit.text().strip()
        if not target:
            self.type_note.setText("")
            self.type_note.setVisible(False)
            return
        if self.kind() == "file":
            text = "Detected type: Folder"
            if not Path(target).expanduser().exists():
                text += " — not found on this computer; it is saved anyway."
        else:
            text = "Detected type: Link"
        if not self.label_edit.text().strip():
            text += f"\nShown as: {self.default_label()}"
        self.type_note.setText(text)
        self.type_note.setVisible(True)

    def accept(self) -> None:
        if not self.target_edit.text().strip():
            QMessageBox.warning(self, "Missing data", "A URL or folder path is required.")
            return
        super().accept()

    def values(self) -> tuple[str, str]:
        return self.target_edit.text().strip(), self.label_edit.text().strip()
