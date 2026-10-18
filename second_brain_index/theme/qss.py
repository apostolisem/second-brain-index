"""Application stylesheet generated from a Palette.

Widgets opt into variants through dynamic properties (see ``widgets.set_prop``):

    QPushButton[variant="primary" | "ghost" | "icon" | "tag"]
    QPushButton:checkable:checked      -> toggled tint (e.g. "Filenames")
    QLabel[role="kicker" | "title" | "section" | "muted" | "brand" | "count"]
    QLabel[role="notice"], QWidget[role="tag"], QWidget[role="banner"]
    QLineEdit[role="search"]
"""
from __future__ import annotations

from .fonts import FontFamilies
from .tokens import Palette


def build_stylesheet(p: Palette, fonts: FontFamilies, icons: dict[str, str]) -> str:
    body = fonts.body
    heading = fonts.heading
    heading_weight = 400 if fonts.heading_is_semibold_family else 600
    return f"""
* {{
    font-family: "{body}";
    font-size: 14px;
    color: {p.text};
    selection-background-color: {p.accent_tint};
    selection-color: {p.accent_text};
    outline: 0;
}}
QMainWindow, QDialog {{ background: {p.bg}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QSplitter::handle {{ background: {p.divider}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

/* Buttons: secondary is the default; square corners, hairline border. */
QPushButton, QToolButton {{
    font-family: "{heading}";
    font-weight: {heading_weight};
    font-size: 14px;
    background: transparent;
    border: 1px solid {p.divider};
    border-radius: 0;
    padding: 6px 12px;
    min-height: 22px;
}}
QPushButton:hover, QToolButton:hover {{ background: {p.hover}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {p.pressed}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {p.subtle}; border-color: {p.rule}; }}
QPushButton:focus, QToolButton:focus {{ border: 1px solid {p.accent}; }}
QPushButton:checkable:checked, QToolButton:checked {{
    background: {p.accent_tint};
    color: {p.accent_text};
    border-color: {p.accent_tint_border};
}}
QPushButton[variant="primary"] {{
    background: {p.accent};
    color: {p.on_accent};
    border-color: {p.accent};
}}
QPushButton[variant="primary"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {p.accent_pressed}; border-color: {p.accent_pressed}; }}
QPushButton[variant="primary"]:disabled {{ background: {p.divider}; border-color: {p.divider}; color: {p.subtle}; }}
QPushButton[variant="ghost"], QToolButton[variant="ghost"] {{
    border-color: transparent;
    color: {p.accent_deep};
    padding: 4px 6px;
}}
QPushButton[variant="ghost"]:hover, QToolButton[variant="ghost"]:hover {{ background: {p.accent_tint}; }}
QPushButton[variant="ghost"]:pressed, QToolButton[variant="ghost"]:pressed {{ background: {p.accent_tint_border}; }}
QPushButton[variant="icon"], QToolButton[variant="icon"] {{
    padding: 0;
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    max-height: 34px;
}}
QPushButton[variant="icon-ghost"], QToolButton[variant="icon-ghost"] {{
    border-color: transparent;
    padding: 0;
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    max-height: 34px;
}}
QPushButton[variant="icon-ghost"]:hover {{ background: {p.accent_tint}; }}
QPushButton[variant="icon-ghost"]:pressed {{ background: {p.accent_tint_border}; }}
QPushButton[dense="true"], QToolButton[dense="true"] {{
    font-family: "{body}";
    font-weight: 400;
    font-size: 13px;
    padding: 2px 6px;
    min-height: 0;
}}
QPushButton[variant="tag-outline"] {{
    font-family: "{body}";
    font-weight: 400;
    font-size: 12px;
    border: 1px solid {p.accent};
    background: transparent;
    color: {p.accent_deep};
    padding: 1px 8px;
    min-height: 0;
}}
QPushButton[variant="tag-outline"]:hover {{ background: {p.accent_tint}; }}
QPushButton[variant="tag"] {{
    font-family: "{body}";
    font-weight: 400;
    font-size: 12px;
    border: none;
    background: transparent;
    color: {p.accent_text};
    padding: 2px 0;
    min-height: 0;
}}
QToolButton::menu-indicator {{
    image: url("{icons['chevron_down']}");
    subcontrol-origin: padding;
    subcontrol-position: right center;
    right: 6px;
    width: 12px;
    height: 12px;
}}
QToolButton[popupMode="2"] {{ padding-right: 24px; }}
QToolButton[popupMode="1"] {{ padding-right: 20px; }}
QToolButton::menu-button {{ border: none; border-left: 1px solid {p.divider}; width: 16px; }}
QToolButton::menu-arrow {{ image: url("{icons['chevron_down']}"); width: 12px; height: 12px; }}

/* Fields */
QLineEdit, QComboBox, QTextEdit, QPlainTextEdit {{
    background: {p.field};
    border: 1px solid {p.divider};
    border-radius: 0;
    padding: 6px 10px;
}}
QLineEdit, QComboBox {{ min-height: 22px; }}
QLineEdit:hover, QComboBox:hover, QTextEdit:hover {{ border-color: {p.subtle}; }}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border-color: {p.accent}; }}
QLineEdit:disabled {{ color: {p.subtle}; }}
QLineEdit[role="search"] {{ padding-left: 6px; }}
/* The icon and clear buttons inside a field are QToolButtons: keep the button
   rules above from inflating them, or they sit below the text line. */
QLineEdit QToolButton {{
    border: none;
    background: transparent;
    padding: 0;
    min-height: 0;
}}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url("{icons['chevron_down']}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {p.bg};
    border: 1px solid {p.divider};
    selection-background-color: {p.accent_tint};
    selection-color: {p.accent_text};
}}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QAbstractItemView::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {p.subtle};
    background: {p.field};
}}
QCheckBox::indicator:hover, QAbstractItemView::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
    image: url("{icons['check']}");
}}

/* Topic tree and lists */
QTreeView, QListView {{
    background: transparent;
    border: none;
    show-decoration-selected: 1;
}}
QTreeView::item, QListView::item {{
    min-height: 32px;
    padding: 0 6px;
    border: none;
}}
QTreeView::item:hover, QListView::item:hover {{ background: {p.hover}; }}
QTreeView::item:selected, QListView::item:selected,
QTreeView::branch:selected {{
    background: {p.accent_tint};
    color: {p.accent_text};
}}
QTreeView::branch {{ background: transparent; border-image: none; }}
QTreeView::branch:has-children:closed {{ image: url("{icons['chevron_right']}"); }}
QTreeView::branch:has-children:open {{ image: url("{icons['chevron_down']}"); }}
QListWidget[role="picker"] {{ border: 1px solid {p.divider}; }}

/* Tables */
QTableView {{
    background: transparent;
    border: none;
    gridline-color: transparent;
    alternate-background-color: transparent;
}}
QTableView::item {{
    padding: 0 8px;
    border: none;
    border-bottom: 1px solid {p.rule};
}}
/* Row hover is painted by RowHoverDelegate so it spans the whole row. */
QTableView::item:selected {{ background: {p.accent_tint}; color: {p.accent_text}; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: transparent;
    color: {p.muted};
    font-size: 11px;
    font-weight: 500;
    border: none;
    border-bottom: 1px solid {p.divider};
    padding: 6px 8px;
}}
QTableCornerButton::section {{ background: transparent; border: none; }}

/* Group boxes become plain labelled regions; framed cards use BlueprintFrame. */
QGroupBox {{
    border: none;
    margin-top: 24px;
    padding: 0;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 0;
    padding: 0 0 6px 0;
    font-family: "{heading}";
    font-weight: {heading_weight};
    font-size: 13px;
    color: {p.muted};
}}

/* Menus */
QMenu {{
    background: {p.bg};
    border: 1px solid {p.divider};
    padding: 6px 0;
}}
QMenu::item {{ padding: 7px 28px 7px 14px; background: transparent; }}
QMenu::item:selected {{ background: {p.accent_tint}; color: {p.accent_text}; }}
QMenu::item:disabled {{ color: {p.subtle}; }}
QMenu::separator {{ height: 1px; background: {p.divider}; margin: 4px 0; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}

/* Status bar */
QStatusBar {{
    background: {p.bg};
    border-top: 1px solid {p.divider};
    min-height: 31px;
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {p.muted}; font-size: 12px; padding: 0 8px; }}
QStatusBar QPushButton {{ font-size: 12px; padding: 1px 6px; min-height: 0; }}

/* Scrollbars */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p.divider}; min-height: 28px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.divider}; min-width: 28px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {p.subtle}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QToolTip {{
    background: {p.text};
    color: {p.bg};
    border: none;
    padding: 4px 8px;
    font-size: 12px;
}}

/* Roles */
QLabel[role="brand"] {{ font-family: "{heading}"; font-weight: {heading_weight}; font-size: 20px; }}
QLabel[role="kicker"] {{ font-size: 11px; font-weight: 500; color: {p.accent_deep}; }}
QLabel[role="kicker-muted"] {{ font-size: 11px; color: {p.subtle}; }}
QLabel[role="title"] {{ font-family: "{heading}"; font-weight: {heading_weight}; font-size: 40px; }}
QLabel[role="title"]:hover {{ color: {p.accent_deep}; }}
QLabel[role="card-title"] {{ font-family: "{heading}"; font-weight: {heading_weight}; font-size: 17px; }}
QLabel[role="dialog-title"] {{ font-family: "{heading}"; font-weight: {heading_weight}; font-size: 20px; }}
QLabel[role="section"] {{ font-family: "{heading}"; font-weight: {heading_weight}; font-size: 13px; color: {p.muted}; }}
QLabel[role="muted"] {{ color: {p.subtle}; font-size: 13px; }}
QLabel[role="count"] {{ color: {p.subtle}; font-size: 12px; }}
QLabel[role="field-label"] {{ color: {p.muted}; font-size: 12px; }}
QLabel[role="notice"] {{
    background: {p.accent_tint};
    color: {p.accent_text};
    border: 1px solid {p.accent_tint_border};
    padding: 10px 14px;
}}
QLabel[role="note"] {{
    background: {p.field};
    border: 1px solid {p.divider};
    padding: 8px 10px;
    font-size: 13px;
}}
QLabel[role="hint"] {{
    font-size: 11px;
    color: {p.muted};
    border: 1px solid {p.divider};
    padding: 1px 6px;
}}
QLabel[role="tag-neutral"] {{
    background: {p.surface};
    font-size: 11px;
    padding: 1px 6px;
}}
QLabel[role="chip-neutral"] {{
    background: {p.surface};
    font-size: 12px;
    padding: 2px 8px;
}}
QLabel[role="body-muted"] {{ color: {p.muted}; font-size: 13px; }}
QLabel[role="accent-note"] {{ color: {p.accent_text}; font-size: 13px; }}
QLabel[role="error"] {{
    background: {p.field};
    border: 1px solid {p.divider};
    padding: 8px 10px;
    font-size: 13px;
}}
QWidget[role="strip"] {{ background: {p.accent_tint}; }}
QWidget[role="strip"] QLabel {{ color: {p.accent_text}; font-size: 13px; font-weight: 500; }}
QWidget[role="strip"] QPushButton {{ color: {p.accent_text}; }}
QDialogButtonBox {{ button-layout: 3; dialogbuttonbox-buttons-have-icons: 0; }}
QWidget[role="tag"] {{ background: {p.accent_tint}; }}
QWidget[role="tag-neutral"] {{ background: {p.surface}; }}
QWidget[role="banner"] {{ background: {p.accent_tint}; border-bottom: 1px solid {p.divider}; }}
QWidget[role="banner"] QLabel {{ color: {p.accent_text}; font-size: 13px; }}
QWidget[role="header"] {{ border-bottom: 1px solid {p.divider}; }}
QWidget[role="sidebar"] {{ border-right: 1px solid {p.divider}; }}
BlueprintFrame {{ background: transparent; border: none; }}
"""
