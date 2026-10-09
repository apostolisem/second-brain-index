from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QTreeWidgetItemIterator,
    QWidget,
    QWidgetAction,
)

from ...config import HubConfig
from ...indexer import SOURCE_SPECS, SourceSpec
from ...location_filter import (
    ALL_LOCATIONS,
    KIND_ROOT,
    NO_FOLDER,
    LocationFilter,
    topic_matches_location,
)
from ...theme import METRICS, BlueprintFrame, ThemeManager, ThemeToggleButton, set_prop
from .common import current_theme, role_label, tracked


class SearchField(QLineEdit):
    """Header search: leading icon, and a "Ctrl K" hint while empty."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        theme = current_theme()
        self.setPlaceholderText("Search topics, tags, bookmarks…  try tag:client")
        self.setMaximumWidth(560)
        self.setFixedHeight(METRICS["control_height"])
        self.setClearButtonEnabled(True)
        set_prop(self, "role", "search")
        self._search_action = self.addAction(
            theme.icon("search", "subtle"), QLineEdit.ActionPosition.LeadingPosition
        )
        self._hint = role_label("Ctrl K", "hint")
        self._hint.setParent(self)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._hint.adjustSize()
        self.textChanged.connect(self._sync_hint)
        self._sync_hint()

    def apply_theme(self) -> None:
        self._search_action.setIcon(current_theme().icon("search", "subtle"))

    def _sync_hint(self) -> None:
        # The clear button takes the same corner once there is text, and a
        # narrow field has no room for the hint beside its placeholder.
        self._hint.setVisible(not self.text() and self.width() >= 320)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_hint()
        self._hint.adjustSize()
        self._hint.move(
            self.width() - self._hint.width() - 8,
            (self.height() - self._hint.height()) // 2,
        )


class HeaderBar(QWidget):
    """Brand, search, Filenames and Location filters, refresh, Sources, Init Sub-Hub, theme."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        theme = current_theme()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        set_prop(self, "role", "header")
        self.setFixedHeight(METRICS["header_height"])
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(14)

        brand_block = QWidget()
        brand_block.setFixedWidth(260)
        brand_layout = QHBoxLayout(brand_block)
        # The frame's registration marks hang 6px outside its 30px box.
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(4)
        frame = BlueprintFrame()
        side = 30 + 2 * BlueprintFrame.OUTSET
        frame.setFixedSize(side, side)
        frame_layout = QHBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        self._brand_icon = QLabel()
        self._brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        frame_layout.addWidget(self._brand_icon)
        brand = role_label("Second Brain Hub", "brand")
        brand_layout.addWidget(frame)
        brand_layout.addWidget(brand)
        brand_layout.addStretch()
        layout.addWidget(brand_block)

        self.search_input = SearchField()
        layout.addWidget(self.search_input, 1)

        self.filename_search_toggle = QPushButton("Filenames")
        self.filename_search_toggle.setCheckable(True)
        self.filename_search_toggle.setChecked(True)
        theme.bind_icon(self.filename_search_toggle, "file-text")
        layout.addWidget(self.filename_search_toggle)
        self.filename_search_toggle.toggled.connect(self._sync_filename_toggle)
        self._sync_filename_toggle(self.filename_search_toggle.isChecked())

        self.location_button = QToolButton()
        self.location_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.location_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        # Checked (tinted) while a location filter is on; never toggled by a click.
        self.location_button.setCheckable(True)
        theme.bind_icon(self.location_button, "folder-open")
        self.location_menu = QMenu(self.location_button)
        self.location_menu.setToolTipsVisible(True)
        self.location_button.setMenu(self.location_menu)
        layout.addWidget(self.location_button)
        self.set_location(ALL_LOCATIONS)
        layout.addStretch()

        self.refresh_button = QPushButton()
        self.refresh_button.setToolTip("Refresh (F5)")
        set_prop(self.refresh_button, "variant", "icon-ghost")
        theme.bind_icon(self.refresh_button, "refresh-cw", "muted", 17)
        layout.addWidget(self.refresh_button)

        self.sources_button = QToolButton()
        self.sources_button.setText("Sources")
        self.sources_button.setToolTip(
            "Open the OneNote, Outlook and PLM topic lists, or add an entry to one"
        )
        self.sources_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        theme.bind_icon(self.sources_button, "database")
        self.sources_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.sources_menu = QMenu(self.sources_button)
        self.sources_menu.setToolTipsVisible(True)
        self.sources_button.setMenu(self.sources_menu)
        layout.addWidget(self.sources_button)

        self.initialize_subhub_button = QPushButton("Init Sub-Hub")
        theme.bind_icon(self.initialize_subhub_button, "plus")
        layout.addWidget(self.initialize_subhub_button)

        self.theme_toggle = ThemeToggleButton()
        layout.addWidget(self.theme_toggle)
        self.apply_theme()

    def _sync_filename_toggle(self, checked: bool) -> None:
        """Spell the state out and back it with the fill: tinted when on, plain when off."""
        toggle = self.filename_search_toggle
        toggle.setText("Filenames on" if checked else "Filenames off")
        if checked:
            toggle.setToolTip(
                "Filename search is on: files inside topic folders are searched too."
                " Click to turn off."
            )
        else:
            toggle.setToolTip(
                "Filename search is off: only names, tags, bookmarks and locations are searched."
                " Click to turn on."
            )
        set_prop(toggle, "state", "on" if checked else "off")

    LOCATION_LABEL_MAX = 28

    def set_location(self, location: LocationFilter) -> None:
        button = self.location_button
        label = location.label
        if len(label) > self.LOCATION_LABEL_MAX:
            label = label[: self.LOCATION_LABEL_MAX - 1] + "…"
        button.setText(label)
        button.setChecked(not location.is_all())
        if location.is_all():
            button.setToolTip("Filter topics by where they live")
        else:
            detail = f"\n{location.tooltip}" if location.tooltip else ""
            button.setToolTip(
                f"Showing topics in {location.label}{detail}\nPick All locations to clear"
            )

    def apply_theme(self) -> None:
        theme = current_theme()
        self._brand_icon.setPixmap(theme.icon("network", "accent").pixmap(18, 18))
        self.search_input.apply_theme()


class HeaderBarMixin:
    """Header bar for MainWindow: brand, search, refresh, sources and theme."""

    def _build_header_bar(self, theme: ThemeManager) -> QWidget:
        header = HeaderBar()
        self.header_bar = header
        self.search_input = header.search_input
        self.filename_search_toggle = header.filename_search_toggle
        self.location_button = header.location_button
        self.location_menu = header.location_menu
        self.refresh_button = header.refresh_button
        self.sources_button = header.sources_button
        self.sources_menu = header.sources_menu
        self.initialize_subhub_button = header.initialize_subhub_button

        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(250)
        self._search_debounce.timeout.connect(self.apply_filter)
        self.search_input.textChanged.connect(self._schedule_filter)
        self.filename_search_toggle.toggled.connect(self.apply_filter)
        self.refresh_button.clicked.connect(
            lambda _checked=False: self.refresh_index()
        )
        self.sources_menu.aboutToShow.connect(self.populate_sources_menu)
        self.location_menu.aboutToShow.connect(self.populate_location_menu)
        # A press on the button must never leave its checked state out of sync.
        self.location_menu.aboutToHide.connect(self._sync_location_button)
        self.initialize_subhub_button.clicked.connect(self.initialize_subhub)
        return header

    def _apply_theme_icons(self) -> None:
        self.header_bar.apply_theme()

    def focus_search(self) -> None:
        self.search_input.setFocus()
        self.search_input.selectAll()

    def focus_first_result(self) -> None:
        if self._search_debounce.isActive():
            self._search_debounce.stop()
            self.apply_filter()
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            if item.data(0, Qt.ItemDataRole.UserRole):
                self.tree.setCurrentItem(item)
                self.tree.setFocus()
                return
            iterator += 1

    def _schedule_filter(self) -> None:
        self._search_debounce.start()

    def _sync_location_button(self) -> None:
        self.header_bar.set_location(self.location_filter)

    def _location_count_topics(self) -> list:
        """The topics the counts are taken over: all of them, or the focused sub-hub."""
        if not self.focused_hub_key:
            return list(self.topics_by_key.values())
        return [
            topic
            for key, topic in self.topics_by_key.items()
            if key == self.focused_hub_key or topic.hub_key == self.focused_hub_key
        ]

    def populate_location_menu(self) -> None:
        menu = self.location_menu
        menu.clear()
        options = self.location_options()
        topics = self._location_count_topics()

        def add(location: LocationFilter) -> None:
            count = sum(1 for topic in topics if topic_matches_location(topic, location))
            text = f"{location.label}\t{count}"
            enabled = True
            if location.kind == KIND_ROOT and Path(location.value) in self.missing_roots:
                text = f"{location.label}  (not found)"
                enabled = False
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(location == self.location_filter)
            action.setEnabled(enabled or location == self.location_filter)
            if location.tooltip:
                action.setToolTip(location.tooltip)
            action.triggered.connect(
                lambda _checked=False, value=location: self.set_location_filter(value)
            )

        add(ALL_LOCATIONS)
        for title, group in (
            ("Sources", options.sources),
            ("Roots", options.roots),
            ("Sub-hub roots", options.subhub_roots),
        ):
            if not group:
                continue
            menu.addSeparator()
            # Styled like the Move menu's heading; addSection's title is not
            # drawn by every style.
            heading = QWidgetAction(menu)
            label = tracked(role_label(title.upper(), "kicker-muted"))
            label.setContentsMargins(14, 6, 14, 2)
            heading.setDefaultWidget(label)
            menu.addAction(heading)
            for location in group:
                add(location)
        menu.addSeparator()
        add(NO_FOLDER)

    def open_source_json(self, spec: SourceSpec, hub: HubConfig) -> None:
        self.open_path(str(spec.topics_path(hub)))

    def _add_open_source_actions(
        self, menu: QMenu, hub: HubConfig, label_suffix: str = ""
    ) -> int:
        menu.setToolTipsVisible(True)
        count = 0
        for spec in SOURCE_SPECS:
            if not spec.enabled(hub):
                continue
            path = spec.topics_path(hub)
            label = f"Open {spec.name} list{label_suffix}"
            if path.is_file():
                action = menu.addAction(label)
                action.triggered.connect(
                    lambda _checked=False, s=spec, h=hub: self.open_source_json(
                        s, h
                    )
                )
            else:
                action = menu.addAction(f"{label} (not found)")
                action.setEnabled(False)
            action.setToolTip(str(path))
            count += 1
        return count

    def _add_new_entry_actions(
        self, menu: QMenu, hub: HubConfig, hub_key: str | None, label_suffix: str = ""
    ) -> int:
        count = 0
        for spec in SOURCE_SPECS:
            if not spec.enabled(hub):
                continue
            action = menu.addAction(f"New {spec.name} entry…{label_suffix}")
            action.setToolTip(f"Add a topic to {spec.topics_path(hub).name}")
            action.triggered.connect(
                lambda _checked=False, s=spec, h=hub, k=hub_key: self.create_source_entry(
                    s, h, k
                )
            )
            count += 1
        return count

    def _active_subhub_key(self) -> str | None:
        topic = (
            self.topics_by_key.get(self.current_topic_key)
            if self.current_topic_key
            else None
        )
        if topic and topic.hub_key:
            return topic.hub_key
        if self.current_topic_key in self.hubs_by_project_key:
            return self.current_topic_key
        return self.focused_hub_key

    def _any_sources_enabled(self) -> bool:
        hubs = [self.config.root_hub, *self.hubs_by_project_key.values()]
        return any(spec.enabled(hub) for hub in hubs for spec in SOURCE_SPECS)

    def populate_sources_menu(self) -> None:
        self.sources_menu.clear()
        count = self._add_open_source_actions(
            self.sources_menu, self.config.root_hub
        )
        subhub_key = self._active_subhub_key()
        subhub = self.hubs_by_project_key.get(subhub_key) if subhub_key else None
        subhub_suffix = ""
        if subhub is not None:
            subhub_topic = self.topics_by_key.get(subhub_key)
            name = subhub_topic.name if subhub_topic else subhub_key
            subhub_suffix = f" — {name}"
            if count:
                self.sources_menu.addSeparator()
            count += self._add_open_source_actions(
                self.sources_menu, subhub, label_suffix=subhub_suffix
            )
        if count:
            self.sources_menu.addSeparator()
            self._add_new_entry_actions(self.sources_menu, self.config.root_hub, None)
            if subhub is not None:
                self._add_new_entry_actions(
                    self.sources_menu, subhub, subhub_key, subhub_suffix
                )
        if count == 0:
            placeholder = self.sources_menu.addAction("No source lists enabled")
            placeholder.setEnabled(False)
