from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtGui import QFontDatabase

from ..resources import ASSETS_DIR

FONTS_DIR = ASSETS_DIR / "fonts"

_BODY_CANDIDATES = ("Barlow",)
_HEADING_CANDIDATES = ("Barlow Condensed", "Barlow Condensed SemiBold")


@dataclass(frozen=True)
class FontFamilies:
    body: str
    heading: str
    heading_is_semibold_family: bool = False


_loaded: FontFamilies | None = None


def load_fonts() -> FontFamilies:
    """Register bundled TTFs once and resolve the body/heading families.

    Missing files are not an error: the system UI font is used instead.
    """
    global _loaded
    if _loaded is not None:
        return _loaded
    if FONTS_DIR.is_dir():
        for path in sorted(FONTS_DIR.glob("*.ttf")):
            QFontDatabase.addApplicationFont(str(path))
    families = set(QFontDatabase.families())
    system = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    body = next((f for f in _BODY_CANDIDATES if f in families), system)
    heading = next((f for f in _HEADING_CANDIDATES if f in families), body)
    _loaded = FontFamilies(
        body=body,
        heading=heading,
        heading_is_semibold_family=heading.endswith("SemiBold"),
    )
    return _loaded
