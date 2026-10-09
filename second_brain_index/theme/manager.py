"""Theme mode handling: follow the OS, or a manual light/dark choice.

Create exactly one ThemeManager right after QApplication, call ``apply()``,
then use ``ThemeManager.instance()`` anywhere in the UI.
"""
from __future__ import annotations

import weakref

from PyQt6.QtCore import QObject, QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPalette
from PyQt6.QtWidgets import QAbstractButton, QApplication

from .cursors import PointerCursorFilter
from .fonts import FontFamilies, load_fonts
from .icons import make_icon, tinted_svg_file
from .qss import build_stylesheet
from .tokens import PALETTES, Palette

MODES = ("system", "light", "dark")
MODE_LABELS = {"system": "Follow system", "light": "Light", "dark": "Dark"}
SETTINGS_KEY = "ui/theme_mode"


class ThemeManager(QObject):
    changed = pyqtSignal(object)  # emits the active Palette

    _instance: "ThemeManager | None" = None

    @classmethod
    def instance(cls) -> "ThemeManager":
        if cls._instance is None:
            raise RuntimeError("ThemeManager has not been created yet")
        return cls._instance

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        ThemeManager._instance = self
        self._app = app
        self._fonts = load_fonts()
        mode = QSettings().value(SETTINGS_KEY, "system", type=str)
        self._mode = mode if mode in MODES else "system"
        self._palette: Palette = PALETTES[self.effective()]
        self._bindings: list[tuple[weakref.ref, str, str, int]] = []
        # Every clickable control gets the pointing hand from one place.
        self.pointer_cursors = PointerCursorFilter(app)
        hints = app.styleHints()
        if hasattr(hints, "colorSchemeChanged"):  # Qt 6.5+
            hints.colorSchemeChanged.connect(self._on_system_scheme_changed)

    # -- state -------------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def palette(self) -> Palette:
        return self._palette

    @property
    def fonts(self) -> FontFamilies:
        return self._fonts

    def effective(self) -> str:
        if self._mode != "system":
            return self._mode
        try:
            scheme = self._app.styleHints().colorScheme()
        except AttributeError:
            return "light"
        return "dark" if scheme == Qt.ColorScheme.Dark else "light"

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self._mode = mode
        QSettings().setValue(SETTINGS_KEY, mode)
        self.apply()

    def toggle(self) -> None:
        """Header button click: flip to the opposite of what is showing."""
        self.set_mode("dark" if self.effective() == "light" else "light")

    def _on_system_scheme_changed(self, _scheme) -> None:
        if self._mode == "system":
            self.apply()

    # -- application -------------------------------------------------------
    def apply(self) -> None:
        p = PALETTES[self.effective()]
        self._palette = p
        font = QFont(self._fonts.body)
        font.setPixelSize(14)
        self._app.setFont(font)
        self._app.setPalette(self._qpalette(p))
        icons = {
            "check": tinted_svg_file("check", p.on_accent, stroke=2.5),
            "chevron_right": tinted_svg_file("chevron-right", p.subtle),
            "chevron_down": tinted_svg_file("chevron-down", p.muted),
        }
        self._app.setStyleSheet(build_stylesheet(p, self._fonts, icons))
        self._refresh_bindings()
        self.changed.emit(p)

    @staticmethod
    def _qpalette(p: Palette) -> QPalette:
        pal = QPalette()
        role = QPalette.ColorRole
        for r, value in (
            (role.Window, p.bg),
            (role.WindowText, p.text),
            (role.Base, p.field),
            (role.AlternateBase, p.surface),
            (role.Text, p.text),
            (role.Button, p.bg),
            (role.ButtonText, p.text),
            (role.Highlight, p.accent_tint),
            (role.HighlightedText, p.accent_text),
            (role.ToolTipBase, p.text),
            (role.ToolTipText, p.bg),
            (role.PlaceholderText, p.subtle),
            (role.Link, p.accent_deep),
            (role.Mid, p.divider),
        ):
            pal.setColor(r, QColor(value))
        pal.setColor(QPalette.ColorGroup.Disabled, role.Text, QColor(p.subtle))
        pal.setColor(QPalette.ColorGroup.Disabled, role.ButtonText, QColor(p.subtle))
        pal.setColor(QPalette.ColorGroup.Disabled, role.WindowText, QColor(p.subtle))
        return pal

    # -- icons -------------------------------------------------------------
    def icon(self, name: str, role: str = "text") -> QIcon:
        """A Lucide icon tinted with a palette attribute (``text``, ``accent_deep`` …)."""
        p = self._palette
        return make_icon(
            name,
            getattr(p, role),
            disabled_color=p.subtle,
            active_color=p.accent_text,
        )

    def bind_icon(
        self, target: QAbstractButton | QAction, name: str, role: str = "text", size: int = 16
    ) -> None:
        """Set an icon now and re-tint it whenever the theme changes.

        Binding the same target again replaces its previous icon, so a widget
        whose icon depends on its state can simply call this on every change.
        """
        self._set_icon(target, name, role, size)
        self._bindings = [
            binding for binding in self._bindings if binding[0]() not in (None, target)
        ]
        self._bindings.append((weakref.ref(target), name, role, size))

    def _set_icon(self, target, name: str, role: str, size: int) -> None:
        target.setIcon(self.icon(name, role))
        if isinstance(target, QAbstractButton):
            target.setIconSize(QSize(size, size))

    def _refresh_bindings(self) -> None:
        alive = []
        for ref, name, role, size in self._bindings:
            target = ref()
            if target is None:
                continue
            try:
                self._set_icon(target, name, role, size)
            except RuntimeError:  # underlying C++ object deleted
                continue
            alive.append((ref, name, role, size))
        self._bindings = alive
