from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QLineEdit, QMessageBox, QWidget

from .common import add_actions, add_field, build_shell, note_label


class DirectLinkDialog(QDialog):
    """Set what a OneNote, Outlook or PLM location opens when clicked."""

    def __init__(self, parent: QWidget | None, source_name: str, url: str = "") -> None:
        super().__init__(parent)
        layout = build_shell(self, f"Direct link for {source_name}")

        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("https://, onenote:, obsidian://")
        add_field(layout, "URL", self.url_edit)

        layout.addWidget(
            note_label(
                "Clicking this location opens this link instead of the"
                f" {source_name} home page."
            )
        )

        self.buttons = add_actions(self, layout, "Save")
        self.resize(self.minimumWidth() + 80, self.sizeHint().height())
        self.url_edit.setFocus()

    def accept(self) -> None:
        if not self.url_edit.text().strip():
            QMessageBox.warning(self, "Missing data", "A URL is required.")
            return
        super().accept()

    def value(self) -> str:
        return self.url_edit.text().strip()
