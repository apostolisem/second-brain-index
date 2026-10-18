from __future__ import annotations

from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...models import ManualLink
from ...search import SearchToken, parse_search_query
from ...theme import METRICS, set_prop
from .common import (
    RowHoverTable,
    current_theme,
    ghost_button,
    make_delete_button,
    make_edit_button,
    role_label,
    secondary_button,
    section_label,
)

EMPTY_TOPIC_TEXT = (
    "No bookmarks yet. Add links to files, sites, emails or Obsidian notes for"
    " this topic."
)
EMPTY_FILTER_TEXT = "No bookmarks match this filter."


class BookmarksPanel(QWidget):
    """BOOKMARKS section: filter, checkbox selection strip and the table."""

    CHECK_COLUMN = 0
    NAME_COLUMN = 1
    TARGET_COLUMN = 2
    TYPE_COLUMN = 3
    ACTIONS_COLUMN = 4

    def __init__(
        self,
        on_add,
        on_edit,
        on_delete,
        on_open,
        on_move=None,
        on_delete_many=None,
        on_copy=None,
    ) -> None:
        super().__init__()
        self._row_height = METRICS["tree_row_height"]
        self._on_add = on_add
        self._on_edit = on_edit
        self._on_delete = on_delete
        self._on_open = on_open
        self._on_move = on_move
        self._on_copy = on_copy
        self._on_delete_many = on_delete_many
        self._links: list[ManualLink] = []
        self._active_filter = ""
        self._filter_tokens: list[SearchToken] = []
        self._syncing = False
        self.checked_ids: set[int] = set()
        theme = current_theme()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        header_row.addWidget(section_label("Bookmarks"))
        self.count_label = role_label("", "count")
        header_row.addWidget(self.count_label)
        header_row.addStretch()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter bookmarks")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedSize(240, METRICS["compact_control_height"])
        self.search_input.setStyleSheet("font-size: 13px; padding: 4px 10px 4px 6px;")
        self._filter_action = self.search_input.addAction(
            theme.icon("search", "subtle"), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search_input.textChanged.connect(self._on_filter_text_changed)
        header_row.addWidget(self.search_input)
        self.add_button = secondary_button("Add bookmark", "plus")
        self.add_button.clicked.connect(self._on_add)
        header_row.addWidget(self.add_button)
        layout.addLayout(header_row)

        self.selection_strip = QWidget()
        self.selection_strip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        set_prop(self.selection_strip, "role", "strip")
        strip_layout = QHBoxLayout(self.selection_strip)
        strip_layout.setContentsMargins(10, 6, 10, 6)
        strip_layout.setSpacing(8)
        self.selection_label = QLabel("")
        self.move_selected_button = ghost_button("Move to topic…")
        self.copy_selected_button = ghost_button("Copy to topic…")
        self.delete_selected_button = ghost_button("Delete")
        self.clear_selected_button = ghost_button("Clear")
        self.move_selected_button.clicked.connect(self._move_checked)
        self.copy_selected_button.clicked.connect(self._copy_checked)
        self.delete_selected_button.clicked.connect(self._delete_checked)
        self.clear_selected_button.clicked.connect(self.clear_checked)
        strip_layout.addWidget(self.selection_label, 1)
        strip_layout.addWidget(self.move_selected_button)
        strip_layout.addWidget(self.copy_selected_button)
        strip_layout.addWidget(self.delete_selected_button)
        strip_layout.addWidget(self.clear_selected_button)
        self.move_selected_button.setVisible(on_move is not None)
        self.copy_selected_button.setVisible(on_copy is not None)
        layout.addWidget(self.selection_strip)

        self.table = RowHoverTable(0, 5)
        self.table.setHorizontalHeaderLabels(["", "NAME", "TARGET", "TYPE", ""])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(self.CHECK_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.NAME_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.TARGET_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.TYPE_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.ACTIONS_COLUMN, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.CHECK_COLUMN, 34)
        self.table.setColumnWidth(self.TYPE_COLUMN, 90)
        self.table.setColumnWidth(self.ACTIONS_COLUMN, 84)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Rows are highlighted by their checkbox, never by clicking the row.
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setShowGrid(False)
        # Every row is shown; the details page does the scrolling.
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.verticalHeader().setDefaultSectionSize(self._row_height)
        self.table.verticalHeader().setMinimumSectionSize(self._row_height)
        # The checkbox column ticks the row; a click anywhere else opens it.
        self.table.inert_columns = {self.CHECK_COLUMN}
        self.table.row_activated.connect(self._open_row)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        for sequence in ("Return", "Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self._open_current_row)
        layout.addWidget(self.table)

        # Lives in the header's first section: selects or clears all visible rows.
        self.select_all_box = QCheckBox(header)
        self.select_all_box.setToolTip("Select all")
        self.select_all_box.clicked.connect(self._on_select_all)
        header.geometriesChanged.connect(self._place_select_all)

        self.empty_label = role_label("", "muted", wrap=True)
        self.empty_label.setContentsMargins(0, 8, 0, 8)
        layout.addWidget(self.empty_label)
        self._refresh_chrome()

    # -- public ------------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self.add_button.setEnabled(enabled)
        self.search_input.setEnabled(enabled)
        self.table.setEnabled(enabled)

    def set_links(self, links: list[ManualLink]) -> None:
        self._links = links
        known = {link.id for link in links}
        self.checked_ids &= known
        theme = current_theme()
        self._filter_action.setIcon(theme.icon("search", "subtle"))
        name_font = QFont(self.table.font())
        name_font.setWeight(QFont.Weight.Medium)
        self._syncing = True
        try:
            self.table.setRowCount(len(links))
            for row, link in enumerate(links):
                check_item = QTableWidgetItem()
                check_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                )
                check_item.setCheckState(
                    Qt.CheckState.Checked
                    if link.id in self.checked_ids
                    else Qt.CheckState.Unchecked
                )
                self.table.setItem(row, self.CHECK_COLUMN, check_item)

                name_item = QTableWidgetItem(link.link_name)
                name_item.setFont(name_font)
                name_item.setToolTip("Open")
                self.table.setItem(row, self.NAME_COLUMN, name_item)

                target_item = QTableWidgetItem(link.url)
                target_item.setToolTip(link.url)
                target_item.setForeground(self.palette().placeholderText())
                self.table.setItem(row, self.TARGET_COLUMN, target_item)

                type_host = QWidget()
                # The chip is only a label: clicks on it belong to the row.
                type_host.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
                )
                type_layout = QHBoxLayout(type_host)
                type_layout.setContentsMargins(8, 0, 0, 0)
                type_layout.addWidget(
                    role_label(link.link_type, "tag-neutral"),
                    0,
                    Qt.AlignmentFlag.AlignVCenter,
                )
                type_layout.addStretch()
                self.table.setCellWidget(row, self.TYPE_COLUMN, type_host)

                edit_button = make_edit_button()
                edit_button.clicked.connect(
                    lambda _checked=False, link_item=link: self._on_edit(link_item)
                )
                delete_button = make_delete_button()
                delete_button.clicked.connect(
                    lambda _checked=False, link_item=link: self._on_delete(link_item)
                )
                action_container = QWidget()
                action_layout = QHBoxLayout(action_container)
                action_layout.setContentsMargins(0, 0, 0, 0)
                action_layout.setSpacing(2)
                action_layout.addStretch()
                action_layout.addWidget(edit_button)
                action_layout.addWidget(delete_button)
                self.table.setCellWidget(row, self.ACTIONS_COLUMN, action_container)
                self._row_height = max(
                    self._row_height, action_container.sizeHint().height()
                )
        finally:
            self._syncing = False
        self._apply_uniform_row_height()
        self._apply_filter()

    def reset_view(self) -> None:
        """Forget checked rows and the filter; used when the topic changes."""
        self.checked_ids.clear()
        self.search_input.clear()

    def checked_links(self) -> list[ManualLink]:
        return [link for link in self._links if link.id in self.checked_ids]

    def selected_links(self) -> list[ManualLink]:
        return self.checked_links()

    def set_checked(self, link_ids: set[int]) -> None:
        self.checked_ids = set(link_ids) & {link.id for link in self._links}
        self._sync_checks()

    def clear_checked(self) -> None:
        self.set_checked(set())

    # -- internals ---------------------------------------------------------
    def _visible_rows(self) -> list[int]:
        return [
            row for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)
        ]

    def _on_filter_text_changed(self, text: str) -> None:
        self._active_filter = text.strip().casefold()
        self._filter_tokens = parse_search_query(text)
        self._apply_filter()

    def _apply_filter(self) -> None:
        for row, link in enumerate(self._links):
            # Every typed word must appear, in any order, as in the main search.
            # One line per field, so a quoted phrase cannot span two of them.
            text = "\n".join(
                (link.link_name, link.searchable_title, link.url)
            ).casefold()
            visible = all(token.text in text for token in self._filter_tokens)
            self.table.setRowHidden(row, not visible)
        self._update_table_height()
        self._sync_checks()

    def _sync_checks(self) -> None:
        """Make checkboxes, row highlight and the strip agree with checked_ids."""
        self._syncing = True
        try:
            selection = self.table.selectionModel()
            selection.clearSelection()
            for row, link in enumerate(self._links):
                checked = link.id in self.checked_ids
                item = self.table.item(row, self.CHECK_COLUMN)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                    )
                if checked:
                    selection.select(
                        self.table.model().index(row, 0),
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
        finally:
            self._syncing = False
        self._refresh_chrome()

    def _refresh_chrome(self) -> None:
        total = len(self._links)
        visible = self._visible_rows()
        if self._active_filter and len(visible) != total:
            self.count_label.setText(f"{len(visible)} of {total}")
        else:
            self.count_label.setText(str(total) if total else "")
        count = len(self.checked_ids)
        self.selection_label.setText(f"{count} selected")
        self.selection_strip.setVisible(count > 0)
        visible_ids = {self._links[row].id for row in visible}
        self.select_all_box.setEnabled(bool(visible_ids))
        self.select_all_box.setChecked(
            bool(visible_ids) and visible_ids <= self.checked_ids
        )
        if not total:
            self.empty_label.setText(EMPTY_TOPIC_TEXT)
        elif not visible:
            self.empty_label.setText(EMPTY_FILTER_TEXT)
        self.empty_label.setVisible(not visible)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._syncing or item.column() != self.CHECK_COLUMN:
            return
        row = item.row()
        if not 0 <= row < len(self._links):
            return
        link_id = self._links[row].id
        if item.checkState() == Qt.CheckState.Checked:
            self.checked_ids.add(link_id)
        else:
            self.checked_ids.discard(link_id)
        self._sync_checks()

    def _on_select_all(self, checked: bool) -> None:
        visible_ids = {self._links[row].id for row in self._visible_rows()}
        if checked:
            self.checked_ids |= visible_ids
        else:
            self.checked_ids -= visible_ids
        self._sync_checks()

    def _place_select_all(self) -> None:
        header = self.table.horizontalHeader()
        size = self.select_all_box.sizeHint()
        x = header.sectionViewportPosition(self.CHECK_COLUMN)
        width = header.sectionSize(self.CHECK_COLUMN)
        self.select_all_box.move(
            x + (width - size.width()) // 2 + 2, (header.height() - size.height()) // 2
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # NAME takes 36% of the table; TARGET stretches into the rest.
        self.table.setColumnWidth(
            self.NAME_COLUMN, max(int(self.table.viewport().width() * 0.36), 120)
        )
        self._place_select_all()

    def _move_checked(self) -> None:
        links = self.checked_links()
        if links and self._on_move is not None:
            self._on_move(links)

    def _copy_checked(self) -> None:
        links = self.checked_links()
        if links and self._on_copy is not None:
            self._on_copy(links)

    def _delete_checked(self) -> None:
        links = self.checked_links()
        if not links:
            return
        if self._on_delete_many is not None:
            self._on_delete_many(links)
        elif len(links) == 1:
            self._on_delete(links[0])

    def _show_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None or not 0 <= item.row() < len(self._links):
            return
        link = self._links[item.row()]
        # Right-clicking a checked row acts on everything checked.
        links = self.checked_links() if link.id in self.checked_ids else [link]
        menu = self._build_links_context_menu(links)
        if not menu.isEmpty():
            menu.exec(self.table.viewport().mapToGlobal(pos))

    def _build_links_context_menu(self, links: list[ManualLink]) -> QMenu:
        menu = QMenu(self.table)
        if len(links) == 1:
            link = links[0]
            open_action = menu.addAction("Open")
            open_action.triggered.connect(
                lambda _checked=False, url=link.url: self._on_open(url)
            )
            edit_action = menu.addAction("Edit")
            edit_action.triggered.connect(
                lambda _checked=False, link_item=link: self._on_edit(link_item)
            )
            move_label = "Move to topic…"
            copy_label = "Copy to topic…"
            delete_label = "Delete"
        else:
            move_label = f"Move {len(links)} bookmarks to topic…"
            copy_label = f"Copy {len(links)} bookmarks to topic…"
            delete_label = f"Delete {len(links)} bookmarks…"
        if self._on_move is not None:
            move_action = menu.addAction(move_label)
            move_action.triggered.connect(
                lambda _checked=False, items=list(links): self._on_move(items)
            )
        if self._on_copy is not None:
            copy_action = menu.addAction(copy_label)
            copy_action.triggered.connect(
                lambda _checked=False, items=list(links): self._on_copy(items)
            )
        if len(links) == 1:
            delete_action = menu.addAction(delete_label)
            delete_action.triggered.connect(
                lambda _checked=False, link_item=links[0]: self._on_delete(link_item)
            )
        elif self._on_delete_many is not None:
            delete_action = menu.addAction(delete_label)
            delete_action.triggered.connect(
                lambda _checked=False, items=list(links): self._on_delete_many(items)
            )
        return menu

    def _open_row(self, row: int) -> None:
        if 0 <= row < len(self._links) and not self.table.isRowHidden(row):
            self._on_open(self._links[row].url)

    def _open_current_row(self) -> None:
        self._open_row(self.table.currentRow())

    def _update_table_height(self) -> None:
        self.table.fit_height()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Rows set while hidden were measured before the header had its size.
        self._update_table_height()

    def _apply_uniform_row_height(self) -> None:
        self.table.verticalHeader().setDefaultSectionSize(self._row_height)
        self.table.verticalHeader().setMinimumSectionSize(self._row_height)
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, self._row_height)


# The panel's previous name; kept so existing imports continue to work.
ManualLinksPanel = BookmarksPanel
