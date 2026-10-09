from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from PyQt6.QtCore import QModelIndex, QRect, QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from ...constants import OTHER_STATE, STATE_FOLDERS, STATE_ORDER, is_other_key
from ...location_filter import ALL_LOCATIONS, topic_matches_location
from ...models import Topic
from ...search import (
    FIELD_BOOKMARKS,
    FIELD_FILENAMES,
    FIELD_LOCATIONS,
    FIELD_NAME,
    FIELD_TAGS,
    SearchToken,
    format_match_fields,
    match_topic,
    parse_search_query,
)
from ...theme import METRICS, set_prop
from .common import current_theme, ghost_button, role_label, secondary_button


HUB_ROOT_EXPANSION_PREFIX = "__hubroot__::"

ROLE_KEY = Qt.ItemDataRole.UserRole
ROLE_SECTION = Qt.ItemDataRole.UserRole + 1
# "section", "group" or "topic"; decides how the delegate paints the row.
ROLE_KIND = Qt.ItemDataRole.UserRole + 2
ROLE_META = Qt.ItemDataRole.UserRole + 3

KIND_SECTION = "section"
KIND_GROUP = "group"
KIND_TOPIC = "topic"

_PREFIX_PATTERN = re.compile(r"^([A-Z]) - (.+)$")


def split_topic_prefix(name: str) -> tuple[str, str]:
    """("P", "Alpha") for "P - Alpha"; ("", name) when there is no prefix."""
    match = _PREFIX_PATTERN.match(name)
    if match:
        return match.group(1), match.group(2)
    return "", name


def ordered_states(states: set[str]) -> list[str]:
    known = [state for state in STATE_ORDER if state in states]
    return known + sorted(states - set(known))


def topic_sort_key(item: tuple[str, Topic]) -> tuple[bool, int, str, str]:
    topic = item[1]
    return (
        topic.pinned_rank is None,
        topic.pinned_rank or 0,
        topic.name.casefold(),
        topic.name,
    )


class TopicTreeDelegate(QStyledItemDelegate):
    """Paints section headers, group rows and topic rows of the sidebar."""

    TOPIC_PAD = 16
    CHILD_PAD = 34
    GROUP_PAD = 30
    RIGHT_PAD = 14
    GROUP_ROW_HEIGHT = 26

    def __init__(self, tree: QTreeWidget) -> None:
        super().__init__(tree)
        self._tree = tree

    def _font(self, pixel_size: int, medium: bool = False, tracked: bool = False) -> QFont:
        font = QFont(current_theme().fonts.body)
        font.setPixelSize(pixel_size)
        if medium:
            font.setWeight(QFont.Weight.Medium)
        if tracked:
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        return font

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        if index.data(ROLE_KIND) == KIND_GROUP:
            return QSize(120, self.GROUP_ROW_HEIGHT)
        return QSize(120, METRICS["tree_row_height"])

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        kind = index.data(ROLE_KIND)
        meta = index.data(ROLE_META) or {}
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if kind == KIND_SECTION:
            self._paint_section(painter, option, index, meta)
        elif kind == KIND_GROUP:
            self._paint_group(painter, option, meta)
        else:
            self._paint_topic(painter, option, index, meta)
        painter.restore()

    def _draw_icon(
        self, painter: QPainter, name: str, role: str, x: int, center_y: int, size: int
    ) -> None:
        icon = current_theme().icon(name, role)
        icon.paint(painter, QRect(x, center_y - size // 2, size, size))

    def group_action_rect(self, row: QRect, meta: dict) -> QRect:
        """Where the Others header's Group / Ungroup button is painted."""
        label = meta.get("group_action")
        if not label:
            return QRect()
        width = QFontMetrics(self._font(11)).horizontalAdvance(label) + 10
        center_y = row.top() + 19
        return QRect(row.right() - 10 - width, center_y - 9, width, 18)

    def hub_tag_rect(self, row: QRect, meta: dict) -> QRect:
        """Where a sub-hub project's "hub" tag is painted."""
        if not meta.get("hub"):
            return QRect()
        right = row.right() - self.RIGHT_PAD
        if meta.get("warn"):
            right -= 14 + 8
        if meta.get("pinned"):
            right -= 13 + 8
        width = QFontMetrics(self._font(10)).horizontalAdvance("hub") + 10
        return QRect(right - width, row.center().y() - 8, width, 16)

    def _paint_section(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
        meta: dict,
    ) -> None:
        palette = current_theme().palette
        row = option.rect
        # 12px above the label and 6px below it, as in the design.
        center_y = row.top() + 19
        x = row.left() + (self.GROUP_PAD if meta.get("indent") else self.TOPIC_PAD)
        chevron = "chevron-down" if self._tree.isExpanded(index) else "chevron-right"
        self._draw_icon(painter, chevron, "muted", x, center_y, 14)
        x += 14 + 6

        right = row.right() - 10
        action_rect = self.group_action_rect(row, meta)
        if action_rect.isValid():
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
            if hovered:
                painter.fillRect(action_rect, QColor(palette.accent_tint))
            painter.setFont(self._font(11))
            painter.setPen(QColor(palette.accent_deep))
            painter.drawText(
                action_rect, Qt.AlignmentFlag.AlignCenter, meta["group_action"]
            )
            right = action_rect.left() - 6

        count = str(meta.get("count", ""))
        painter.setFont(self._font(11))
        count_width = painter.fontMetrics().horizontalAdvance(count)
        painter.setPen(QColor(palette.subtle))
        painter.drawText(
            QRect(right - count_width, center_y - 9, count_width, 18),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            count,
        )
        right -= count_width + 8

        painter.setFont(self._font(11, medium=True, tracked=True))
        painter.setPen(QColor(palette.muted))
        label = painter.fontMetrics().elidedText(
            meta.get("label", ""), Qt.TextElideMode.ElideRight, max(right - x, 0)
        )
        painter.drawText(
            QRect(x, center_y - 9, max(right - x, 0), 18),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            label,
        )

    def _paint_group(
        self, painter: QPainter, option: QStyleOptionViewItem, meta: dict
    ) -> None:
        palette = current_theme().palette
        row = option.rect
        center_y = row.center().y()
        x = row.left() + self.GROUP_PAD
        self._draw_icon(painter, "folder", "muted", x, center_y, 13)
        x += 13 + 6
        right = row.right() - self.RIGHT_PAD

        count = str(meta.get("count", ""))
        painter.setFont(self._font(11))
        count_width = painter.fontMetrics().horizontalAdvance(count)
        painter.setPen(QColor(palette.subtle))
        painter.drawText(
            QRect(right - count_width, row.top(), count_width, row.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            count,
        )
        right -= count_width + 8

        painter.setFont(self._font(12))
        painter.setPen(QColor(palette.muted))
        label = painter.fontMetrics().elidedText(
            meta.get("label", ""), Qt.TextElideMode.ElideRight, max(right - x, 0)
        )
        painter.drawText(
            QRect(x, row.top(), max(right - x, 0), row.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            label,
        )

    def _paint_topic(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
        meta: dict,
    ) -> None:
        palette = current_theme().palette
        row = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected:
            painter.fillRect(row, QColor(palette.accent_tint))
        elif hovered:
            painter.fillRect(row, QColor(palette.hover))

        center_y = row.center().y()
        x = row.left() + (self.CHILD_PAD if meta.get("indent") else self.TOPIC_PAD)
        right = row.right() - self.RIGHT_PAD

        if meta.get("warn"):
            self._draw_icon(painter, "triangle-alert", "text", right - 14, center_y, 14)
            right -= 14 + 8
        if meta.get("pinned"):
            self._draw_icon(painter, "pin", "accent_deep", right - 13, center_y, 13)
            right -= 13 + 8
        hub_rect = self.hub_tag_rect(row, meta)
        if hub_rect.isValid():
            painter.setPen(QPen(QColor(palette.accent), 1))
            painter.drawRect(hub_rect.adjusted(0, 0, -1, -1))
            painter.setFont(self._font(10))
            painter.setPen(QColor(palette.accent_deep))
            painter.drawText(hub_rect, Qt.AlignmentFlag.AlignCenter, "hub")
            right = hub_rect.left() - 8

        prefix = meta.get("prefix", "")
        painter.setFont(self._font(11, medium=True))
        painter.setPen(QColor(palette.subtle))
        painter.drawText(
            QRect(x, row.top(), 14, row.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            prefix,
        )
        x += 14 + 8

        note = meta.get("note", "")
        if note:
            painter.setFont(self._font(11))
            metrics = painter.fontMetrics()
            note_width = min(metrics.horizontalAdvance(note), max((right - x) // 2, 0))
            note_text = metrics.elidedText(note, Qt.TextElideMode.ElideRight, note_width)
            painter.setPen(QColor(palette.subtle))
            painter.drawText(
                QRect(right - note_width, row.top(), note_width, row.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                note_text,
            )
            right -= note_width + 8

        painter.setFont(self._font(14, medium=selected))
        painter.setPen(QColor(palette.accent_text if selected else palette.text))
        name = painter.fontMetrics().elidedText(
            meta.get("rest") or index.data(Qt.ItemDataRole.DisplayRole) or "",
            Qt.TextElideMode.ElideRight,
            max(right - x, 0),
        )
        painter.drawText(
            QRect(x, row.top(), max(right - x, 0), row.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            name,
        )


class TopicTree(QTreeWidget):
    """Sidebar tree. Rows are painted by ``TopicTreeDelegate``."""

    group_toggle_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._delegate = TopicTreeDelegate(self)
        self.setItemDelegate(self._delegate)
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # The delegate draws its own chevrons and indents.
        self.setIndentation(0)
        self.setRootIsDecorated(False)
        self.setExpandsOnDoubleClick(False)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        # Row backgrounds are painted by the delegate, not by the stylesheet.
        self.setStyleSheet(
            "QTreeView { padding-top: 8px; }"
            "QTreeView::item, QTreeView::item:hover, QTreeView::item:selected"
            " { background: transparent; }"
        )

    def _row_rect(self, item: QTreeWidgetItem) -> QRect:
        rect = self.visualItemRect(item)
        rect.setLeft(0)
        rect.setRight(self.viewport().width() - 1)
        return rect

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        if item is None:
            return
        kind = item.data(0, ROLE_KIND)
        meta = item.data(0, ROLE_META) or {}
        row = self._row_rect(item)
        if kind == KIND_SECTION:
            if self._delegate.group_action_rect(row, meta).contains(pos):
                self.group_toggle_requested.emit()
            else:
                item.setExpanded(not item.isExpanded())
        elif kind == KIND_GROUP:
            item.setExpanded(not item.isExpanded())
        elif self._delegate.hub_tag_rect(row, meta).contains(pos):
            item.setExpanded(not item.isExpanded())

    def mouseDoubleClickEvent(self, event) -> None:
        super().mouseDoubleClickEvent(event)
        item = self.itemAt(event.position().toPoint())
        # Sections toggle on each click already; only sub-hub projects need this.
        if (
            item is not None
            and item.data(0, ROLE_KIND) == KIND_TOPIC
            and item.childCount()
        ):
            item.setExpanded(not item.isExpanded())


class Sidebar(QWidget):
    """Focus banner, topic tree and the no-results message."""

    SEARCH_HINT = (
        "Search covers names, tags, bookmarks and added locations. Turn"
        " on Filenames to search inside topic folders."
    )

    clear_search_requested = pyqtSignal()
    show_all_locations_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.banner = QWidget()
        self.banner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        set_prop(self.banner, "role", "banner")
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(16, 10, 16, 10)
        banner_layout.setSpacing(8)
        self.focus_label = QLabel("")
        self.focus_label.setTextFormat(Qt.TextFormat.RichText)
        self.exit_focus_button = ghost_button("Exit focus")
        banner_layout.addWidget(self.focus_label, 1)
        banner_layout.addWidget(self.exit_focus_button)
        layout.addWidget(self.banner)

        self.tree = TopicTree()
        layout.addWidget(self.tree, 1)

        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(20, 32, 20, 32)
        empty_layout.setSpacing(8)
        empty_layout.addWidget(role_label("No topics match", "card-title"))
        self.empty_text = role_label(self.SEARCH_HINT, "muted", wrap=True)
        empty_layout.addWidget(self.empty_text)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        self.clear_search_button = secondary_button("Clear search")
        self.clear_search_button.clicked.connect(self.clear_search_requested)
        buttons.addWidget(self.clear_search_button)
        self.show_all_locations_button = secondary_button("Show all locations")
        self.show_all_locations_button.clicked.connect(
            self.show_all_locations_requested
        )
        buttons.addWidget(self.show_all_locations_button)
        buttons.addStretch()
        empty_layout.addLayout(buttons)
        empty_layout.addStretch()
        layout.addWidget(self.empty_state, 1)

        self.set_focus_label(None)
        self.set_no_results(False)

    def set_focus_label(self, name: str | None) -> None:
        self.banner.setVisible(bool(name))
        self.exit_focus_button.setVisible(bool(name))
        if name:
            escaped = name.replace("&", "&amp;").replace("<", "&lt;")
            self.focus_label.setText(f"Focused on <b>{escaped}</b>")

    def set_no_results(
        self,
        no_results: bool,
        query_active: bool = True,
        location_label: str | None = None,
    ) -> None:
        self.empty_state.setVisible(no_results)
        self.tree.setVisible(not no_results)
        if location_label:
            text = f"Nothing in {location_label} matches."
            if query_active:
                text = f"{text} {self.SEARCH_HINT}"
            self.empty_text.setText(text)
        else:
            self.empty_text.setText(self.SEARCH_HINT)
        self.clear_search_button.setVisible(query_active)
        self.show_all_locations_button.setVisible(bool(location_label))


class TopicTreeMixin:
    """Topic tree for MainWindow: filtering, sections, grouping and sub-hubs."""

    def _build_topic_tree(self) -> None:
        self.sidebar = Sidebar()
        self.tree = self.sidebar.tree
        self.exit_focus_button = self.sidebar.exit_focus_button
        self.exit_focus_button.clicked.connect(self.exit_subhub_focus)
        self.sidebar.clear_search_requested.connect(self.search_input.clear)
        self.sidebar.show_all_locations_requested.connect(
            lambda: self.set_location_filter(ALL_LOCATIONS)
        )
        self.tree.group_toggle_requested.connect(
            lambda: self.set_group_others_by_parent(not self._group_others_by_parent)
        )
        self.tree.itemSelectionChanged.connect(self.on_tree_selection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_tree_context_menu)
        self.tree.itemExpanded.connect(
            lambda item: self.on_section_expansion_changed(item, True)
        )
        self.tree.itemCollapsed.connect(
            lambda item: self.on_section_expansion_changed(item, False)
        )

    def _update_focus_banner(self) -> None:
        topic = self.topics_by_key.get(self.focused_hub_key or "")
        if not self.focused_hub_key:
            self.sidebar.set_focus_label(None)
        else:
            self.sidebar.set_focus_label(topic.name if topic else self.focused_hub_key)

    def apply_filter(self) -> None:
        query = self.search_input.text().strip()
        tokens = parse_search_query(query)
        include_filenames = self.filename_search_toggle.isChecked()
        self._active_tokens = tokens

        self.match_fields_by_key = {}
        topics = list(self.topics_by_key.items())
        location_active = not self.location_filter.is_all()
        if location_active:
            topics = [
                (key, topic)
                for key, topic in topics
                if topic_matches_location(topic, self.location_filter)
            ]
        narrowed = bool(tokens) or location_active
        if tokens:
            filtered: list[tuple[str, Topic]] = []
            for key, topic in topics:
                matched_fields = self.topic_matches(
                    key, topic, tokens, include_filenames
                )
                if matched_fields is None:
                    continue
                self.match_fields_by_key[key] = matched_fields
                filtered.append((key, topic))
            topics = filtered

        main_topics = [(key, topic) for key, topic in topics if topic.hub_key is None]
        sub_topics = [(key, topic) for key, topic in topics if topic.hub_key is not None]

        if self.focused_hub_key:
            sub_topics = [
                (key, topic)
                for key, topic in sub_topics
                if topic.hub_key == self.focused_hub_key
            ]
            focused_main = [
                (key, topic)
                for key, topic in main_topics
                if key == self.focused_hub_key
            ]
            if not focused_main and (
                not narrowed or sub_topics
            ) and self.focused_hub_key in self.topics_by_key:
                focused_main = [
                    (
                        self.focused_hub_key,
                        self.topics_by_key[self.focused_hub_key],
                    )
                ]
            main_topics = focused_main

        if narrowed and sub_topics:
            # A matched sub-topic needs its owning project visible to host it.
            present = {key for key, _topic in main_topics}
            for _key, topic in sub_topics:
                project_key = topic.hub_key
                if project_key not in present and project_key in self.topics_by_key:
                    main_topics.append(
                        (project_key, self.topics_by_key[project_key])
                    )
                    present.add(project_key)

        topics_by_state: dict[str, list[tuple[str, Topic]]] = {
            state: [] for state in STATE_ORDER
        }
        for key, topic in main_topics:
            topics_by_state[topic.display_state].append((key, topic))

        for state in topics_by_state:
            if state == OTHER_STATE:
                topics_by_state[state].sort(
                    key=lambda item: self.other_sort_key(item[1].name)
                )
            else:
                topics_by_state[state].sort(key=topic_sort_key)

        subtopics_by_hub: dict[str, dict[str, list[tuple[str, Topic]]]] = {}
        for key, topic in sub_topics:
            by_state = subtopics_by_hub.setdefault(topic.hub_key, {})
            by_state.setdefault(topic.display_state, []).append((key, topic))
        for by_state in subtopics_by_hub.values():
            for entries in by_state.values():
                entries.sort(key=topic_sort_key)
        self._subtopics_by_hub = subtopics_by_hub

        self.rebuild_tree(topics_by_state)

    def other_sort_key(self, name: str) -> tuple[int, int, str, str]:
        stripped = name.strip()
        if stripped:
            match = re.search(r"\d+", stripped)
            if match:
                try:
                    numeric = int(match.group(0))
                    return (0, -numeric, stripped.casefold(), stripped)
                except ValueError:
                    pass
        return (1, 0, stripped.casefold(), stripped)

    def topic_matches(
        self,
        topic_key: str,
        topic: Topic,
        tokens: list[SearchToken],
        include_filenames: bool,
    ) -> set[str] | None:
        bookmark_fields: list[str] = []
        for link in topic.manual_links:
            bookmark_fields.append(link.link_name)
            bookmark_fields.append(link.searchable_title)
            bookmark_fields.append(link.url)
        field_blobs = {
            FIELD_NAME: topic.name.casefold(),
            FIELD_TAGS: "\n".join(topic.tags).casefold(),
            FIELD_BOOKMARKS: " ".join(bookmark_fields).casefold(),
        }
        if topic.manual_locations:
            # One line per value, so a phrase cannot match across two entries.
            field_blobs[FIELD_LOCATIONS] = "\n".join(
                value
                for location in topic.manual_locations
                for value in (location.label, location.target)
            ).casefold()
        if include_filenames:
            field_blobs[FIELD_FILENAMES] = self.get_topic_filename_blob(topic_key)
        return match_topic(field_blobs, tokens)

    def rebuild_tree(self, topics_by_state: dict[str, list[tuple[str, Topic]]]) -> None:
        query_active = bool(self.search_input.text().strip())
        location_active = not self.location_filter.is_all()
        # Searching or filtering by location: show only what matched.
        narrowed = query_active or location_active
        # A rebuild must not collapse a multi-selection to one topic.
        previously_selected = self.selected_topic_keys()
        self._rebuilding_tree = True
        try:
            self.tree.clear()
            self.tree_items_by_key = {}

            self._update_focus_banner()
            self.sidebar.set_no_results(
                narrowed and not any(topics_by_state.values()),
                query_active=query_active,
                location_label=self.location_filter.label if location_active else None,
            )
            if self.focused_hub_key:
                self._rebuild_focused_tree(topics_by_state)
                if (
                    self.current_topic_key
                    and self.current_topic_key in self.tree_items_by_key
                ):
                    self.tree.setCurrentItem(
                        self.tree_items_by_key[self.current_topic_key]
                    )
                    self.show_topic_details(self.current_topic_key)
                else:
                    self.current_topic_key = self.focused_hub_key
                    if self.current_topic_key in self.tree_items_by_key:
                        self.tree.setCurrentItem(
                            self.tree_items_by_key[self.current_topic_key]
                        )
                    self.show_topic_details(self.current_topic_key)
                self.tree.expandAll()
                self._restore_multi_selection(previously_selected)
                return

            for state in STATE_ORDER:
                state_topics = topics_by_state.get(state, [])
                if narrowed and not state_topics:
                    # While searching, sections without a match are hidden.
                    continue
                group_action = None
                if state == OTHER_STATE and state_topics:
                    group_action = (
                        "Ungroup" if self._group_others_by_parent else "Group"
                    )
                parent = self._make_section_item(
                    state, len(state_topics), group_action=group_action
                )
                parent.setData(0, ROLE_SECTION, state)
                self.tree.addTopLevelItem(parent)

                if (
                    state == OTHER_STATE
                    and self._group_others_by_parent
                    and state_topics
                ):
                    for label, parent_path, items in self.group_other_topics(
                        state_topics
                    ):
                        group_item = QTreeWidgetItem([f"{label} ({len(items)})"])
                        group_item.setFlags(
                            group_item.flags() & ~Qt.ItemFlag.ItemIsSelectable
                        )
                        group_item.setData(0, ROLE_KIND, KIND_GROUP)
                        group_item.setData(
                            0, ROLE_META, {"label": label, "count": len(items)}
                        )
                        group_item.setToolTip(0, parent_path)
                        group_item.setData(
                            0,
                            Qt.ItemDataRole.UserRole + 1,
                            f"{OTHER_STATE}::{parent_path}",
                        )
                        parent.addChild(group_item)
                        for key, topic in items:
                            group_item.addChild(
                                self._make_topic_item(key, topic, indent=True)
                            )
                else:
                    for key, topic in state_topics:
                        item = self._make_topic_item(key, topic)
                        parent.addChild(item)
                        self._add_subhub_sections(item, key)

            if (
                self.current_topic_key
                and self.current_topic_key in self.tree_items_by_key
            ):
                # setCurrentItem auto-expands collapsed ancestors, so the saved
                # expansion must be applied after it, inside the rebuild guard.
                self.tree.setCurrentItem(
                    self.tree_items_by_key[self.current_topic_key]
                )
                self.show_topic_details(self.current_topic_key)
            else:
                self.current_topic_key = None
                self.show_topic_details(None)

            if narrowed:
                self.tree.expandAll()
            else:
                self._apply_saved_expansion()
            self._restore_multi_selection(previously_selected)
        finally:
            self._rebuilding_tree = False

    def _restore_multi_selection(self, keys: list[str]) -> None:
        items = [self.tree_items_by_key[key] for key in keys if key in self.tree_items_by_key]
        if len(items) < 2:
            return
        for item in items:
            item.setSelected(True)
        self.show_topic_details(self.current_topic_key)

    def _rebuild_focused_tree(
        self, topics_by_state: dict[str, list[tuple[str, Topic]]]
    ) -> None:
        if not self.focused_hub_key:
            return
        focused_entry: tuple[str, Topic] | None = None
        for entries in topics_by_state.values():
            for key, topic in entries:
                if key == self.focused_hub_key:
                    focused_entry = (key, topic)
                    break
            if focused_entry:
                break
        if not focused_entry:
            return
        key, topic = focused_entry
        root_item = self._make_topic_item(key, topic)
        self.tree.addTopLevelItem(root_item)
        self._add_subhub_sections(root_item, key)

    def _add_subhub_sections(self, item: QTreeWidgetItem, project_key: str) -> None:
        by_state = self._subtopics_by_hub.get(project_key)
        if not by_state:
            return
        item.setData(
            0,
            Qt.ItemDataRole.UserRole + 1,
            f"{HUB_ROOT_EXPANSION_PREFIX}{project_key}",
        )
        for state in STATE_ORDER:
            if state == OTHER_STATE:
                continue
            entries = by_state.get(state, [])
            if not entries:
                continue
            section_item = self._make_section_item(state, len(entries), indent=True)
            section_item.setData(
                0, Qt.ItemDataRole.UserRole + 1, f"{project_key}::{state}"
            )
            item.addChild(section_item)
            for sub_key, sub_topic in entries:
                section_item.addChild(
                    self._make_topic_item(sub_key, sub_topic, indent=True)
                )

    def _apply_saved_expansion(self) -> None:
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            expansion_key = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if expansion_key:
                key = str(expansion_key)
                # Sub-hub project nodes start collapsed to keep sections tidy.
                default = not key.startswith(HUB_ROOT_EXPANSION_PREFIX)
                item.setExpanded(self._section_expanded.get(key, default))
            iterator += 1

    def on_section_expansion_changed(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if self._rebuilding_tree:
            return
        if self.search_input.text().strip() or not self.location_filter.is_all():
            # Expansion while narrowed is forced open; it is not a preference.
            return
        section = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if section:
            self._section_expanded[str(section)] = expanded
            self._save_expanded_sections()

    def _save_expanded_sections(self) -> None:
        QSettings().setValue(
            "session/expanded_sections", json.dumps(self._section_expanded)
        )

    def _make_section_item(
        self,
        state: str,
        count: int,
        indent: bool = False,
        group_action: str | None = None,
    ) -> QTreeWidgetItem:
        label = state.upper()
        item = QTreeWidgetItem([f"{label} ({count})"])
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        item.setData(0, ROLE_KIND, KIND_SECTION)
        item.setData(
            0,
            ROLE_META,
            {
                "label": label,
                "count": count,
                "indent": indent,
                "group_action": group_action,
            },
        )
        if group_action:
            item.setToolTip(0, "Group by parent folder")
        return item

    def _make_topic_item(
        self, key: str, topic: Topic, indent: bool = False
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem([topic.name])
        prefix, rest = split_topic_prefix(topic.name)
        tooltips: list[str] = []
        if topic.has_inconsistency:
            tooltips.append("Found in " + " and ".join(ordered_states(topic.states)))
        item.setData(0, ROLE_KEY, key)
        matched_fields = self.match_fields_by_key.get(key)
        note = ""
        if matched_fields:
            tooltips.append(f"Matched in: {format_match_fields(matched_fields)}")
            # Only worth a note when the name itself is not what matched.
            if FIELD_NAME not in matched_fields:
                note = f"in {format_match_fields(matched_fields)}"
        item.setData(0, ROLE_KIND, KIND_TOPIC)
        item.setData(
            0,
            ROLE_META,
            {
                "prefix": prefix,
                "rest": rest,
                "note": note,
                "hub": key in self.hubs_by_project_key,
                "pinned": topic.pinned_rank is not None,
                "warn": topic.has_inconsistency,
                "indent": indent,
            },
        )
        if tooltips:
            item.setToolTip(0, "\n".join(tooltips))
        self.tree_items_by_key[key] = item
        return item

    def group_other_topics(
        self, state_topics: list[tuple[str, Topic]]
    ) -> list[tuple[str, str, list[tuple[str, Topic]]]]:
        items_by_parent: dict[str, list[tuple[str, Topic]]] = {}
        for key, topic in state_topics:
            parents = sorted(
                {
                    str(location.path.parent)
                    for location in topic.locations
                    if location.state == OTHER_STATE
                }
            )
            if not parents:
                parents = [""]
            for parent_path in parents:
                items_by_parent.setdefault(parent_path, []).append((key, topic))

        config_order = {
            str(root): index for index, root in enumerate(self.config.other_folders)
        }
        ordered_paths = sorted(
            items_by_parent,
            key=lambda path: (
                config_order.get(path, len(config_order)),
                path.casefold(),
            ),
        )

        base_labels = {
            path: (Path(path).name or path or "Unknown") for path in ordered_paths
        }
        label_counts = Counter(base_labels.values())
        groups: list[tuple[str, str, list[tuple[str, Topic]]]] = []
        for path in ordered_paths:
            label = base_labels[path]
            if label_counts[label] > 1 and path:
                parent_dir = Path(path)
                label = f"{parent_dir.parent.name}/{parent_dir.name}"
            groups.append((label, path, items_by_parent[path]))
        return groups

    def on_tree_selection(self) -> None:
        item = self.tree.currentItem()
        if not item:
            return
        topic_key = item.data(0, Qt.ItemDataRole.UserRole)
        if not topic_key:
            return
        self.current_topic_key = topic_key
        self.show_topic_details(topic_key)

    def selected_topic_keys(self) -> list[str]:
        keys: list[str] = []
        for item in self.tree.selectedItems():
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if key:
                keys.append(key)
        return keys

    def get_selected_topics(self) -> list[tuple[str, Topic]]:
        topics: list[tuple[str, Topic]] = []
        for key in self.selected_topic_keys():
            topic = self.topics_by_key.get(key)
            if topic:
                topics.append((key, topic))
        return topics

    def show_tree_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        topic_key = item.data(0, Qt.ItemDataRole.UserRole)
        if not topic_key:
            if item.data(0, Qt.ItemDataRole.UserRole + 1) == OTHER_STATE:
                self._show_others_header_menu(pos)
            return
        if topic_key not in self.selected_topic_keys():
            self.tree.setCurrentItem(item)

        menu = QMenu(self.tree)
        selected = self.get_selected_topics()
        if len(selected) > 1:
            self._populate_multi_topic_menu(menu, selected)
        else:
            self._populate_topic_menu(menu, topic_key)
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _show_others_header_menu(self, pos) -> None:
        menu = QMenu(self.tree)
        group_action = menu.addAction("Group by parent folder")
        group_action.setCheckable(True)
        group_action.setChecked(self._group_others_by_parent)
        group_action.toggled.connect(self.set_group_others_by_parent)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def set_group_others_by_parent(self, enabled: bool) -> None:
        if enabled == self._group_others_by_parent:
            return
        self._group_others_by_parent = enabled
        QSettings().setValue("view/others_group_by_parent", enabled)
        self.apply_filter()

    def _populate_multi_topic_menu(
        self, menu: QMenu, selected: list[tuple[str, Topic]]
    ) -> None:
        count = len(selected)
        move_menu = self._add_move_submenu(menu, f"Move {count} topics to")
        move_menu.setEnabled(
            any(self.topic_action_flags(key, topic)[0] for key, topic in selected)
        )
        archive_action = menu.addAction(f"Archive {count} topics…")
        archive_action.setEnabled(
            any(self.topic_action_flags(key, topic)[1] for key, topic in selected)
        )
        archive_action.triggered.connect(self.archive_selected_topics)
        menu.addSeparator()
        tag_action = menu.addAction(f"Add Tag to {count} topics…")
        tag_action.triggered.connect(self.add_tag_to_selected_topics)

    def _add_move_submenu(self, menu: QMenu, title: str) -> QMenu:
        """Projects / Areas / Resources / Archive, acting on the tree selection."""
        submenu = menu.addMenu(title)
        for state, folder in STATE_FOLDERS.items():
            action = submenu.addAction(f"{state}\t{folder}")
            action.triggered.connect(
                lambda _checked=False, target=state: self.move_topics(
                    self.get_selected_topics(), target
                )
            )
        return submenu

    def _populate_topic_menu(self, menu: QMenu, topic_key: str) -> None:
        topic = self.topics_by_key.get(topic_key)
        if not topic:
            return

        filesystem_paths = sorted(
            (str(loc.path) for loc in topic.locations if loc.source == "filesystem"),
            key=str.casefold,
        )
        obsidian_paths = sorted(
            (str(loc.path) for loc in topic.locations if loc.source == "obsidian"),
            key=str.casefold,
        )
        for path in filesystem_paths:
            label = "Open folder" if len(filesystem_paths) == 1 else f"Open folder: {path}"
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=path: self.open_path(value)
            )
        for path in obsidian_paths:
            label = (
                "Open in Obsidian"
                if len(obsidian_paths) == 1
                else f"Open in Obsidian: {path}"
            )
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=path: self.open_obsidian_path(value)
            )
        copy_action = menu.addAction("Copy title")
        copy_action.triggered.connect(self.copy_topic_title_to_clipboard)

        if not is_other_key(topic_key):
            pin_action = menu.addAction(
                "Unpin" if topic.pinned_rank is not None else "Pin"
            )
            pin_action.triggered.connect(
                lambda _checked=False, key=topic_key: self.toggle_pin_topic(key)
            )
            if topic_key in self.hubs_by_project_key:
                focus_action = menu.addAction("Focus")
                focus_action.triggered.connect(
                    lambda _checked=False, key=topic_key: self.open_subhub_focus(key)
                )

        menu.addSeparator()
        can_move, can_archive, can_rename = self.topic_action_flags(topic_key, topic)
        move_menu = self._add_move_submenu(menu, "Move to")
        move_menu.setEnabled(can_move)
        archive_action = menu.addAction("Archive…")
        archive_action.setEnabled(can_archive)
        archive_action.triggered.connect(self.archive_selected_topics)
        rename_action = menu.addAction("Rename…")
        rename_action.setEnabled(can_rename)
        rename_action.triggered.connect(self.rename_topic)
        tag_action = menu.addAction("Add Tag…")
        tag_action.triggered.connect(self.add_topic_tag)

        source_menu = QMenu("Add to Source", menu)
        if self.populate_json_source_menu(source_menu, topic):
            menu.addSeparator()
            menu.addMenu(source_menu)
            self._add_open_source_actions(menu, self.hub_for_topic(topic))
