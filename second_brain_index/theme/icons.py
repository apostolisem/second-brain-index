"""Lucide icons, tinted at runtime from the active palette."""
from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import QByteArray, QRectF, QStandardPaths, Qt
from PyQt6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from ..resources import ASSETS_DIR

LUCIDE_DIR = ASSETS_DIR / "icons" / "lucide"


@lru_cache(maxsize=None)
def _svg_source(name: str) -> str:
    path = LUCIDE_DIR / f"{name}.svg"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        # An empty square keeps layout stable if an icon file is missing.
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
            'viewBox="0 0 24 24"></svg>'
        )


def _tint(name: str, color: str, stroke: float | None = None) -> str:
    svg = _svg_source(name).replace("currentColor", color)
    if stroke is not None:
        svg = svg.replace('stroke-width="1.5"', f'stroke-width="{stroke}"')
    return svg


def _render(svg: str, size: int, dpr: float) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, pixmap.width(), pixmap.height()))
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def make_icon(
    name: str,
    color: str,
    disabled_color: str | None = None,
    active_color: str | None = None,
    sizes: tuple[int, ...] = (14, 16, 18, 20, 24),
) -> QIcon:
    app = QGuiApplication.instance()
    dpr = app.devicePixelRatio() if app is not None else 1.0
    icon = QIcon()
    for size in sizes:
        icon.addPixmap(_render(_tint(name, color), size, dpr), QIcon.Mode.Normal)
        if disabled_color:
            icon.addPixmap(
                _render(_tint(name, disabled_color), size, dpr), QIcon.Mode.Disabled
            )
        if active_color:
            pixmap = _render(_tint(name, active_color), size, dpr)
            icon.addPixmap(pixmap, QIcon.Mode.Selected)
            icon.addPixmap(pixmap, QIcon.Mode.Active)
    return icon


def tinted_svg_file(name: str, color: str, stroke: float | None = None) -> str:
    """Write a tinted copy for use in QSS ``url()`` and return its posix path."""
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.CacheLocation
    ) or tempfile.gettempdir()
    folder = Path(base) / "theme-icons"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = f"-{stroke}" if stroke is not None else ""
    path = folder / f"{name}-{color.lstrip('#')}{suffix}.svg"
    if not path.exists():
        path.write_text(_tint(name, color, stroke), encoding="utf-8")
    return path.as_posix()
