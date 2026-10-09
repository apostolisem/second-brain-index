from __future__ import annotations

from datetime import datetime

from PyQt6.QtWidgets import QLabel, QPushButton

from ...theme import ThemeManager, set_prop


class StatusBarMixin:
    """Status bar for MainWindow: undo prompt and index summary."""

    def _build_status_bar(self, theme: ThemeManager) -> None:
        self.undo_button = QPushButton("Undo merge")
        self.undo_button.setVisible(False)
        self.undo_button.clicked.connect(self.undo_last_operation)
        theme.bind_icon(self.undo_button, "undo-2", "accent_deep", 13)
        set_prop(self.undo_button, "variant", "ghost")
        self.statusBar().addPermanentWidget(self.undo_button)

        self.statusBar().setSizeGripEnabled(False)
        self.status_summary_label = QLabel("")
        self.statusBar().addPermanentWidget(self.status_summary_label)

    def update_status_summary(self) -> None:
        topic_count = len(self.topics_by_key)
        subtopic_count = sum(
            1 for topic in self.topics_by_key.values() if topic.hub_key is not None
        )
        inconsistency_count = sum(
            1 for topic in self.topics_by_key.values() if topic.has_inconsistency
        )
        parts = [f"{topic_count} topics"]
        if self.focused_hub_key:
            topic = self.topics_by_key.get(self.focused_hub_key)
            label = topic.name if topic else self.focused_hub_key
            parts.append(f"focused on {label}")
        if not self.location_filter.is_all():
            parts.append(f"location: {self.location_filter.label}")
        if subtopic_count:
            parts.append(f"{subtopic_count} in sub-hubs")
        if inconsistency_count:
            parts.append(f"{inconsistency_count} inconsistencies")
        parts.append(f"refreshed {datetime.now().strftime('%H:%M')}")
        self.status_summary_label.setText(" · ".join(parts))

    def show_undo_prompt(self, message: str, kind: str) -> None:
        self.undo_button.setText(f"Undo {kind}")
        self.undo_button.setVisible(True)
        self.statusBar().showMessage(message, 15000)

    def clear_undo_prompt(self) -> None:
        self.undo_button.setVisible(False)
