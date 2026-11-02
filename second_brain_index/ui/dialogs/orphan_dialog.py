from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from ...constants import STATE_FOLDERS
from ...theme import set_prop
from ..widgets.common import role_label, secondary_button
from .common import build_shell, note_label

if TYPE_CHECKING:
    from ..main_window import MainWindow
    from ..orphans import OrphanEntry

GROUPS = (
    (False, "Not found"),
    (True, "Unavailable — may come back"),
)


class OrphanDialog(QDialog):
    """Data stored under topics that no longer exist: reattach, restore or delete.

    Finding and resolving the orphans is the window's; this only lists and asks.
    """

    def __init__(self, parent: QWidget | None, planner: "MainWindow") -> None:
        super().__init__(parent)
        self.planner = planner
        self.entries_by_key: dict[str, OrphanEntry] = {}

        layout = build_shell(self, "Orphaned data", width=640)
        layout.addWidget(
            role_label(
                "Bookmarks, links and folders, tags and pins stored under a topic"
                " that no longer exists. Reattach them to a topic, bring the topic"
                " back as links-only, or delete them.",
                "body-muted",
                wrap=True,
            )
        )
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Topic", "Stored"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setMinimumHeight(260)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.tree)
        self.empty_label = role_label("No orphaned data.", "muted")
        layout.addWidget(self.empty_label)
        self.reason_note = note_label()
        layout.addWidget(self.reason_note)

        actions = QHBoxLayout()
        self.reattach_button = secondary_button("Reattach to topic…")
        self.reattach_button.clicked.connect(self.reattach)
        self.restore_button = secondary_button("Restore as links-only")
        restore_menu = QMenu(self.restore_button)
        for state in STATE_FOLDERS:
            action = restore_menu.addAction(state)
            action.triggered.connect(
                lambda _checked=False, target=state: self.restore(target)
            )
        self.restore_button.setMenu(restore_menu)
        self.delete_button = secondary_button("Delete…")
        self.delete_button.clicked.connect(self.delete)
        for button in (self.reattach_button, self.restore_button, self.delete_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        actions.addStretch(1)
        close_button = QPushButton("Close")
        set_prop(close_button, "variant", "primary")
        close_button.clicked.connect(self.accept)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        self.reload()

    def reload(self) -> None:
        entries = self.planner.find_orphans()
        self.entries_by_key = {entry.key: entry for entry in entries}
        self.tree.clear()
        for may_come_back, title in GROUPS:
            group = [entry for entry in entries if entry.may_come_back == may_come_back]
            if not group:
                continue
            header = QTreeWidgetItem([f"{title} ({len(group)})"])
            header.setFlags(header.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(header)
            for entry in group:
                item = QTreeWidgetItem([entry.label, entry.summary()])
                item.setData(0, Qt.ItemDataRole.UserRole, entry.key)
                item.setToolTip(0, entry.key)
                header.addChild(item)
            header.setExpanded(True)
        self.tree.resizeColumnToContents(0)
        self.tree.setVisible(bool(entries))
        self.empty_label.setVisible(not entries)
        self._update_buttons()

    def selected_entries(self) -> list[OrphanEntry]:
        keys = [item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()]
        return [self.entries_by_key[key] for key in keys if key in self.entries_by_key]

    def selected_keys(self) -> list[str]:
        return [entry.key for entry in self.selected_entries()]

    def _update_buttons(self) -> None:
        selected = self.selected_entries()
        for button in (self.reattach_button, self.delete_button):
            button.setEnabled(bool(selected))
        blockers = sorted(
            {entry.restore_blocked for entry in selected if entry.restore_blocked}
        )
        self.restore_button.setEnabled(bool(selected) and not blockers)
        # Say why Restore is off rather than leaving a dead button.
        reason = " ".join(blockers) if len(selected) == 1 else (
            "Some of the selected topics cannot be restored as links-only."
            if blockers else ""
        )
        self.reason_note.setText(reason)
        self.reason_note.setVisible(bool(reason))

    def reattach(self) -> None:
        keys = self.selected_keys()
        if keys and self.planner.reattach_orphans(keys):
            self.reload()

    def restore(self, state: str) -> None:
        keys = self.selected_keys()
        if keys:
            self.planner.restore_orphans(keys, state)
            self.reload()

    def delete(self) -> None:
        keys = self.selected_keys()
        if keys and self.planner.delete_orphans(keys):
            self.reload()
