from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QWidget,
)

from ..operations import KEEP_SECTIONS, RenamePreview, clean_manual_action_text
from ..widgets.common import role_label
from .common import add_actions, add_field, build_shell, note_label


class RenameTopicDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        old_name: str,
        preview_callback: Callable[[str, str | None], RenamePreview],
    ) -> None:
        super().__init__(parent)
        self._old_name = old_name
        self._preview_callback = preview_callback
        self._preview: RenamePreview | None = None
        self._manual_items: list[QListWidgetItem] = []
        self._refreshing = False

        layout = build_shell(self, "Rename topic", width=640)
        self.name_edit = QLineEdit(old_name)
        add_field(layout, "New topic name", self.name_edit)

        self.section_combo = QComboBox()
        self.section_label = add_field(
            layout, "Land merged topic in", self.section_combo
        )
        self.section_label.setVisible(False)
        self.section_combo.setVisible(False)

        # One live note: what the rename does, the merge warning, or the error.
        self.summary_label = note_label()
        layout.addWidget(self.summary_label)
        self.error_label = note_label()
        layout.addWidget(self.error_label)
        self.merge_label = note_label()
        layout.addWidget(self.merge_label)

        self.merge_confirm = QCheckBox("")
        self.merge_confirm.setVisible(False)
        self.merge_confirm.toggled.connect(lambda _checked: self._update_ok_state())
        layout.addWidget(self.merge_confirm)

        self.manual_label = role_label(
            "Complete these manual steps before renaming:", "field-label", wrap=True
        )
        self.manual_list = QListWidget()
        self.manual_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        # Every row is a checkbox, so the whole list is clickable.
        self.manual_list.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self.manual_list.setWordWrap(True)
        self.manual_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.manual_list.setMaximumHeight(120)
        self.manual_list.itemChanged.connect(lambda _item: self._update_ok_state())
        layout.addWidget(self.manual_label)
        layout.addWidget(self.manual_list)

        self.details_box = QTextEdit()
        self.details_box.setReadOnly(True)
        self.details_box.setMinimumHeight(140)
        layout.addWidget(self.details_box)

        self.buttons = add_actions(self, layout, "Rename")

        self.name_edit.textChanged.connect(self._refresh_preview)
        self.section_combo.currentTextChanged.connect(
            lambda _text: self._refresh_preview()
        )
        self.resize(720, self.sizeHint().height())
        self._refresh_preview()
        self.name_edit.selectAll()
        self.name_edit.setFocus()

    def _summary_text(self, preview: RenamePreview) -> str:
        """The note for a plain, valid rename."""
        new_name = preview.new_name
        if preview.error or preview.is_merge or not new_name or new_name == self._old_name:
            return ""
        count = len(preview.operations)
        noun = "location" if count == 1 else "locations"
        text = f"Renames {count} {noun} (filesystem and Obsidian)."
        if new_name.casefold() != self._old_name.casefold():
            text += f" '{self._old_name}' is kept as a tag so it stays searchable."
        return text

    def selected_section(self) -> str | None:
        # isHidden reflects the explicit setVisible call; isVisible would also
        # report False while the dialog itself has not been shown yet.
        if self.section_combo.isHidden():
            return None
        # KEEP_SECTIONS is returned verbatim rather than as None: None means
        # "no choice made yet, use the default", which is a different answer.
        return self.section_combo.currentText() or None

    def _refresh_preview(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._preview = self._preview_callback(
                self.name_edit.text().strip(), self.selected_section()
            )
            self.error_label.setText(self._preview.error or "")
            self.error_label.setVisible(bool(self._preview.error))
            summary = self._summary_text(self._preview)
            self.summary_label.setText(summary)
            self.summary_label.setVisible(bool(summary))
            self._populate_section_choices(self._preview)
            self._populate_merge_prompt(self._preview)
            self._populate_manual_items(self._preview.manual_actions)
            self.details_box.setPlainText("\n".join(self._preview.details))
            ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
            if ok_button:
                ok_button.setText("Merge" if self._preview.is_merge else "Rename")
        finally:
            self._refreshing = False
        self._update_ok_state()

    def _populate_section_choices(self, preview: RenamePreview) -> None:
        choices = list(preview.section_choices)
        visible = bool(choices)
        self.section_label.setVisible(visible)
        self.section_combo.setVisible(visible)
        if not visible:
            self.section_combo.clear()
            return
        current = preview.target_state or KEEP_SECTIONS
        if [self.section_combo.itemText(i) for i in range(self.section_combo.count())] != choices:
            self.section_combo.clear()
            self.section_combo.addItems(choices)
        index = self.section_combo.findText(current)
        if index >= 0:
            self.section_combo.setCurrentIndex(index)

    def _populate_merge_prompt(self, preview: RenamePreview) -> None:
        if not preview.is_merge:
            self.merge_label.setVisible(False)
            self.merge_confirm.setVisible(False)
            self.merge_confirm.setChecked(False)
            return
        conflicts = preview.conflicts
        summary = f"'{preview.new_name}' already exists — this will merge the two topics."
        if conflicts:
            summary += (
                f" {len(conflicts)} conflicting file(s) will be kept in a"
                f" 'Merged from {self._old_name}' subfolder."
            )
        self.merge_label.setText(summary)
        self.merge_label.setVisible(True)
        self.merge_confirm.setText(
            f"Merge '{self._old_name}' into '{preview.new_name}'"
        )
        self.merge_confirm.setVisible(True)

    def _populate_manual_items(self, actions: list[str]) -> None:
        self._manual_items = []
        self.manual_list.clear()
        self.manual_label.setVisible(bool(actions))
        self.manual_list.setVisible(bool(actions))
        for action in actions:
            item = QListWidgetItem(clean_manual_action_text(action))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.manual_list.addItem(item)
            self._manual_items.append(item)

    def _manual_complete(self) -> bool:
        return all(
            item.checkState() == Qt.CheckState.Checked for item in self._manual_items
        )

    def _merge_confirmed(self) -> bool:
        if self._preview is None or not self._preview.is_merge:
            return True
        return self.merge_confirm.isChecked()

    def _update_ok_state(self) -> None:
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setEnabled(
                self._preview is not None
                and self._preview.error is None
                and self._manual_complete()
                and self._merge_confirmed()
            )

    def preview(self) -> RenamePreview | None:
        return self._preview
