from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QIcon

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
ICON_SVG = ASSETS_DIR / "icon.svg"
ICON_PNG_DIR = ASSETS_DIR / "icons"

DESKTOP_FILE_NAME = "second-brain-hub"


def app_icon() -> QIcon:
    """Application icon, built from every rendered PNG size that exists.

    Falls back to the SVG, and finally to an empty icon, so a missing assets
    folder never stops the app from starting.
    """
    icon = QIcon()
    for png in sorted(ICON_PNG_DIR.glob("icon_*.png")):
        icon.addFile(str(png))
    if icon.isNull() and ICON_SVG.exists():
        icon.addFile(str(ICON_SVG))
    return icon
