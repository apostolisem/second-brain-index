from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..operations import clean_manual_action_text
from ..widgets.common import role_label
from .common import add_actions, build_shell


class OperationConfirmDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        title: str,
        summary: str,
        details: list[str] | None = None,
        manual_actions: list[str] | None = None,
        merge_conflicts: list[str] | None = None,
        accept_label: str = "Continue",
    ) -> None:
        super().__init__(parent)
        self._manual_items: list[QListWidgetItem] = []

        layout = build_shell(self, title, width=620)
        summary_label = QLabel(summary)
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        self._add_manual_checklist(layout, manual_actions or [])

        detail_lines = list(details or [])
        if merge_conflicts:
            detail_lines.append("")
            detail_lines.append("Existing destination folders will be merged.")
            detail_lines.append(
                "Conflicting files are kept in a 'Merged from …' subfolder."
            )
            detail_lines.append("")
            detail_lines.append("Existing folders:")
            detail_lines.extend(f"  {path}" for path in merge_conflicts)

        if detail_lines:
            detail_box = QTextEdit()
            detail_box.setReadOnly(True)
            detail_box.setPlainText("\n".join(detail_lines))
            detail_box.setMinimumHeight(160)
            layout.addWidget(detail_box)

        self.buttons = add_actions(self, layout, accept_label)
        self.resize(700, self.sizeHint().height())
        self._update_ok_state()

    def _add_manual_checklist(
        self, layout: QVBoxLayout, manual_actions: list[str]
    ) -> None:
        if not manual_actions:
            return
        label = role_label(
            "Complete these manual steps before continuing:", "field-label", wrap=True
        )
        layout.addWidget(label)

        checklist = QListWidget()
        checklist.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        # Every row is a checkbox, so the whole list is clickable.
        checklist.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        for action in manual_actions:
            item = QListWidgetItem(clean_manual_action_text(action))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            checklist.addItem(item)
            self._manual_items.append(item)
        checklist.itemChanged.connect(lambda _item: self._update_ok_state())
        layout.addWidget(checklist)

    def _manual_complete(self) -> bool:
        return all(
            item.checkState() == Qt.CheckState.Checked for item in self._manual_items
        )

    def _update_ok_state(self) -> None:
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setEnabled(self._manual_complete())
