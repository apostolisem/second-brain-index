from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ...constants import OTHER_STATE, PREFIX_TO_STATE, STATE_FOLDERS, is_other_key
from ...indexer import SOURCE_SPECS
from ...models import Topic
from ...theme import (
    CLICKABLE_PROPERTY,
    BlueprintFrame,
    FlowLayout,
    TagChip,
    ThemeManager,
    set_prop,
)
from .common import current_theme, ghost_button, role_label, tracked
from .topic_tree import ordered_states

MOVE_STATES = ("Projects", "Areas", "Resources", "Archive")


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Picked up by the application's PointerCursorFilter.
        self.setProperty(CLICKABLE_PROPERTY, True)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class TitleLabel(ClickableLabel):
    """Wrapping title that asks for its one-line width.

    A word-wrapped QLabel reports a narrow size hint, which would wrap a short
    title for no reason; this one only wraps when the row runs out of room.
    """

    def sizeHint(self) -> QSize:
        self.ensurePolished()
        width = self.fontMetrics().horizontalAdvance(self.text()) + 4
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return QSize(120, super().minimumSizeHint().height())


class TopicHeader(QWidget):
    """Kicker, title with its actions, tag chips and the inconsistency notice."""

    tag_filter_requested = pyqtSignal(str)
    tag_remove_requested = pyqtSignal(str)
    tag_edit_requested = pyqtSignal(str)
    tag_add_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        theme = current_theme()
        self._tags: list[str] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(28)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(12)
        outer.addWidget(top)

        kicker_row = QHBoxLayout()
        kicker_row.setSpacing(8)
        self.kicker_label = tracked(role_label("", "kicker"))
        self.folder_label = tracked(role_label("", "kicker-muted"))
        kicker_row.addWidget(self.kicker_label)
        kicker_row.addWidget(self.folder_label)
        kicker_row.addStretch()
        top_layout.addLayout(kicker_row)

        # Title and actions share a row while the title fits in two lines;
        # otherwise the actions drop underneath it (see _reflow).
        self.actions_stacked = False
        self._title_grid = QGridLayout()
        self._title_grid.setContentsMargins(0, 0, 0, 0)
        self._title_grid.setHorizontalSpacing(16)
        self._title_grid.setVerticalSpacing(12)
        self._title_grid.setColumnStretch(0, 1)
        self._title_block = QWidget()
        title_block = QHBoxLayout(self._title_block)
        title_block.setContentsMargins(0, 0, 0, 0)
        title_block.setSpacing(10)
        self.topic_title = TitleLabel("Select a topic")
        set_prop(self.topic_title, "role", "title")
        self.topic_title.setWordWrap(True)
        self.topic_title.setMinimumWidth(120)
        # Preferred, not Expanding: the copy icon follows the text.
        self.topic_title.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        self.topic_title.setToolTip("Click to copy topic title")
        self.copy_icon = ClickableLabel()
        self.copy_icon.setToolTip("Click to copy topic title")
        self.copy_icon.clicked.connect(self.topic_title.clicked)
        title_block.addWidget(self.topic_title)
        title_block.addWidget(self.copy_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        title_block.addStretch(1)

        self._actions_widget = QWidget()
        actions = QHBoxLayout(self._actions_widget)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.focus_button = QPushButton("Focus")
        self.focus_button.setVisible(False)
        self.pin_button = QPushButton()
        self.pin_button.setCheckable(True)
        set_prop(self.pin_button, "variant", "icon")
        theme.bind_icon(self.pin_button, "pin")
        self.move_button = QToolButton()
        self.move_button.setText("Move")
        self.move_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.move_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        theme.bind_icon(self.move_button, "arrow-right-left")
        self.move_menu = QMenu(self.move_button)
        self.move_menu.setMinimumWidth(240)
        self.move_button.setMenu(self.move_menu)
        self.rename_button = QPushButton("Rename")
        self.rename_button.setToolTip("Rename (F2)")
        theme.bind_icon(self.rename_button, "pencil")
        self.open_folder_button = QPushButton("Open folder")
        set_prop(self.open_folder_button, "variant", "primary")
        theme.bind_icon(self.open_folder_button, "folder-open", "on_accent")
        for button in (
            self.focus_button,
            self.pin_button,
            self.move_button,
            self.rename_button,
            self.open_folder_button,
        ):
            actions.addWidget(button)
        self._title_grid.addWidget(self._title_block, 0, 0)
        self._place_actions()
        top_layout.addLayout(self._title_grid)

        self.tags_row = QWidget()
        policy = self.tags_row.sizePolicy()
        policy.setHeightForWidth(True)
        self.tags_row.setSizePolicy(policy)
        self._tags_layout = FlowLayout(self.tags_row, spacing=6)
        top_layout.addWidget(self.tags_row)

        self.notice_frame = BlueprintFrame(fill_role="accent_tint")
        notice_layout = QHBoxLayout(self.notice_frame)
        outset = BlueprintFrame.OUTSET
        notice_layout.setContentsMargins(16 + outset, 12 + outset, 16 + outset, 12 + outset)
        notice_layout.setSpacing(12)
        self._notice_icon = QLabel()
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.resolve_button = QPushButton("Resolve with Move")
        self.resolve_button.clicked.connect(self.move_button.showMenu)
        notice_layout.addWidget(self._notice_icon)
        notice_layout.addWidget(self.warning_label, 1)
        notice_layout.addWidget(self.resolve_button)
        outer.addWidget(self.notice_frame)

        theme.changed.connect(self._apply_theme)
        self._apply_theme()
        self.clear()

    def _place_actions(self) -> None:
        self._title_grid.removeWidget(self._actions_widget)
        if self.actions_stacked:
            self._title_grid.addWidget(
                self._actions_widget, 1, 0, Qt.AlignmentFlag.AlignLeft
            )
        else:
            self._title_grid.addWidget(
                self._actions_widget,
                0,
                1,
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
            )

    def _reflow(self) -> None:
        """Drop the actions under the title when it does not fit on one line beside them."""
        self.topic_title.ensurePolished()
        # Showing or hiding a button only invalidates the cached size hint on
        # the next event-loop pass; this needs the new width now.
        self._actions_widget.layout().invalidate()
        spacing = self._title_grid.horizontalSpacing()
        copy_width = self.copy_icon.sizeHint().width() + 10
        beside = (
            self.width()
            - self._actions_widget.sizeHint().width()
            - spacing
            - copy_width
        )
        # sizeHint is the title's one-line width; any wrap means the row is too tight.
        stacked = self.topic_title.sizeHint().width() > beside
        if stacked != self.actions_stacked:
            self.actions_stacked = stacked
            self._place_actions()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def set_focus_available(self, available: bool) -> None:
        """Show or hide the Focus button; it changes how wide the actions are."""
        self.focus_button.setVisible(available)
        self._reflow()

    def _apply_theme(self, _palette=None) -> None:
        theme = current_theme()
        palette = theme.palette
        self.copy_icon.setPixmap(theme.icon("copy", "subtle").pixmap(16, 16))
        self._notice_icon.setPixmap(
            theme.icon("triangle-alert", "accent_text").pixmap(18, 18)
        )
        self.warning_label.setStyleSheet(f"color: {palette.accent_text};")
        self.resolve_button.setStyleSheet(f"background: {palette.bg};")

    def clear(self) -> None:
        self.set_kicker("", "")
        self.topic_title.setText("Select a topic")
        self.copy_icon.setVisible(False)
        self.set_tags([], enabled=False)
        self.set_notice(None)
        self.set_pinned(False)
        for button in (
            self.pin_button,
            self.move_button,
            self.rename_button,
            self.open_folder_button,
        ):
            button.setEnabled(False)
        self.set_focus_available(False)

    def set_kicker(self, kicker: str, folder: str) -> None:
        self.kicker_label.setText(kicker.upper())
        self.folder_label.setText(f"/ {folder.upper()}" if folder else "")

    def set_title(self, text: str) -> None:
        self.topic_title.setText(text)
        self.topic_title.updateGeometry()
        self._reflow()
        self.copy_icon.setVisible(True)

    def set_pinned(self, pinned: bool) -> None:
        self.pin_button.setChecked(pinned)
        self.pin_button.setToolTip("Unpin" if pinned else "Pin to top of section")

    def set_notice(self, text: str | None) -> None:
        self.warning_label.setText(text or "")
        self.notice_frame.setVisible(bool(text))

    def tags(self) -> list[str]:
        return list(self._tags)

    def set_tags(self, tags: list[str], enabled: bool = True) -> None:
        self._tags = list(tags)
        while self._tags_layout.count():
            item = self._tags_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.tags_row.setVisible(enabled)
        if not enabled:
            return
        for tag in tags:
            chip = TagChip(tag)
            chip.filter_requested.connect(self.tag_filter_requested)
            chip.remove_requested.connect(self.tag_remove_requested)
            chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            chip.customContextMenuRequested.connect(
                lambda pos, widget=chip: self._show_chip_menu(widget, pos)
            )
            self._tags_layout.addWidget(chip)
        add_button = ghost_button("Add tag", "plus")
        add_button.clicked.connect(self.tag_add_requested)
        self._tags_layout.addWidget(add_button)
        self.tags_row.updateGeometry()

    def build_chip_menu(self, tag: str) -> QMenu:
        menu = QMenu(self)
        filter_action = menu.addAction("Filter topics by this tag")
        filter_action.triggered.connect(
            lambda _checked=False: self.tag_filter_requested.emit(tag)
        )
        rename_action = menu.addAction("Rename tag…")
        rename_action.triggered.connect(
            lambda _checked=False: self.tag_edit_requested.emit(tag)
        )
        remove_action = menu.addAction("Remove tag")
        remove_action.triggered.connect(
            lambda _checked=False: self.tag_remove_requested.emit(tag)
        )
        return menu

    def _show_chip_menu(self, chip: TagChip, pos) -> None:
        self.build_chip_menu(chip.tag).exec(chip.mapToGlobal(pos))


class TopicHeaderMixin:
    """Topic title and actions for MainWindow."""

    def _build_topic_header(
        self, theme: ThemeManager, details_layout: QVBoxLayout
    ) -> None:
        header = TopicHeader()
        self.topic_header = header
        self.topic_title = header.topic_title
        self.move_button = header.move_button
        self.rename_button = header.rename_button
        self.pin_button = header.pin_button
        self.open_folder_button = header.open_folder_button
        self.focus_button = header.focus_button
        self.warning_label = header.warning_label

        self.topic_title.clicked.connect(self.copy_topic_title_to_clipboard)
        header.move_menu.aboutToShow.connect(self._populate_move_menu)
        self.rename_button.clicked.connect(self.rename_topic)
        self.pin_button.clicked.connect(self.toggle_pin_current_topic)
        self.open_folder_button.clicked.connect(self.open_current_topic_folder)
        self.focus_button.clicked.connect(self.focus_current_subhub)
        header.tag_filter_requested.connect(self.filter_by_tag)
        header.tag_remove_requested.connect(self.delete_topic_tag)
        header.tag_edit_requested.connect(self.edit_topic_tag)
        header.tag_add_requested.connect(self.add_topic_tag)
        details_layout.addWidget(header)

    def _populate_move_menu(self) -> None:
        menu = self.topic_header.move_menu
        menu.clear()
        key = self.current_topic_key
        topic = self.topics_by_key.get(key or "")
        if not key or topic is None:
            return
        heading = QWidgetAction(menu)
        label = tracked(role_label("MOVE TO", "kicker-muted"))
        label.setContentsMargins(14, 6, 14, 6)
        heading.setDefaultWidget(label)
        menu.addAction(heading)
        for state in MOVE_STATES:
            # The text after the tab is shown right-aligned, like a shortcut.
            action = menu.addAction(f"{state}\t{STATE_FOLDERS[state]}")
            # An inconsistent topic can be merged into any state, even its own.
            action.setEnabled(topic.has_inconsistency or topic.states != {state})
            action.triggered.connect(
                lambda _checked=False, target=state: self.move_topics(
                    [(key, topic)], target
                )
            )

    def topic_kicker(self, topic_key: str, topic: Topic) -> tuple[str, str]:
        """(kicker, folder) shown above the title."""
        if topic.hub_key:
            parent = self.topics_by_key.get(topic.hub_key)
            return (
                f"Sub-hub · {parent.name if parent else topic.hub_key}",
                STATE_FOLDERS.get(topic.display_state, topic.display_state),
            )
        if is_other_key(topic_key):
            return OTHER_STATE, "Additional root"
        return (
            topic.display_state,
            STATE_FOLDERS.get(topic.display_state, topic.display_state),
        )

    def inconsistency_notice(self, topic: Topic) -> str:
        found = " and ".join(ordered_states(topic.states))
        prefix = next(
            (prefix for prefix in PREFIX_TO_STATE if topic.name.startswith(prefix)),
            None,
        )
        if prefix and PREFIX_TO_STATE[prefix] == topic.display_state:
            reason = f"because of its “{prefix}” prefix"
        else:
            reason = "because that section comes first"
        return f"Found in {found}. Listed under {topic.display_state} {reason}."

    def topic_action_flags(
        self, topic_key: str, topic: Topic
    ) -> tuple[bool, bool, bool]:
        """Return (can_move, can_archive, can_rename) for a topic."""
        if is_other_key(topic_key):
            return False, False, False
        has_active_locations = any(
            loc.state not in {"Archive", OTHER_STATE} for loc in topic.locations
        )
        has_json_active = self.topic_has_unarchived_json(topic)
        has_any_locations = any(loc.state != OTHER_STATE for loc in topic.locations)
        has_json_source = bool(self.get_json_source_states(topic))
        return (
            has_any_locations or has_json_source,
            has_active_locations or has_json_active,
            True,
        )

    def copy_topic_title_to_clipboard(self) -> None:
        if not self.current_topic_key:
            return
        topic = self.topics_by_key.get(self.current_topic_key)
        if not topic:
            return
        text = topic.name.strip()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"Copied '{text}' to clipboard", 2000)
        tooltip_pos = self.topic_title.mapToGlobal(self.topic_title.rect().bottomLeft())
        QTimer.singleShot(
            0,
            lambda: QToolTip.showText(
                tooltip_pos,
                "Copied to clipboard",
                self.topic_title,
                self.topic_title.rect(),
                1500,
            ),
        )

    def populate_json_source_menu(self, menu: QMenu, topic: Topic) -> bool:
        if topic.display_state == OTHER_STATE:
            # An Others folder has no PARA section to be listed under.
            return False
        hub = self.hub_for_topic(topic)
        sources = [
            (spec.name, spec.enabled(hub), spec.paths_of(topic))
            for spec in SOURCE_SPECS
        ]
        enabled_sources = [source for source in sources if source[1]]
        for name, _enabled, paths in enabled_sources:
            if paths:
                action = menu.addAction(f"{name} (already added)")
                action.setEnabled(False)
                continue
            action = menu.addAction(f"Add to {name} list")
            action.triggered.connect(
                lambda _checked=False, source=name: self.add_topic_to_json_source(
                    source
                )
            )
        return bool(enabled_sources)
