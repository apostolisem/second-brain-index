from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLineEdit, QWidget

from ...constants import STATE_FOLDERS
from .common import add_actions, add_field, build_shell, note_label


class InitializeSubHubDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        destination_root: Path,
        validate_callback: Callable[[str], str | None],
    ) -> None:
        super().__init__(parent)
        self.destination_root = destination_root
        self._validate_callback = validate_callback

        layout = build_shell(self, "Init Sub-Hub", width=580)
        self.name_edit = QLineEdit()
        add_field(layout, "Project folder name", self.name_edit)

        self.destination_label = note_label()
        self.destination_label.setVisible(True)
        layout.addWidget(self.destination_label)

        self.error_label = note_label()
        layout.addWidget(self.error_label)

        self.buttons = add_actions(self, layout, "Initialize")
        self.name_edit.textChanged.connect(lambda _text: self._refresh())
        self._refresh()
        self.name_edit.setFocus()

    def _refresh(self) -> None:
        name = self.name()
        destination = self.destination_path()
        self.destination_label.setText(f"Destination: {destination}")
        error = self._validate_callback(name)
        self.error_label.setText(error or "")
        self.error_label.setVisible(bool(error))
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setEnabled(error is None)

    def name(self) -> str:
        return self.name_edit.text().strip()

    def destination_path(self) -> Path:
        return self.destination_root / STATE_FOLDERS["Projects"] / self.name()
