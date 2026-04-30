"""Centralized visual tokens + global QSS for the Cove PDF Editor.

Visual-only. No business logic, no behavior. Other modules (canvas.py,
tools.py) read the QColor constants here so the look stays consistent
without grepping a dozen widgets.

System font stack only — no bundled fonts.
"""
from __future__ import annotations

from PySide6.QtGui import QColor

# ---------------------------------------------------------------- tokens

# Background tier (page chrome → surfaces → elevation).
BG            = "#0b0b10"
SURFACE       = "#13131b"
SURFACE_2     = "#181822"
SURFACE_3     = "#1f1f2b"

# Borders. Hex equivalents of rgba(255,255,255,0.06) / 0.10 over BG.
BORDER        = "#1c1c26"
BORDER_STRONG = "#25252f"

# Text tier.
TEXT          = "#ececf1"
TEXT_DIM      = "#9a9aae"
TEXT_FAINT    = "#6b6b80"

# Accent (cyan/teal) + soft variants. Soft and ring use rgba so they
# blend on top of any surface tier without recomputing against BG.
ACCENT        = "#50e6cf"
ACCENT_SOFT   = "rgba(80, 230, 207, 0.14)"
ACCENT_RING   = "rgba(80, 230, 207, 0.35)"

DANGER        = "#ff6b6b"

# Status / signal tones used in the segmented status bar and the
# sidebar footer pip. Match the HTML mockup's --good / --warn vars.
GOOD          = "#3ddc97"
WARN          = "#ffb454"

# Radii.
RADIUS        = "8px"
RADIUS_SM     = "6px"
RADIUS_LG     = "12px"

# System font stack — picks up Inter / Segoe UI / SF Pro Text /
# DejaVu Sans depending on platform. No bundling.
FONT_STACK = (
    '"Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", '
    '"DejaVu Sans", system-ui, sans-serif'
)

# ---------------------------------------------------------- canvas colors
#
# canvas.py and tools.py import these. They were previously hard-coded
# blues; centralizing here keeps the selection chrome aligned with the
# accent token.

HANDLE_BORDER     = QColor(80, 230, 207)        # ACCENT
HANDLE_FILL       = QColor(255, 255, 255)
FREETEXT_BORDER   = QColor(80, 230, 207)
INLINE_EDIT_BORDER = QColor(80, 230, 207)
DRAG_PREVIEW_PEN  = QColor(80, 230, 207)
DRAG_PREVIEW_FILL = QColor(80, 230, 207, 28)    # 11% opacity tint
VIEW_BG_HEX       = BG

# ------------------------------------------------------------- global QSS
#
# Applied once at MainWindow level. Per-widget setStyleSheet calls are
# removed in app.py so this single sheet is the source of truth for the
# app shell. The format toolbar, sidebar, page list, menu, statusbar,
# and scrollbars all match the same palette.

GLOBAL_QSS = f"""
/* ---- Window + central -------------------------------------------- */
QMainWindow {{ background: {BG}; }}
QMainWindow::separator {{ background: {BORDER}; width: 1px; height: 1px; }}

/* ---- Status bar -------------------------------------------------- */
QStatusBar {{
    background: {BG};
    color: {TEXT_DIM};
    border-top: 1px solid {BORDER};
    padding: 2px 12px;
    font-size: 11px;
}}
QStatusBar::item {{ border: none; }}

/* ---- Menu bar ---------------------------------------------------- */
QMenuBar {{
    background: {BG};
    color: {TEXT_DIM};
    border-bottom: 1px solid {BORDER};
    padding: 2px 4px;
    font-size: 13px;
}}
QMenuBar::item {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 5px 10px;
    border-radius: {RADIUS_SM};
}}
QMenuBar::item:selected,
QMenuBar::item:pressed {{
    background: {ACCENT_SOFT};
    color: {TEXT};
}}

QMenu {{
    background: {SURFACE_2};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SM};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 16px 6px 14px;
    border-radius: {RADIUS_SM};
    color: {TEXT};
}}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::separator {{
    background: {BORDER};
    height: 1px;
    margin: 4px 6px;
}}

/* ---- Format toolbar --------------------------------------------- */
QToolBar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    spacing: 2px;
}}
QToolBar::separator {{
    background: {BORDER};
    width: 1px;
    margin: 4px 6px;
}}
QToolBar QLabel {{
    background: transparent;
    color: {TEXT_FAINT};
    padding: 0 10px 0 4px;
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QToolBar QToolButton {{
    color: {TEXT_DIM};
    padding: 5px 10px;
    margin: 0 1px;
    border: 1px solid transparent;
    border-radius: {RADIUS_SM};
    font-size: 13px;
    min-width: 24px;
    background: transparent;
}}
QToolBar QToolButton:hover:!disabled {{
    color: {TEXT};
    background: {SURFACE_2};
}}
QToolBar QToolButton:checked:!disabled {{
    color: {ACCENT};
    background: {ACCENT_SOFT};
    border-color: {BORDER_STRONG};
}}
QToolBar QToolButton:disabled {{ color: {TEXT_FAINT}; }}

QToolBar QComboBox,
QToolBar QSpinBox {{
    background: {BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM};
    padding: 3px 6px;
    min-height: 22px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {ACCENT};
}}
QToolBar QComboBox:focus,
QToolBar QSpinBox:focus {{
    border: 1px solid {ACCENT};
}}
QToolBar QComboBox:disabled,
QToolBar QSpinBox:disabled {{
    color: {TEXT_FAINT};
    background: {SURFACE};
}}
QToolBar QComboBox QAbstractItemView {{
    background: {SURFACE_2};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    selection-background-color: {ACCENT_SOFT};
    selection-color: {ACCENT};
    outline: 0;
}}

/* ---- Sidebar frame ---------------------------------------------- */
QFrame#Sidebar {{
    background: {SURFACE};
    border: none;
    border-right: 1px solid {BORDER};
}}

QLabel#SidebarTitle {{
    color: {TEXT};
    background: transparent;
    font-size: 13px;
    font-weight: 600;
    padding: 8px 8px 4px 8px;
    letter-spacing: 0.2px;
}}

QLabel#SidebarSection {{
    color: {TEXT_FAINT};
    background: transparent;
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 1.2px;
    padding: 8px 8px 4px 8px;
}}

QLabel#SidebarVersion {{
    color: {TEXT_FAINT};
    background: transparent;
    font-size: 10px;
    padding: 6px 8px 2px 8px;
}}

/* ---- Sidebar tool buttons --------------------------------------- */
/* Left border is always 2px (transparent when unchecked) so the */
/* active teal strip doesn't shift the row geometry on toggle.    */
QPushButton#ToolButton {{
    text-align: left;
    padding: 6px 10px;
    border: none;
    border-left: 2px solid transparent;
    border-radius: {RADIUS_SM};
    color: {TEXT_DIM};
    background: transparent;
    font-size: 13.5px;
    min-height: 34px;
}}
QPushButton#ToolButton:hover {{
    background: {SURFACE_2};
    color: {TEXT};
}}
QPushButton#ToolButton:checked {{
    background: {ACCENT_SOFT};
    color: {TEXT};
    border-left: 2px solid {ACCENT};
}}
QPushButton#ToolButton:disabled {{ color: {TEXT_FAINT}; }}

/* Children of ToolButton — transparent to mouse so clicks fall      */
/* through to the button itself (set in app.py via WA_TransparentForMouseEvents). */
QLabel#ToolIcon {{
    color: {TEXT_DIM};
    background: transparent;
    font-size: 13.5px;
    padding: 0;
    min-width: 16px;
}}
QLabel#ToolName {{
    color: {TEXT_DIM};
    background: transparent;
    font-size: 13.5px;
}}
QLabel#ToolName[active="true"] {{ color: {TEXT}; font-weight: 500; }}
QLabel#ToolIcon[active="true"] {{ color: {ACCENT}; }}

QLabel#HotKey {{
    color: {TEXT_FAINT};
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 0 5px;
    font-family: monospace;
    font-size: 10.5px;
    min-width: 14px;
}}
QLabel#HotKey[active="true"] {{
    color: {ACCENT};
    border-color: {BORDER_STRONG};
    background: {ACCENT_SOFT};
}}

/* ---- Page list -------------------------------------------------- */
QListWidget#PageList {{
    background: {BG};
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM};
    padding: 4px;
    outline: 0;
}}
QListWidget#PageList::item {{
    padding: 6px 10px;
    border-radius: {RADIUS_SM};
    margin: 1px 0;
}}
QListWidget#PageList::item:hover {{
    background: {SURFACE_2};
    color: {TEXT};
}}
QListWidget#PageList::item:selected {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
}}

/* ---- Canvas stack (background between viewport + chrome) -------- */
QStackedWidget#CanvasStack {{ background: {BG}; }}
QLabel#Placeholder {{
    color: {TEXT_DIM};
    background: transparent;
    font-size: 14px;
}}

/* ---- Dialog backdrops (color picker, file dialog) --------------- */
QDialog {{ background: {BG}; color: {TEXT}; }}
QInputDialog {{ background: {BG}; color: {TEXT}; }}
QMessageBox {{ background: {BG}; color: {TEXT}; }}

/* ---- Scrollbars ------------------------------------------------- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-height: 24px;
    border: 2px solid transparent;
    background-clip: padding;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_FAINT}; }}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{ height: 0; background: transparent; }}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{ background: transparent; }}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-width: 24px;
    border: 2px solid transparent;
    background-clip: padding;
}}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_FAINT}; }}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{ width: 0; background: transparent; }}
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* ---- Sidebar section headers ------------------------------------ */
QFrame#SectionRow {{
    background: transparent;
    border: none;
}}
QLabel#SectionLabel {{
    background: transparent;
    color: {TEXT_FAINT};
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 1.4px;
    padding: 0 4px;
}}
QLabel#SectionCount {{
    background: transparent;
    color: {TEXT_FAINT};
    font-family: monospace;
    font-size: 10.5px;
    padding: 0 4px;
}}

/* ---- Pages empty card ------------------------------------------- */
QFrame#PagesEmpty {{
    background: transparent;
    border: 1px dashed {BORDER_STRONG};
    border-radius: 10px;
    padding: 14px 12px;
}}
QLabel#PagesEmptyText {{
    background: transparent;
    color: {TEXT_FAINT};
    font-size: 11.5px;
}}
QLabel#PagesEmptyMono {{
    background: transparent;
    color: {TEXT_FAINT};
    font-family: monospace;
    font-size: 10.5px;
    padding-top: 4px;
}}

/* ---- Canvas wrap + toolbar ------------------------------------- */
QFrame#CanvasWrap {{ background: {BG}; border: none; }}
QFrame#CanvasToolbar {{
    background: {SURFACE};
    border: none;
    border-bottom: 1px solid {BORDER};
    min-height: 44px;
    max-height: 44px;
}}
QLabel#Crumb {{
    background: transparent;
    color: {TEXT_FAINT};
    font-family: monospace;
    font-size: 11px;
}}
QLabel#CrumbActive {{
    background: transparent;
    color: {TEXT_DIM};
    font-family: monospace;
    font-size: 11px;
}}
QLabel#CrumbSep {{
    background: transparent;
    color: {TEXT_FAINT};
    font-family: monospace;
    font-size: 11px;
    padding: 0 4px;
}}
QFrame#ToolbarGroup {{
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
}}
QToolButton#IconBtn {{
    background: transparent;
    border: none;
    border-radius: {RADIUS_SM};
    color: {TEXT_DIM};
    padding: 0;
    min-width: 28px; max-width: 28px;
    min-height: 24px; max-height: 24px;
    font-size: 12px;
}}
QToolButton#IconBtn:hover:!disabled {{
    background: {SURFACE_2};
    color: {TEXT};
}}
QToolButton#IconBtn:disabled {{ color: {TEXT_FAINT}; }}
QLabel#ZoomReadout {{
    background: transparent;
    color: {TEXT_DIM};
    font-family: monospace;
    font-size: 11px;
    padding: 0 6px;
    min-width: 44px;
    qproperty-alignment: 'AlignCenter';
}}

/* ---- Drop card (empty state) ------------------------------------ */
QFrame#DropWrap {{ background: transparent; border: none; }}
QFrame#DropCard {{
    background: {SURFACE};
    border: 2px dashed {BORDER_STRONG};
    border-radius: 18px;
}}
QLabel#DropGlyph {{
    background: {SURFACE_3};
    border: 1px solid {BORDER_STRONG};
    border-radius: 14px;
    color: {ACCENT};
    font-size: 26px;
    qproperty-alignment: 'AlignCenter';
    min-width: 56px; max-width: 56px;
    min-height: 56px; max-height: 56px;
}}
QLabel#DropTitle {{
    background: transparent;
    color: {TEXT};
    font-size: 19px;
    font-weight: 600;
}}
QLabel#DropBody {{
    background: transparent;
    color: {TEXT_DIM};
    font-size: 13px;
}}
QPushButton#PrimaryBtn {{
    background: {ACCENT};
    color: #0a0a0f;
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS};
    padding: 7px 16px;
    font-size: 13px;
    font-weight: 600;
    min-height: 30px;
}}
QPushButton#PrimaryBtn:hover {{ background: #6cf0db; }}
QPushButton#PrimaryBtn:pressed {{ background: #3fd9c1; }}
QPushButton#GhostBtn {{
    background: rgba(255, 255, 255, 0.02);
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    padding: 7px 16px;
    font-size: 13px;
    min-height: 30px;
}}
QPushButton#GhostBtn:hover:!disabled {{
    background: {SURFACE_2};
    color: {TEXT};
    border-color: {BORDER_STRONG};
}}
QPushButton#GhostBtn:disabled {{ color: {TEXT_FAINT}; }}
QLabel#DropMeta {{
    background: transparent;
    color: {TEXT_FAINT};
    font-family: monospace;
    font-size: 10.5px;
    letter-spacing: 0.4px;
}}

/* ---- Status bar segments --------------------------------------- */
QLabel#StatusOK {{
    background: transparent;
    color: {GOOD};
    font-family: monospace;
    font-size: 10.5px;
    padding: 0 6px;
}}
QLabel#StatusSeg {{
    background: transparent;
    color: {TEXT_FAINT};
    font-family: monospace;
    font-size: 10.5px;
    padding: 0 6px;
}}
QLabel#StatusTool {{
    background: transparent;
    color: {TEXT_FAINT};
    font-family: monospace;
    font-size: 10.5px;
    padding: 0 6px;
}}
QLabel#StatusToolName {{
    background: transparent;
    color: {ACCENT};
    font-family: monospace;
    font-size: 10.5px;
    padding: 0 0 0 2px;
}}
QFrame#StatusSep {{
    background: {BORDER};
    min-width: 1px; max-width: 1px;
    min-height: 12px; max-height: 12px;
}}
"""


def color_swatch_qss(r: int, g: int, b: int) -> str:
    """Per-button QSS for the format toolbar's 'A' color swatch.

    Keeps the shared toolbar QToolButton geometry but overrides the
    foreground color to reflect the selected FreeText's text color.
    """
    return (
        f"QToolButton {{"
        f"  color: rgb({r},{g},{b});"
        f"  padding: 5px 10px;"
        f"  margin: 0 1px;"
        f"  border: 1px solid transparent;"
        f"  border-radius: {RADIUS_SM};"
        f"  min-width: 24px;"
        f"  background: transparent;"
        f"}}"
        f"QToolButton:hover {{"
        f"  background: {SURFACE_2};"
        f"  border-color: {BORDER_STRONG};"
        f"}}"
    )
