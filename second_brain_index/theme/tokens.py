"""Industry design-system tokens for the Qt app.

Light values are taken verbatim from the Industry stylesheet (``--color-*`` and
the 100-900 ramps). Dark values are derived from the same ramps: the light
ground and ink swap, the accent moves one step lighter (500 base, 400 hover,
300 pressed) and tints use the deep accent steps.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str  # window ground
    surface: str  # raised neutral surface
    field: str  # input fill
    text: str
    muted: str  # secondary text, section labels
    subtle: str  # tertiary text, counts, placeholders
    divider: str  # hairlines (16% ink)
    rule: str  # table row rules (8% ink)
    hover: str  # neutral hover tint (6% ink)
    pressed: str  # neutral pressed tint (12% ink)
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_deep: str  # accent used as small text / icons
    accent_tint: str  # selected rows, chips, notices
    accent_tint_border: str
    accent_text: str  # text on accent_tint
    on_accent: str  # text on solid accent
    corner: str  # blueprint registration marks (55% ink)


LIGHT = Palette(
    name="light",
    bg="#f2f2f3",
    surface="#e9e9ea",
    field="#f5f5f8",
    text="#1d1f20",
    muted="#5d5d60",
    subtle="#7a7a7d",
    divider="#d0d0d1",
    rule="#e1e1e2",
    hover="#e4e4e5",
    pressed="#d8d8d9",
    accent="#5980a6",
    accent_hover="#597ea3",
    accent_pressed="#416180",
    accent_deep="#416180",
    accent_tint="#eef6ff",
    accent_tint_border="#b5d9fd",
    accent_text="#2c455d",
    on_accent="#f2f2f3",
    corner="#7d7e7f",
)

DARK = Palette(
    name="dark",
    bg="#1d1f20",
    surface="#2b2b2d",
    field="#242628",
    text="#f2f2f3",
    muted="#b7b7ba",
    subtle="#98989b",
    divider="#3f4142",
    rule="#2e3031",
    hover="#2a2c2d",
    pressed="#37393a",
    accent="#749dc4",
    accent_hover="#94bce3",
    accent_pressed="#b5d9fd",
    accent_deep="#94bce3",
    accent_tint="#1d2d3d",
    accent_tint_border="#416180",
    accent_text="#d6ebff",
    on_accent="#1d1f20",
    corner="#929394",
)

PALETTES = {"light": LIGHT, "dark": DARK}

# Spacing (Industry --space-*, 0.85x density), rounded to whole pixels.
SPACE = {1: 3, 2: 7, 3: 10, 4: 14, 6: 20, 8: 27}

# Type scale in pixels.
TYPE = {
    "body": 14,
    "small": 13,
    "caption": 12,
    "kicker": 11,
    "card_title": 17,
    "dialog_title": 20,
    "brand": 20,
    "topic_title": 40,
}

# Fixed layout metrics from the 1b design.
METRICS = {
    "header_height": 60,
    "sidebar_width": 300,
    "tree_row_height": 32,
    "control_height": 36,
    "compact_control_height": 32,
    "status_height": 32,
    "blueprint_mark": 11,
}
