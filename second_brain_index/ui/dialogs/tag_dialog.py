from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QLineEdit, QMessageBox, QPushButton, QWidget

from ...theme import FlowLayout, set_prop
from .common import add_actions, add_field, build_shell

MAX_SUGGESTIONS = 12


class TopicTagDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        title: str,
        tag_name: str = "",
        tag_suggestions: list[str] | None = None,
        exclude: list[str] | None = None,
        accept_label: str | None = None,
    ) -> None:
        super().__init__(parent)
        layout = build_shell(self, title)
        excluded = {tag.casefold() for tag in exclude or []}
        self._suggestions = [
            tag for tag in tag_suggestions or [] if tag.casefold() not in excluded
        ]

        self.tag_edit = QLineEdit(tag_name)
        add_field(layout, "Tag", self.tag_edit)

        # Existing tags as chips, narrowed by what has been typed.
        self.suggestions_host = QWidget()
        policy = self.suggestions_host.sizePolicy()
        policy.setHeightForWidth(True)
        self.suggestions_host.setSizePolicy(policy)
        self._suggestions_layout = FlowLayout(self.suggestions_host, spacing=6)
        layout.addWidget(self.suggestions_host)
        self.tag_edit.textChanged.connect(self._refresh_suggestions)
        self._refresh_suggestions()

        default_label = "Save" if tag_name else "Add tag"
        self.buttons = add_actions(self, layout, accept_label or default_label)
        self.resize(self.minimumWidth(), self.sizeHint().height())
        self.tag_edit.selectAll()
        self.tag_edit.setFocus()

    def visible_suggestions(self) -> list[str]:
        needle = self.tag_edit.text().strip().casefold()
        matches = [
            tag
            for tag in self._suggestions
            if needle in tag.casefold() and tag.casefold() != needle
        ]
        return matches[:MAX_SUGGESTIONS]

    def _refresh_suggestions(self) -> None:
        while self._suggestions_layout.count():
            widget = self._suggestions_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        suggestions = self.visible_suggestions()
        for tag in suggestions:
            chip = QPushButton(tag)
            chip.setAutoDefault(False)
            set_prop(chip, "variant", "tag-outline")
            chip.clicked.connect(
                lambda _checked=False, value=tag: self.tag_edit.setText(value)
            )
            self._suggestions_layout.addWidget(chip)
        self.suggestions_host.setVisible(bool(suggestions))
        self.suggestions_host.updateGeometry()

    def accept(self) -> None:
        if not self.tag_edit.text().strip():
            QMessageBox.warning(self, "Missing data", "Tag name is required.")
            return
        super().accept()

    def value(self) -> str:
        return self.tag_edit.text().strip()
