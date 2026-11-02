from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...constants import PREFIX_TO_STATE, STATE_FOLDERS
from ...models import ManualLocation
from ...naming import validate_portable_name
from ...theme import set_prop
from ..creation import CreationOption, CreationPlan, CreationReport, creation_key
from ..widgets.common import (
    ghost_button,
    make_delete_button,
    role_label,
    secondary_button,
    tracked,
)
from .common import add_field, build_shell, note_label
from .location_dialog import LocationDialog

if TYPE_CHECKING:
    from ..main_window import MainWindow

STEP_TITLES = ("Name", "Place", "Sources", "Review", "Done")
URL_HINTS = {"onenote": "onenote:, https://", "outlook": "https://outlook…"}


class OptionRow:
    """A place to create the topic in, with its checkbox and, for lists, a link field."""

    def __init__(self, option: CreationOption) -> None:
        self.option = option
        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        text = option.label
        if option.blocked:
            text = f"{text}  ({option.blocked})"
        self.checkbox = QCheckBox(text)
        self.checkbox.setEnabled(option.blocked is None)
        layout.addWidget(self.checkbox)
        detail = role_label(option.detail, "muted", wrap=True)
        detail.setContentsMargins(24, 0, 0, 0)
        layout.addWidget(detail)
        self.url_edit: QLineEdit | None = None
        if option.spec is not None:
            self.url_edit = QLineEdit()
            self.url_edit.setPlaceholderText(
                f"{option.label} direct link (optional): {URL_HINTS.get(option.source, 'https://')}"
            )
            # Only asked for once the list is ticked; a field for an unticked
            # source reads as if it were going to be used.
            self.url_edit.setVisible(False)
            self.checkbox.toggled.connect(self.url_edit.setVisible)
            layout.addWidget(self.url_edit)

    def checked(self) -> bool:
        return self.checkbox.isEnabled() and self.checkbox.isChecked()


class NewTopicDialog(QDialog):
    """Wizard for a new topic: name, hub and section, places to create it in.

    The planning and the creation itself are the window's; this only asks.
    """

    def __init__(
        self,
        parent: QWidget | None,
        planner: "MainWindow",
        hub_key: str | None,
        state: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.planner = planner
        self.report: CreationReport | None = None
        self._section_chosen = False
        self._options_for: tuple[str, str, str | None] | None = None
        self.rows: list[OptionRow] = []
        self.manual_locations: list[ManualLocation] = []

        layout = build_shell(self, "New topic", width=560)
        self.step_label = tracked(role_label("", "kicker-muted"))
        layout.addWidget(self.step_label)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)
        self.stack.addWidget(self._build_name_page())
        self.stack.addWidget(self._build_place_page(hub_key, state))
        self.stack.addWidget(self._build_sources_page())
        self.stack.addWidget(self._build_review_page())
        self.stack.addWidget(self._build_result_page())
        for index in range(self.stack.count()):
            # The stack is as tall as its tallest page; the others start at the top.
            self.stack.widget(index).layout().addStretch(1)

        self.error_label = role_label("", "error", wrap=True)
        layout.addWidget(self.error_label)
        layout.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.back_button = secondary_button("Back")
        self.cancel_button = secondary_button("Cancel")
        self.next_button = QPushButton("Next")
        set_prop(self.next_button, "variant", "primary")
        for button in (self.back_button, self.cancel_button, self.next_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.next_button.setDefault(True)
        self.back_button.clicked.connect(self.go_back)
        self.cancel_button.clicked.connect(self.reject)
        self.next_button.clicked.connect(self.go_next)

        self.show_page(0)
        self.name_edit.setFocus()

    # Pages

    def _page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        return page, layout

    def _build_name_page(self) -> QWidget:
        page, layout = self._page()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("P - , A - or R - followed by a name")
        add_field(layout, "Topic name", self.name_edit)
        self.name_edit.textChanged.connect(self._name_changed)
        return page

    def _build_place_page(self, hub_key: str | None, state: str | None) -> QWidget:
        page, layout = self._page()
        self.hub_combo = QComboBox()
        for choice in self.planner.creation_hubs():
            self.hub_combo.addItem(choice.label, choice.key)
        index = self.hub_combo.findData(hub_key)
        self.hub_combo.setCurrentIndex(max(index, 0))
        add_field(layout, "Hub", self.hub_combo)
        self.section_combo = QComboBox()
        for section_state, folder_name in STATE_FOLDERS.items():
            self.section_combo.addItem(folder_name, section_state)
        if state in STATE_FOLDERS:
            self.section_combo.setCurrentIndex(self.section_combo.findData(state))
            # Picked by right-clicking the section; a name prefix must not undo that.
            self._section_chosen = True
        add_field(layout, "Section", self.section_combo)
        self.section_combo.activated.connect(self._mark_section_chosen)
        self.hub_combo.currentIndexChanged.connect(lambda _index: self._refresh())
        self.section_combo.currentIndexChanged.connect(lambda _index: self._refresh())
        self.place_note = note_label()
        layout.addWidget(self.place_note)
        return page

    def _build_sources_page(self) -> QWidget:
        page, layout = self._page()
        layout.addWidget(role_label("Create it in", "field-label"))
        self.options_host = QVBoxLayout()
        self.options_host.setSpacing(10)
        layout.addLayout(self.options_host)
        self.subhub_checkbox = QCheckBox(
            "Make it a Sub-Hub (its own PARA sections and topic lists)"
        )
        self.subhub_checkbox.toggled.connect(lambda _checked: self._refresh())
        layout.addWidget(self.subhub_checkbox)

        layout.addWidget(role_label("Your own links and folders", "field-label"))
        self.locations_host = QVBoxLayout()
        self.locations_host.setSpacing(4)
        layout.addLayout(self.locations_host)
        self.add_location_button = ghost_button("Link or folder…", "plus")
        self.add_location_button.setToolTip(
            "Add a URL or a folder somewhere else, as Add location does"
        )
        self.add_location_button.setAutoDefault(False)
        self.add_location_button.clicked.connect(self.add_manual_location)
        layout.addWidget(self.add_location_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.lists_note = note_label()
        layout.addWidget(self.lists_note)
        return page

    def _build_review_page(self) -> QWidget:
        page, layout = self._page()
        layout.addWidget(role_label("This will", "field-label"))
        self.review_label = role_label("", "body-muted", wrap=True)
        layout.addWidget(self.review_label)
        return page

    def _build_result_page(self) -> QWidget:
        page, layout = self._page()
        self.result_label = role_label("", "body-muted", wrap=True)
        layout.addWidget(self.result_label)
        self.manual_host = QVBoxLayout()
        self.manual_host.setSpacing(6)
        layout.addLayout(self.manual_host)
        return page

    # Navigation

    def show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.step_label.setText(
            f"STEP {min(index, 3) + 1} OF 4 · {STEP_TITLES[index].upper()}"
        )
        done = index == 4
        self.back_button.setVisible(0 < index < 4)
        self.cancel_button.setVisible(not done)
        self.next_button.setText({3: "Create", 4: "Close"}.get(index, "Next"))
        if index == 2:
            self._build_options()
        if index == 3:
            self.review_label.setText(
                "\n".join(f"• {line}" for line in self.planner.describe_creation_plan(self.plan()))
            )
        self._refresh()

    def go_back(self) -> None:
        self.show_page(self.stack.currentIndex() - 1)

    def go_next(self) -> None:
        index = self.stack.currentIndex()
        if index == 4:
            self.accept()
            return
        if self.current_error():
            return
        if index == 3:
            self.report = self.planner.execute_creation_plan(self.plan())
            self._show_report(self.report)
        self.show_page(index + 1)

    # State

    def name(self) -> str:
        return self.name_edit.text().strip()

    def state(self) -> str:
        return self.section_combo.currentData()

    def hub_key(self) -> str | None:
        return self.hub_combo.currentData()

    def exists_already(self) -> bool:
        return creation_key(self.name(), self.hub_key()) in self.planner.topics_by_key

    def current_error(self) -> str | None:
        index = self.stack.currentIndex()
        if index == 0:
            return validate_portable_name(self.name())
        if index == 1:
            return self.planner.validate_new_topic(self.name(), self.state(), self.hub_key())
        if index == 2 and not any(row.checked() for row in self.rows):
            if not self.manual_locations:
                return "Pick a place to create the topic in, or add a link or folder of your own."
        return None

    def _refresh(self) -> None:
        index = self.stack.currentIndex()
        error = self.current_error()
        # An empty name is not worth a message, only a disabled Next.
        shown = error if (index != 0 or self.name()) else None
        self.error_label.setText(shown or "")
        self.error_label.setVisible(bool(shown))
        self.next_button.setEnabled(error is None)
        if index == 1:
            exists = error is None and self.exists_already()
            self.place_note.setText(
                f"'{self.name()}' already exists here; only the places it is"
                " missing can be added." if exists else ""
            )
            self.place_note.setVisible(exists)
        if index == 2:
            self.subhub_checkbox.setVisible(self._subhub_possible())
            self.subhub_checkbox.setEnabled(
                any(row.checked() and row.option.is_folder for row in self.rows)
            )
        self._fit_height()

    def _fit_height(self) -> None:
        """Grow to fit what is shown now: a message, link field or result line.

        Qt only keeps the window at the layout's minimum, which undercounts
        the wrapped labels and squeezes the rows together. Measured on the
        next event loop pass, because rows added to a shown dialog only
        appear then.
        """
        QTimer.singleShot(0, self._grow_to_fit)

    def _grow_to_fit(self) -> None:
        layout = self.layout()
        if layout.hasHeightForWidth():
            needed = layout.totalHeightForWidth(self.width())
        else:
            needed = layout.totalSizeHint().height()
        if self.height() < needed:
            self.resize(self.width(), needed)

    def _subhub_possible(self) -> bool:
        # Sub-hubs nest one level deep, and an archived one would be pointless.
        return (
            self.hub_key() is None
            and self.state() != "Archive"
            and not self.exists_already()
        )

    def _name_changed(self, text: str) -> None:
        """Follow the name's prefix until the user picks a section themselves."""
        if not self._section_chosen:
            cleaned = text.strip()
            for prefix, state in PREFIX_TO_STATE.items():
                if cleaned.startswith(prefix):
                    self.section_combo.setCurrentIndex(self.section_combo.findData(state))
                    break
        self._refresh()

    def _mark_section_chosen(self, _index: int) -> None:
        self._section_chosen = True

    def _build_options(self) -> None:
        wanted = (self.name(), self.state(), self.hub_key())
        # Coming back from Review keeps what was ticked.
        if wanted == self._options_for:
            return
        self._options_for = wanted
        for row in self.rows:
            row.widget.hide()
            row.widget.deleteLater()
        self.rows = []
        self.subhub_checkbox.setChecked(False)
        options = self.planner.creation_options(*wanted)
        list_names = [option.label for option in options if not option.is_folder]
        self.lists_note.setText(
            f"{' and '.join(list_names)} only "
            f"{'gets an entry in its' if len(list_names) == 1 else 'get an entry in their'} topic list;"
            " the last page shows what to create there yourself."
            if list_names
            else ""
        )
        self.lists_note.setVisible(bool(list_names))
        defaults: set[str] = set()
        for option in options:
            row = OptionRow(option)
            # One folder per kind: a second PARA root would duplicate the topic.
            if option.is_folder and option.blocked is None and option.source not in defaults:
                row.checkbox.setChecked(True)
                defaults.add(option.source)
            row.checkbox.toggled.connect(lambda _checked: self._refresh())
            self.rows.append(row)
            self.options_host.addWidget(row.widget)

    def plan(self) -> CreationPlan:
        hub_key = self.hub_key()
        plan = CreationPlan(
            self.name(), self.state(), hub_key, self.planner.creation_hub(hub_key)
        )
        for row in self.rows:
            if not row.checked():
                continue
            if row.option.is_folder:
                plan.folders.append(row.option)
            else:
                url = row.url_edit.text().strip() if row.url_edit else ""
                plan.lists.append((row.option.spec, url))
        plan.manual_locations = list(self.manual_locations)
        plan.make_subhub = (
            self._subhub_possible()
            and self.subhub_checkbox.isChecked()
            and bool(plan.folders)
        )
        return plan

    def add_manual_location(self) -> None:
        dialog = LocationDialog(self, "Add location", accept_label="Add")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target, label = dialog.values()
        self.manual_locations.append(ManualLocation(0, "", label, target, dialog.kind()))
        self._show_manual_locations()

    def remove_manual_location(self, location: ManualLocation) -> None:
        self.manual_locations = [item for item in self.manual_locations if item is not location]
        self._show_manual_locations()

    def _show_manual_locations(self) -> None:
        while self.locations_host.count():
            widget = self.locations_host.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        for location in self.manual_locations:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            text = QLabel(f"{location.source_label}: {location.display_label}")
            text.setToolTip(location.target)
            row_layout.addWidget(text, 1)
            remove = make_delete_button()
            remove.setToolTip("Remove")
            remove.setAutoDefault(False)
            remove.clicked.connect(
                lambda _checked=False, item=location: self.remove_manual_location(item)
            )
            row_layout.addWidget(remove)
            self.locations_host.addWidget(row)
        self._refresh()

    def _show_report(self, report: CreationReport) -> None:
        lines = [f"✓ {line}" for line in report.done]
        lines += [f"✗ {line}" for line in report.failed]
        self.result_label.setText("\n".join(lines))
        if not report.manual_actions:
            return
        self.manual_host.addWidget(role_label("Still to do by hand", "field-label"))
        for action in report.manual_actions:
            row = QHBoxLayout()
            label = QLabel(action.text)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            if action.url:
                button = ghost_button(f"Open {action.source}", "external-link")
                button.clicked.connect(
                    lambda _checked=False, url=action.url: self.planner.open_link(url)
                )
                row.addWidget(button)
            self.manual_host.addLayout(row)
        copy_button = ghost_button("Copy topic name", "copy")
        copy_button.clicked.connect(
            lambda _checked=False: QGuiApplication.clipboard().setText(self.name())
        )
        self.manual_host.addWidget(copy_button, 0, Qt.AlignmentFlag.AlignLeft)
