from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

import pymupdf
import pypdfium2 as pdfium
from PySide6.QtCore import QRect, Qt, QSettings, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontDatabase,
    QIcon,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__, theme, updater
from .canvas import PageCanvas
from .chrome import CoveTitleBar, FramelessResizer
from .document import BubbleEdit, Document, FreeText, RedactionEdit
from .overlay import append_balloon_key_pages, export_pages, save
from .render import page_info, render_page
from .tools import (
    AddImageTool,
    BubbleTool,
    EditTextTool,
    FreeTextTool,
    RedactTool,
    SelectTool,
    SignatureTool,
    TextPlusTool,
)


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICON_PATH = ASSETS_DIR / "cove_icon.png"

# Supported image formats for the "Insert Images as Pages" picker and the
# page-list image drop. Mirrors AddImageTool's filter so the two entry
# points accept the same files.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp")
_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.bmp);;All files (*)"
# Default page size for imported images that are smaller than Letter at
# 72 dpi — image is centered onto the page with aspect preserved.
_DEFAULT_PAGE_PT = (612.0, 792.0)
# Cap a single import operation. 50 high-res photos already costs a lot
# of memory and disk; beyond that we'd rather warn the user.
_MAX_IMAGE_IMPORT = 50

# Recent-documents store. Persists across launches via QSettings; only
# absolute file paths are stored (no contents, hashes, or extra metadata).
_RECENT_KEY = "recentFiles"
_RECENT_CAP = 8
# Cap on the number of recents shown directly on the empty drop card —
# the full list is always available under File → Open Recent.
_RECENT_CARD_VISIBLE = 5


def _rects_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """Axis-aligned rect intersection in PDF points (bottom-left origin
    or top-left — orientation doesn't matter for AABB overlap)."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or ax0 >= bx1 or ay1 <= by0 or ay0 >= by1)


def _segment_crosses_rect(
    p1: tuple[float, float],
    p2: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> bool:
    """Liang-Barsky parametric clip: True iff the closed segment p1-p2
    has any point inside the AABB ``rect``."""
    x0, y0, x1, y1 = rect
    px0, py0 = p1
    px1, py1 = p2
    t_min, t_max = 0.0, 1.0

    dx = px1 - px0
    if dx == 0:
        if px0 < x0 or px0 > x1:
            return False
    else:
        tx0 = (x0 - px0) / dx
        tx1 = (x1 - px0) / dx
        if tx0 > tx1:
            tx0, tx1 = tx1, tx0
        t_min = max(t_min, tx0)
        t_max = min(t_max, tx1)
        if t_min > t_max:
            return False

    dy = py1 - py0
    if dy == 0:
        if py0 < y0 or py0 > y1:
            return False
    else:
        ty0 = (y0 - py0) / dy
        ty1 = (y1 - py0) / dy
        if ty0 > ty1:
            ty0, ty1 = ty1, ty0
        t_min = max(t_min, ty0)
        t_max = min(t_max, ty1)
        if t_min > t_max:
            return False

    return True


def _bubble_redacted_by(
    bubble: BubbleEdit,
    redact_rects: list[tuple[float, float, float, float]],
) -> bool:
    """True if any rect overlaps the bubble's marker bbox or its leader
    segment (circle center → leader anchor). Caller must filter
    ``redact_rects`` to the bubble's page."""
    for r in redact_rects:
        if _rects_intersect(bubble.bbox, r):
            return True
    if bubble.leader_anchor is not None:
        cx = (bubble.bbox[0] + bubble.bbox[2]) / 2
        cy = (bubble.bbox[1] + bubble.bbox[3]) / 2
        for r in redact_rects:
            if _segment_crosses_rect((cx, cy), bubble.leader_anchor, r):
                return True
    return False


def _fit_centered(
    target: QRect,
    src_w: int,
    src_h: int,
) -> QRect:
    """Largest ``target``-aligned rect with the same aspect as
    ``src_w × src_h``, centered inside ``target``. Used by the physical
    print path so a landscape page on portrait paper gets letterboxed
    instead of stretched."""
    if src_w <= 0 or src_h <= 0 or target.width() <= 0 or target.height() <= 0:
        return QRect(target)
    if src_w * target.height() > src_h * target.width():
        fitted_w = target.width()
        fitted_h = round(target.width() * src_h / src_w)
    else:
        fitted_h = target.height()
        fitted_w = round(target.height() * src_w / src_h)
    dx = (target.width() - fitted_w) // 2
    dy = (target.height() - fitted_h) // 2
    return QRect(target.x() + dx, target.y() + dy, fitted_w, fitted_h)


class _PageList(QListWidget):
    """Page list that also accepts dropped image files.

    Image URL drops emit ``imagesDropped`` so the main window can append
    one new PDF page per image. PDF and unsupported drops are ignored
    here so they fall through to the main window's drag/drop, which is
    the only path that opens or replaces the active document.
    """

    imagesDropped = Signal(list)

    def __init__(self, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._image_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._image_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self._image_paths(event)
        if paths:
            event.acceptProposedAction()
            self.imagesDropped.emit(paths)
        else:
            event.ignore()

    @staticmethod
    def _image_paths(event) -> list[Path]:  # noqa: ANN001
        md = event.mimeData()
        if not md.hasUrls():
            return []
        out: list[Path] = []
        for url in md.urls():
            p = url.toLocalFile()
            if p and Path(p).suffix.lower() in _IMAGE_EXTS:
                out.append(Path(p))
        return out


@dataclass
class _Tab:
    """Per-tab state. Each open document gets one of these. The
    ``QTabWidget`` index for this tab matches its index in
    ``MainWindow._tabs``; the canvas widget is what the tab page
    actually shows. ``blank_tmp_dir`` is owned by this tab when the
    document was created via File → New (or rebased through Insert
    Images as Pages…) — the dir is reaped on tab close, on the next
    rebase, or on app exit."""

    doc: Document
    canvas: PageCanvas
    blank_tmp_dir: Path | None = None


_CURSOR_SVG_TMPL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path d="M5 3l14 9-6.5 1.5L16 21l-3 1.5-3.5-7.5L4 18z"'
    ' fill="{color}"/></svg>'
)


def _cursor_pixmap(color: str, size: int = 18) -> QPixmap:
    svg_bytes = _CURSOR_SVG_TMPL.format(color=color).encode()
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    renderer = QSvgRenderer(svg_bytes)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    return pm


# Curated font list shown at the top of the format-bar Family combo.
#
# Each entry is (display_name, [preferred_aliases...]). The dropdown
# shows ``display_name``; the canvas stores whichever alias is actually
# installed so Qt renders the right glyphs without proprietary font
# bundling. PDF base-14 names (Helvetica/Times/Courier) always show
# even without an exact-name font installed because the save layer
# resolves them through ``_resolve_font``.
#
# The Microsoft alternatives below use Google's Chrome OS open-source
# Liberation/Tinos/Cousine/Carlito/Caladea families when MS fonts
# aren't installed. We do NOT bundle or download these families —
# users install them via their OS package manager.
_CURATED_FONTS: list[tuple[str, list[str]]] = [
    ("Helvetica",       ["Helvetica", "Arial", "Liberation Sans",
                         "Arimo", "Nimbus Sans", "DejaVu Sans"]),
    ("Times",           ["Times", "Times New Roman", "Liberation Serif",
                         "Tinos", "Nimbus Roman", "DejaVu Serif"]),
    ("Courier",         ["Courier", "Courier New", "Liberation Mono",
                         "Cousine", "Nimbus Mono", "DejaVu Sans Mono"]),
    ("Arial",           ["Arial", "Liberation Sans",
                         "Arimo", "Nimbus Sans"]),
    ("Times New Roman", ["Times New Roman", "Liberation Serif",
                         "Tinos", "Nimbus Roman"]),
    ("Courier New",     ["Courier New", "Liberation Mono",
                         "Cousine", "Nimbus Mono"]),
    ("Calibri",         ["Calibri", "Carlito"]),
    ("Cambria",         ["Cambria", "Caladea"]),
    ("Georgia",         ["Georgia", "Tinos", "DejaVu Serif"]),
    ("Verdana",         ["Verdana", "DejaVu Sans"]),
    ("Tahoma",          ["Tahoma", "DejaVu Sans"]),
    ("Noto Sans",       ["Noto Sans"]),
    ("Noto Serif",      ["Noto Serif"]),
    ("DejaVu Sans",     ["DejaVu Sans"]),
    ("DejaVu Serif",    ["DejaVu Serif"]),
    ("Liberation Sans", ["Liberation Sans"]),
    ("Liberation Serif",["Liberation Serif"]),
    ("Liberation Mono", ["Liberation Mono"]),
]
# These three friendly names always show — save resolves to base-14
# even if no system font matches.
_PDF_BASE14_FRIENDLY = ("Helvetica", "Times", "Courier")

# Family-name substrings (lowercase) that mark a font as not appropriate
# for general body text. These are filtered out of the "everything else"
# group so the dropdown doesn't surface symbol / icon / emoji / dingbat
# fonts (unreadable previews) or language-script-specific subsets that
# the user is unlikely to want for a Latin PDF.
_NON_TEXT_TOKENS = (
    # Symbol / icon / pseudo-glyph fonts.
    "symbol", "icon", "emoji", "math", "music", "barcode",
    "wingdings", "webdings", "marlett", "dingbat", "ornament",
    "musical", "mt extra", "braille", "ocr",
    # Script-specific subsets. These ARE text fonts, but their glyph
    # previews aren't Latin. Filtering keeps the dropdown short and
    # readable. Users with a real script need can type the family.
    "arabic", "armenian", "bengali", "devanagari", "ethiopic",
    "georgian", "gujarati", "gurmukhi", "hebrew", "kannada",
    "khmer", "lao", "malayalam", "mongolian", "myanmar", "oriya",
    "sinhala", "syriac", "tamil", "telugu", "thaana", "thai",
    "tibetan", "cherokee", "hanifi", "vai", "tifinagh", "yi ",
    "n'ko", "nko", "javanese", "balinese", "buginese", "buhid",
    "carian", "chakma", "cham", "duployan", "glagolitic", "gothic",
    "kayah", "lepcha", "limbu", "lisu", "lycian", "lydian",
    "miao", "modi", "mro", "newa", "ol chiki", "osage", "osmanya",
    "phags", "rejang", "runic", "samaritan", "saurashtra", "shavian",
    "siddham", "sora", "sundanese", "sylo", "tagalog", "tagbanwa",
    "takri", "tai ", "tirhuta", "ugaritic", "vai", "wancho",
    "phoenician", "imperial", "old ", "linear ", "meroitic",
    "manichaean", "mende", "kharoshthi", "kaithi", "brahmi",
    "ahom", "elbasan", "hatran", "mahajani", "marchen", "multani",
    "nabataean", "nushu", "pahlavi", "palmyrene", "parthian",
    "pau cin hau", "psalter", "sharada", "soyombo", "tangut",
    "warang", "anatolian", "bamum", "bassa", "batak", "bhaiksuki",
    "caucasian", "cuneiform", "egyptian", "hanuno", "hieroglyph",
    "katakana", "hiragana", "hangul", "kufi", "naskh",
    # CJK / language-tagged variants.
    "cjk", "jp", "kr", "sc", "tc", "hk", "japanese", "korean",
    "chinese",
    # Internal / private.
    "noto color",
)


def _is_text_font(family: str) -> bool:
    """Filter for the secondary 'all fonts' group below the curated tier.
    Drops private fonts (leading dot), CJK-only families that won't read
    in most PDFs, and obvious symbol / icon / barcode fonts."""
    if not family or family.startswith("."):
        return False
    lower = family.casefold()
    return not any(tok in lower for tok in _NON_TEXT_TOKENS)


def _resolve_curated(installed: set[str]) -> list[tuple[str, str]]:
    """Walk the curated table and emit ``(display_name, installed_family)``
    pairs for entries that have a usable mapping on this system. PDF
    base-14 entries always appear — save will translate them. Other
    entries appear only when at least one alias is installed."""
    out: list[tuple[str, str]] = []
    seen_display: set[str] = set()
    for display, aliases in _CURATED_FONTS:
        if display in seen_display:
            continue
        installed_match = next((a for a in aliases if a in installed), None)
        if installed_match is not None:
            out.append((display, installed_match))
            seen_display.add(display)
        elif display in _PDF_BASE14_FRIENDLY:
            # Save layer maps these to base-14 fonts even with no system
            # font of that name — keep them visible.
            out.append((display, display))
            seen_display.add(display)
    return out


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Cove PDF Editor v{__version__}")
        self.resize(1300, 820)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self._frameless_resizer = FramelessResizer(self)
        self.setMouseTracking(True)
        # Single source of truth for the app-shell look. Per-widget
        # setStyleSheet calls are intentionally avoided so this sheet
        # drives the entire chrome.
        self.setStyleSheet(theme.GLOBAL_QSS)
        # ``self._doc`` / ``self._canvas`` are aliases that follow the
        # active tab. Reassigned in ``_on_active_tab_changed``; ``None``
        # when zero tabs are open. Existing call sites that read these
        # attributes keep working unchanged.
        self._doc: Document | None = None
        self._canvas: PageCanvas | None = None
        self._tabs: list[_Tab] = []
        self._tool_buttons: dict[str, QPushButton] = {}
        self._build_ui()
        self._build_menu()
        self._install_global_shortcuts()
        self.setAcceptDrops(True)
        # Recent-files surface on the empty drop card. Built after the UI
        # so the section widgets exist; built before any window operations
        # so first paint shows the populated state on subsequent launches.
        self._refresh_drop_card_recents()
        self._updater = updater.UpdateController(
            parent=self,
            current_version=__version__,
            repo="Sin213/cove-pdf-editor",
            app_display_name="Cove PDF Editor",
            cache_subdir="cove-pdf-editor",
        )
        QTimer.singleShot(4000, self._updater.check)

    # ---------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        # Custom status bar lives at the bottom of the central layout
        # (see _build_status_bar). The QMainWindow's native status bar
        # slot is unused — we don't call setStatusBar(). The `_status`
        # attribute exposes a showMessage(text, ms) shim for backward
        # compatibility with the existing showMessage call sites.
        self._status = _StatusShim(self)

        self._open_act = QAction("Open PDF…", self)
        self._open_act.setShortcut(QKeySequence.Open)
        self._open_act.setToolTip("Open a PDF (Ctrl+O)")
        self._open_act.triggered.connect(self._on_open)
        self._save_act = QAction("Save As…", self)
        self._save_act.setShortcut(QKeySequence.Save)
        self._save_act.setToolTip("Save the edited PDF (Ctrl+S)")
        self._save_act.setEnabled(False)
        self._save_act.triggered.connect(self._on_save)

        # Formatting toolbar (hidden until a PDF is open). The toolbar is
        # placed inside the central layout instead of QMainWindow's
        # toolbar area, because we are reparenting the menu bar inside
        # the central widget — the toolbar area would otherwise sit
        # above our in-app title band.
        self._fmt_bar = self._build_format_bar()
        self._fmt_bar.setVisible(False)
        self._selected_edit: FreeText | None = None

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 0. Custom titlebar (frameless chrome).
        self._titlebar = CoveTitleBar(
            self,
            icon_path=str(ICON_PATH) if ICON_PATH.exists() else None,
            title="Cove PDF Editor",
            version=f"v{__version__}",
        )
        outer.addWidget(self._titlebar)

        # 1. Menu bar.
        self._menubar = QMenuBar()
        self._menubar.setNativeMenuBar(False)
        outer.addWidget(self._menubar)

        # 2. Format toolbar (hidden initially; show on first PDF load).
        outer.addWidget(self._fmt_bar)

        # 4. Main horizontal split.
        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)
        split.addWidget(self._build_sidebar())
        split.addWidget(self._build_canvas_wrap(), stretch=1)
        outer.addLayout(split, stretch=1)

        # 5. Custom status bar.
        outer.addWidget(self._build_status_bar())

        self._update_tool_enabled(False)
        self._update_canvas_toolbar_state(False)
        self._set_pages_count(0)
        self._update_crumb(None, None)
        self._set_status_tool("—")
        self._set_status_page(0, 0)

    # ---- Sidebar ----------------------------------------------------

    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(240)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(14, 16, 14, 0)
        side_layout.setSpacing(18)

        # ---- TOOLS section -----------------------------------------
        tools_section = QFrame()
        tools_section.setObjectName("ToolsSection")
        tools_lay = QVBoxLayout(tools_section)
        tools_lay.setContentsMargins(0, 0, 0, 0)
        tools_lay.setSpacing(2)
        tools_lay.addWidget(self._make_section_row("TOOLS", "5"))

        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        for icon, name, hot, key, factory, tip in (
            (None, "Select",    "V",  "select",    SelectTool,
             "Select objects to move, resize, or delete"),
            ("📝", "Edit Text", "E",  "edit_text", EditTextTool,
             "Double-click searchable PDF text to replace it"),
            ("🅰", "Add Text",  "T",  "freetext",  FreeTextTool,
             "Drag a rectangle to add a new text box"),
            ("➕", "Text Plus", "⇧T", "text_plus", TextPlusTool,
             "Click to drop quick text entries — good for filling forms"),
            ("🖼", "Add Image", "I",  "image",     AddImageTool,
             "Pick a PNG or JPG and drag a rectangle to place it"),
            ("✍", "Signature", "S",  "signature", SignatureTool,
             "Place your saved signature; hold Shift to pick a different image"),
            ("①", "Balloon",  "B",  "bubble",    BubbleTool,
             "Drag from a feature to drop a numbered balloon (just-click for no leader)"),
            ("⬛", "Redact",   "R",  "redact",    RedactTool,
             "Drag a rectangle to permanently remove its content on save"),
        ):
            tools_lay.addWidget(
                self._make_tool_row(key, icon, name, hot, factory, tip)
            )

        side_layout.addWidget(tools_section)

        # ---- PAGES section ----------------------------------------
        pages_section = QFrame()
        pages_section.setObjectName("PagesSection")
        pages_lay = QVBoxLayout(pages_section)
        pages_lay.setContentsMargins(0, 0, 0, 0)
        pages_lay.setSpacing(6)

        self._pages_count_label = QLabel("0")
        self._pages_count_label.setObjectName("SectionCount")
        pages_lay.addWidget(
            self._make_section_row("PAGES", count_widget=self._pages_count_label)
        )

        # Stack: empty card vs. populated page list. Switched in
        # _set_pages_count().
        self._pages_stack = QStackedWidget()
        self._pages_stack.setObjectName("PagesStack")
        self._pages_empty = self._build_pages_empty()
        self._pages_stack.addWidget(self._pages_empty)
        self.page_list = _PageList()
        self.page_list.setObjectName("PageList")
        self.page_list.currentRowChanged.connect(self._on_page_changed)
        self.page_list.imagesDropped.connect(self._insert_image_paths_as_pages)
        self.page_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._pages_stack.addWidget(self.page_list)
        pages_lay.addWidget(self._pages_stack, stretch=1)

        side_layout.addWidget(pages_section, stretch=1)
        return side

    def _make_section_row(
        self,
        label: str,
        count_text: str | None = None,
        count_widget: QLabel | None = None,
    ) -> QFrame:
        row = QFrame()
        row.setObjectName("SectionRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(4, 0, 4, 4)
        lay.setSpacing(0)
        lbl = QLabel(label)
        lbl.setObjectName("SectionLabel")
        lay.addWidget(lbl)
        lay.addStretch(1)
        if count_widget is not None:
            lay.addWidget(count_widget)
        elif count_text is not None:
            cnt = QLabel(count_text)
            cnt.setObjectName("SectionCount")
            lay.addWidget(cnt)
        return row

    def _make_tool_row(
        self,
        key: str,
        icon: str,
        name: str,
        hot: str,
        factory,  # noqa: ANN001
        tooltip: str,
    ) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName("ToolButton")
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(tooltip)

        lay = QHBoxLayout(btn)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(11)

        ico_lbl = QLabel()
        ico_lbl.setObjectName("ToolIcon")
        ico_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        if icon is None:
            ico_lbl.setPixmap(_cursor_pixmap(theme.TEXT_DIM))
            ico_lbl.setProperty("_cursor_svg", True)
        else:
            ico_lbl.setText(icon)
        name_lbl = QLabel(name)
        name_lbl.setObjectName("ToolName")
        name_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        hot_lbl = QLabel(hot)
        hot_lbl.setObjectName("HotKey")
        hot_lbl.setAlignment(Qt.AlignCenter)
        hot_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

        lay.addWidget(ico_lbl)
        lay.addWidget(name_lbl)
        lay.addStretch(1)
        lay.addWidget(hot_lbl)

        self._tool_group.addButton(btn)
        btn.clicked.connect(lambda: self._select_tool(key, factory))
        # Mirror :checked onto the children's `active` dynamic property
        # so QSS can flip the icon / name / hotkey badge to the accent
        # variant. QSS can't traverse parent states from a child label.
        btn.toggled.connect(lambda on, b=btn: self._sync_tool_row_active(b, on))
        self._tool_buttons[key] = btn
        return btn

    def _sync_tool_row_active(self, btn: QPushButton, active: bool) -> None:
        flag = "true" if active else "false"
        for child in btn.findChildren(QLabel):
            if child.objectName() in {"ToolIcon", "ToolName", "HotKey"}:
                child.setProperty("active", flag)
                child.style().unpolish(child)
                child.style().polish(child)
                if child.property("_cursor_svg"):
                    color = theme.ACCENT if active else theme.TEXT_DIM
                    child.setPixmap(_cursor_pixmap(color))

    def _build_pages_empty(self) -> QFrame:
        card = QFrame()
        card.setObjectName("PagesEmpty")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 14, 10, 14)
        lay.setSpacing(2)
        lay.setAlignment(Qt.AlignCenter)
        line1 = QLabel("📄")
        line1.setAlignment(Qt.AlignCenter)
        line1.setObjectName("PagesEmptyText")
        line2 = QLabel("No pages yet")
        line2.setAlignment(Qt.AlignCenter)
        line2.setObjectName("PagesEmptyText")
        line3 = QLabel("open a pdf to begin")
        line3.setAlignment(Qt.AlignCenter)
        line3.setObjectName("PagesEmptyMono")
        lay.addWidget(line1)
        lay.addWidget(line2)
        lay.addWidget(line3)
        return card

    def _set_pages_count(self, n: int) -> None:
        self._pages_count_label.setText(str(n))
        if n > 0:
            self._pages_stack.setCurrentWidget(self.page_list)
        else:
            self._pages_stack.setCurrentWidget(self._pages_empty)

    # ---- Canvas wrap + toolbar --------------------------------------

    def _build_canvas_wrap(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("CanvasWrap")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        lay.addWidget(self._build_canvas_toolbar())

        # Two states: the empty "drop a PDF" card (no tabs open) and the
        # multi-document QTabWidget (one tab per open document). Switched
        # in ``_sync_canvas_stack``.
        self._canvas_stack = QStackedWidget()
        self._canvas_stack.setObjectName("CanvasStack")
        self._drop_wrap = self._build_drop_card()
        self._canvas_stack.addWidget(self._drop_wrap)
        self._tab_widget = QTabWidget()
        self._tab_widget.setObjectName("DocTabs")
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.setMovable(False)
        self._tab_widget.setDocumentMode(True)
        self._tab_widget.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_widget.currentChanged.connect(self._on_active_tab_changed)
        self._canvas_stack.addWidget(self._tab_widget)
        lay.addWidget(self._canvas_stack, stretch=1)
        return wrap

    def _sync_canvas_stack(self) -> None:
        """Show the drop card when no tabs are open; otherwise the tab
        widget. Called whenever a tab is added or removed."""
        if self._tabs:
            self._canvas_stack.setCurrentWidget(self._tab_widget)
        else:
            self._canvas_stack.setCurrentWidget(self._drop_wrap)

    def _build_canvas_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("CanvasToolbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(8)

        # Crumb area: doc name / page label.
        self._crumb_doc = QLabel("Untitled")
        self._crumb_doc.setObjectName("CrumbActive")
        crumb_sep = QLabel("/")
        crumb_sep.setObjectName("CrumbSep")
        self._crumb_page = QLabel("—")
        self._crumb_page.setObjectName("Crumb")
        lay.addWidget(self._crumb_doc)
        lay.addWidget(crumb_sep)
        lay.addWidget(self._crumb_page)
        lay.addStretch(1)

        # Page-nav group: prev / readout / next.
        nav_group = QFrame()
        nav_group.setObjectName("ToolbarGroup")
        nav_lay = QHBoxLayout(nav_group)
        nav_lay.setContentsMargins(3, 3, 3, 3)
        nav_lay.setSpacing(2)
        self._nav_prev = self._make_icon_btn("‹", "Previous page")
        self._nav_next = self._make_icon_btn("›", "Next page")
        self._nav_readout = QLabel("0 / 0")
        self._nav_readout.setObjectName("ZoomReadout")
        self._nav_readout.setAlignment(Qt.AlignCenter)
        self._nav_prev.clicked.connect(lambda: self._step_page(-1))
        self._nav_next.clicked.connect(lambda: self._step_page(+1))
        nav_lay.addWidget(self._nav_prev)
        nav_lay.addWidget(self._nav_readout)
        nav_lay.addWidget(self._nav_next)
        lay.addWidget(nav_group)

        # Zoom group — placeholder readout, all buttons disabled.
        zoom_group = QFrame()
        zoom_group.setObjectName("ToolbarGroup")
        zoom_lay = QHBoxLayout(zoom_group)
        zoom_lay.setContentsMargins(3, 3, 3, 3)
        zoom_lay.setSpacing(2)
        self._zoom_out = self._make_icon_btn("−", "Zoom out")
        self._zoom_readout = QLabel("100%")
        self._zoom_readout.setObjectName("ZoomReadout")
        self._zoom_readout.setAlignment(Qt.AlignCenter)
        self._zoom_in = self._make_icon_btn("+", "Zoom in")
        self._zoom_fit = self._make_icon_btn("⤢", "Fit page")
        for b in (self._zoom_out, self._zoom_in, self._zoom_fit):
            b.setEnabled(False)
        zoom_lay.addWidget(self._zoom_out)
        zoom_lay.addWidget(self._zoom_readout)
        zoom_lay.addWidget(self._zoom_in)
        zoom_lay.addWidget(self._zoom_fit)
        lay.addWidget(zoom_group)

        # History group — undo / redo (wired to existing handlers).
        hist_group = QFrame()
        hist_group.setObjectName("ToolbarGroup")
        hist_lay = QHBoxLayout(hist_group)
        hist_lay.setContentsMargins(3, 3, 3, 3)
        hist_lay.setSpacing(2)
        self._hist_undo = self._make_icon_btn("↶", "Undo (Ctrl+Z)")
        self._hist_redo = self._make_icon_btn("↷", "Redo (Ctrl+Y)")
        self._hist_undo.clicked.connect(self._do_undo)
        self._hist_redo.clicked.connect(self._do_redo)
        hist_lay.addWidget(self._hist_undo)
        hist_lay.addWidget(self._hist_redo)
        lay.addWidget(hist_group)
        return bar

    def _make_icon_btn(self, glyph: str, tip: str) -> QToolButton:
        btn = QToolButton()
        btn.setObjectName("IconBtn")
        btn.setText(glyph)
        btn.setToolTip(tip)
        btn.setCursor(Qt.PointingHandCursor)
        return btn

    def _step_page(self, delta: int) -> None:
        if self._doc is None:
            return
        cur = self.page_list.currentRow()
        if cur < 0:
            cur = 0
        target = max(0, min(self._doc.page_count - 1, cur + delta))
        if target != cur:
            self.page_list.setCurrentRow(target)

    def _update_canvas_toolbar_state(self, has_doc: bool) -> None:
        for b in (self._nav_prev, self._nav_next, self._hist_undo, self._hist_redo):
            b.setEnabled(has_doc)
        # Zoom buttons stay disabled — placeholders for unimplemented zoom.

    def _update_crumb(self, doc_name: str | None, page_label: str | None) -> None:
        self._crumb_doc.setText(doc_name if doc_name else "Untitled")
        self._crumb_page.setText(page_label if page_label else "—")

    # ---- Drop card --------------------------------------------------

    def _build_drop_card(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("DropWrap")
        wrap_lay = QVBoxLayout(wrap)
        wrap_lay.setContentsMargins(40, 40, 40, 40)
        wrap_lay.addStretch(1)

        card = QFrame()
        card.setObjectName("DropCard")
        card.setMaximumWidth(560)
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(36, 36, 36, 36)
        card_lay.setSpacing(14)
        card_lay.setAlignment(Qt.AlignCenter)

        glyph = QLabel("📄")
        glyph.setObjectName("DropGlyph")
        glyph.setAlignment(Qt.AlignCenter)

        title = QLabel("Drop a PDF to begin")
        title.setObjectName("DropTitle")
        title.setAlignment(Qt.AlignCenter)

        body = QLabel(
            "Drag any PDF onto this window — or press Ctrl+O to open one. "
            "Then pick a tool on the left and click or drag on the page."
        )
        body.setObjectName("DropBody")
        body.setAlignment(Qt.AlignCenter)
        body.setWordWrap(True)
        body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.setAlignment(Qt.AlignCenter)
        open_btn = QPushButton("Open PDF")
        open_btn.setObjectName("PrimaryBtn")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(self._on_open)
        new_btn = QPushButton("New blank PDF")
        new_btn.setObjectName("GhostBtn")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(self._on_new)
        actions.addWidget(open_btn)
        actions.addWidget(new_btn)

        meta = QLabel(".pdf  •  up to 200 MB  •  processed locally")
        meta.setObjectName("DropMeta")
        meta.setAlignment(Qt.AlignCenter)

        # Optional Recent section. Hidden when there are no recent files
        # so the original card layout is preserved on first launch.
        self._recent_card_section = QFrame()
        self._recent_card_section.setObjectName("DropRecent")
        rc_lay = QVBoxLayout(self._recent_card_section)
        rc_lay.setContentsMargins(0, 6, 0, 0)
        rc_lay.setSpacing(4)
        rc_label = QLabel("Recent")
        rc_label.setObjectName("DropRecentLabel")
        rc_label.setAlignment(Qt.AlignCenter)
        rc_lay.addWidget(rc_label)
        self._recent_btn_layout = QVBoxLayout()
        self._recent_btn_layout.setContentsMargins(0, 0, 0, 0)
        self._recent_btn_layout.setSpacing(2)
        rc_lay.addLayout(self._recent_btn_layout)
        self._recent_card_section.setVisible(False)

        card_lay.addWidget(glyph, alignment=Qt.AlignCenter)
        card_lay.addWidget(title)
        card_lay.addWidget(body, alignment=Qt.AlignCenter)
        card_lay.addLayout(actions)
        card_lay.addWidget(self._recent_card_section)
        card_lay.addWidget(meta)

        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(card)
        h.addStretch(1)
        wrap_lay.addLayout(h)
        wrap_lay.addStretch(2)
        return wrap

    # ---- Status bar -------------------------------------------------

    def _build_status_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(8)

        self._status_ok = QLabel("● Ready")
        self._status_ok.setObjectName("StatusOK")

        self._status_tool_label = QLabel("tool:")
        self._status_tool_label.setObjectName("StatusTool")
        self._status_tool_name = QLabel("—")
        self._status_tool_name.setObjectName("StatusToolName")

        self._status_zoom = QLabel("zoom: 100%")
        self._status_zoom.setObjectName("StatusSeg")

        self._status_message = QLabel("")
        self._status_message.setObjectName("StatusSeg")

        self._status_objects = QLabel("0 objects")
        self._status_objects.setObjectName("StatusSeg")
        self._status_page_label = QLabel("page 0 / 0")
        self._status_page_label.setObjectName("StatusSeg")

        lay.addWidget(self._status_ok)
        lay.addWidget(self._make_status_sep())
        lay.addWidget(self._status_tool_label)
        lay.addWidget(self._status_tool_name)
        lay.addWidget(self._make_status_sep())
        lay.addWidget(self._status_zoom)
        lay.addWidget(self._make_status_sep())
        lay.addWidget(self._status_message, stretch=1)
        lay.addWidget(self._status_objects)
        lay.addWidget(self._make_status_sep())
        lay.addWidget(self._status_page_label)

        # Wire the showMessage shim to the message label.
        self._status.set_target(self._status_message)
        bar.setMinimumHeight(28)
        bar.setMaximumHeight(28)
        return bar

    def _make_status_sep(self) -> QFrame:
        sep = QFrame()
        sep.setObjectName("StatusSep")
        return sep

    def _set_status_tool(self, name: str) -> None:
        self._status_tool_name.setText(name if name else "—")

    def _set_status_page(self, current: int, total: int) -> None:
        self._status_page_label.setText(f"page {current} / {total}")
        self._nav_readout.setText(f"{current} / {total}")

    # --------------------------------------------------------- menu

    def _build_menu(self) -> None:
        # Native macOS menu off so the global QSS in theme.py styles the
        # menu bar consistently across platforms. The menu bar instance
        # was created in _build_ui and is reparented inside the central
        # widget below the in-app title band.
        file_menu = self._menubar.addMenu("&File")

        self._new_act = QAction("&New…", self)
        self._new_act.setShortcut(QKeySequence.New)
        self._new_act.triggered.connect(self._on_new)
        file_menu.addAction(self._new_act)

        file_menu.addAction(self._open_act)

        self._recent_menu = file_menu.addMenu("Open &Recent")
        # Repopulate just before the user sees it so we always reflect
        # the current QSettings state — not whatever was on disk at
        # construction time.
        self._recent_menu.aboutToShow.connect(self._refresh_recent_menu)
        self._refresh_recent_menu()

        self._insert_images_act = QAction("Insert Images as Pages…", self)
        self._insert_images_act.setEnabled(False)
        self._insert_images_act.triggered.connect(self._on_insert_images_as_pages)
        file_menu.addAction(self._insert_images_act)

        self._balloon_key_act = QAction("Append Balloon &Key Page", self)
        self._balloon_key_act.setEnabled(False)
        self._balloon_key_act.setToolTip(
            "Add a page summarizing every numbered balloon and its description",
        )
        self._balloon_key_act.triggered.connect(self._on_append_balloon_key)
        file_menu.addAction(self._balloon_key_act)

        file_menu.addSeparator()

        self._save_menu_act = QAction("&Save", self)
        self._save_menu_act.setEnabled(False)
        file_menu.addAction(self._save_menu_act)

        file_menu.addAction(self._save_act)

        file_menu.addSeparator()

        export_menu = file_menu.addMenu("E&xport")
        self._export_current_act = QAction("Current Page as PDF…", self)
        self._export_current_act.setEnabled(False)
        self._export_current_act.triggered.connect(self._on_export_current)
        export_menu.addAction(self._export_current_act)
        self._export_selected_act = QAction("Selected Pages as PDF…", self)
        self._export_selected_act.setEnabled(False)
        self._export_selected_act.triggered.connect(self._on_export_selected)
        export_menu.addAction(self._export_selected_act)

        file_menu.addSeparator()

        self._print_act = QAction("&Print…", self)
        self._print_act.setShortcut(QKeySequence.Print)
        self._print_act.setEnabled(False)
        self._print_act.triggered.connect(self._on_print)
        file_menu.addAction(self._print_act)

        file_menu.addSeparator()

        self._close_act = QAction("Close PDF", self)
        self._close_act.setEnabled(False)
        file_menu.addAction(self._close_act)

        file_menu.addSeparator()

        exit_act = QAction("E&xit", self)
        exit_act.setShortcut(QKeySequence.Quit)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

    # --------------------------------------------------------- file ops

    def _install_global_shortcuts(self) -> None:
        """Install Undo/Redo as ``QAction``s on the MainWindow.

        One ``QAction`` per logical action with all bindings attached at
        once, instead of multiple ``QShortcut``s. Two separate shortcut
        objects each claiming Ctrl+Shift+Z collide with
        ``QGraphicsTextItem``'s built-in text-redo action — Qt resolves
        ``QKeySequence::Redo`` on Linux to both Ctrl+Y *and*
        Ctrl+Shift+Z, so the shortcut map sees the same combo claimed
        twice and prints "Ambiguous shortcut overload: Ctrl+Shift+Z".
        Folding everything into one QAction per logical action removes
        that competing-on-our-side registration.

        ``Qt.WindowShortcut`` (default for QActions) scopes the binding
        to MainWindow + descendants, so it fires from the canvas, the
        sidebar, the format toolbar, and the page list — but doesn't
        compete app-wide with other windows / standard-key actions.
        """
        self._undo_act = QAction("Undo", self)
        self._undo_act.setShortcut(QKeySequence("Ctrl+Z"))
        self._undo_act.setShortcutContext(Qt.WindowShortcut)
        self._undo_act.triggered.connect(self._do_undo)
        self.addAction(self._undo_act)

        # ONE QAction with BOTH redo bindings — no competing registrations.
        self._redo_act = QAction("Redo", self)
        self._redo_act.setShortcuts([
            QKeySequence("Ctrl+Y"),
            QKeySequence("Ctrl+Shift+Z"),
        ])
        self._redo_act.setShortcutContext(Qt.WindowShortcut)
        self._redo_act.triggered.connect(self._do_redo)
        self.addAction(self._redo_act)

        for seq, key, factory in (
            ("V",       "select",    SelectTool),
            ("E",       "edit_text", EditTextTool),
            ("T",       "freetext",  FreeTextTool),
            ("Shift+T", "text_plus", TextPlusTool),
            ("I",       "image",     AddImageTool),
            ("S",       "signature", SignatureTool),
            ("B",       "bubble",    BubbleTool),
            ("R",       "redact",    RedactTool),
        ):
            act = QAction(self)
            act.setShortcut(QKeySequence(seq))
            act.setShortcutContext(Qt.WindowShortcut)
            act.triggered.connect(
                lambda _=False, k=key, f=factory: self._hotkey_tool(k, f)
            )
            self.addAction(act)

    def _do_undo(self) -> None:
        if self._canvas is not None:
            self._canvas.undo()

    def _do_redo(self) -> None:
        if self._canvas is not None:
            self._canvas.redo()

    def _hotkey_tool(self, key: str, factory) -> None:  # noqa: ANN001
        if self._canvas is None or self._canvas.is_inline_editing():
            return
        btn = self._tool_buttons.get(key)
        if btn is not None and btn.isEnabled():
            btn.setChecked(True)
            self._select_tool(key, factory)

    def _confirm_discard_changes(self, tab: _Tab | None = None) -> bool:
        """Save / Discard / Cancel prompt before discarding a tab's
        unsaved edits. ``tab`` defaults to the active tab — pass an
        explicit one when handling tab-close on a non-active tab.

        Captures any in-flight inline editor first so typed-but-
        unsubmitted text counts as unsaved state. Without this, opening
        a different PDF or drag-dropping one onto the window during an
        active inline edit would silently throw away whatever the user
        was typing — ``Document.dirty`` only flips when the editor
        commits.
        """
        target = tab if tab is not None else self._active_tab()
        if target is None:
            return True
        target.canvas.commit_active_editor()
        if not target.doc.dirty:
            return True
        # Surface the prompt with the affected tab's filename so users
        # closing one of several open tabs know which doc this is about.
        reply = QMessageBox.question(
            self,
            "Unsaved Changes",
            f"{target.doc.source.name} has unsaved changes.",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply == QMessageBox.Cancel:
            return False
        if reply == QMessageBox.Save:
            self._save_tab(target)
            if target.doc.dirty:
                return False
        return True

    def _on_new(self) -> None:
        # Always opens in a fresh tab — never replaces the active one.
        self._create_and_load_blank()

    def _create_and_load_blank(self) -> None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="cove-"))
        tmp = tmp_dir / "Untitled.pdf"
        doc = pymupdf.open()
        doc.new_page(width=612, height=792)
        doc.save(str(tmp))
        doc.close()
        self._load(tmp, blank_tmp_dir=tmp_dir)

    def _discard_blank_tmp_dir(self, tab: _Tab) -> None:
        """Reap the tab-owned blank-PDF tempdir if it has one."""
        if tab.blank_tmp_dir is None:
            return
        shutil.rmtree(tab.blank_tmp_dir, ignore_errors=True)
        tab.blank_tmp_dir = None

    def _on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", "", "PDF files (*.pdf);;All files (*)",
        )
        if path:
            self._load(Path(path))

    def _load(
        self,
        path: Path,
        blank_tmp_dir: Path | None = None,
    ) -> None:
        """Open ``path`` in a new tab. ``blank_tmp_dir`` is the tempdir
        that owns ``path`` (set by File → New); cleaned up on tab close."""
        try:
            with pdfium.PdfDocument(str(path)) as doc:
                n = len(doc)
        except (OSError, ValueError, RuntimeError) as exc:
            log.warning("Could not open PDF %s: %s", path, exc)
            QMessageBox.critical(self, "Could not open PDF", str(exc))
            return
        document = Document(source=path, page_count=n)
        canvas = PageCanvas(document)
        canvas.selectionChanged.connect(self._on_canvas_selection)
        canvas.statusMessage.connect(
            lambda msg: self._status.showMessage(msg, 5000),
        )
        canvas.toolChanged.connect(self._on_canvas_tool_changed)
        # Mouse-wheel page navigation in the canvas updates the sidebar
        # row + crumb + status without re-entering set_page (block
        # signals so the sidebar's currentRowChanged doesn't bounce
        # back into the canvas, which already moved).
        canvas.pageChanged.connect(self._on_canvas_page_changed)

        tab = _Tab(doc=document, canvas=canvas, blank_tmp_dir=blank_tmp_dir)
        self._tabs.append(tab)
        # Switching to the new tab fires currentChanged →
        # _on_active_tab_changed, which reassigns self._doc / self._canvas
        # and refreshes sidebar / toolbar / title.
        idx = self._tab_widget.addTab(canvas, self._tab_label(tab))
        self._tab_widget.setTabToolTip(idx, str(path))
        self._sync_canvas_stack()
        self._tab_widget.setCurrentIndex(idx)

        # Default to Select mode so the user can click objects right away.
        select_btn = self._tool_buttons.get("select")
        if select_btn is not None:
            select_btn.setChecked(True)
        canvas.set_tool(SelectTool())
        self._status.showMessage(f"{path.name} • {n} page(s)", 6000)
        # Don't pollute Recent with /tmp blank-PDF placeholders. Real
        # opens (and Save As → ``_save_tab``) are the only sources.
        if blank_tmp_dir is None:
            self._remember_recent(path)

    def _tab_label(self, tab: _Tab) -> str:
        """Tab title: filename, plus a leading * when the doc is dirty."""
        prefix = "● " if tab.doc.dirty else ""
        return f"{prefix}{tab.doc.source.name}"

    def _refresh_tab_label(self, tab: _Tab) -> None:
        try:
            idx = self._tabs.index(tab)
        except ValueError:
            return
        self._tab_widget.setTabText(idx, self._tab_label(tab))
        self._tab_widget.setTabToolTip(idx, str(tab.doc.source))

    def _active_tab(self) -> _Tab | None:
        idx = self._tab_widget.currentIndex()
        if 0 <= idx < len(self._tabs):
            return self._tabs[idx]
        return None

    def _on_active_tab_changed(self, idx: int) -> None:
        """The currentIndex changed — point the window-level handles at
        the new active tab and refresh sidebar / toolbar / title to
        match."""
        if idx < 0 or idx >= len(self._tabs):
            self._doc = None
            self._canvas = None
            self._set_active_state(False)
            return
        tab = self._tabs[idx]
        self._doc = tab.doc
        self._canvas = tab.canvas

        # Sidebar page list mirrors the active doc's page count and
        # current page. Block signals so re-populating doesn't re-enter
        # set_page on the already-correct canvas page.
        self.page_list.blockSignals(True)
        self.page_list.clear()
        for i in range(self._doc.page_count):
            self.page_list.addItem(QListWidgetItem(f"Page {i + 1}"))
        cur = self._canvas.page_index()
        self.page_list.setCurrentRow(cur if 0 <= cur < self._doc.page_count else 0)
        self.page_list.blockSignals(False)
        self._set_pages_count(self._doc.page_count)

        self._set_active_state(True)
        self._update_crumb(self._doc.source.name, f"page {cur + 1}")
        self._set_status_page(cur + 1, self._doc.page_count)

        # Sync the toolbar's checked tool with the new active canvas's
        # current tool (canvases preserve their own tool selection).
        active_tool = getattr(self._canvas, "_tool", None)
        tool_name = active_tool.name if active_tool is not None else "select"
        self._on_canvas_tool_changed(tool_name)

        # Re-emit the new canvas's selection so the format bar reflects
        # whatever (if anything) was selected on this tab.
        self._canvas._emit_selection()

        self._update_window_title()

    def _set_active_state(self, has_doc: bool) -> None:
        """Toggle controls that should only be available with at least
        one open document. Single-tab UX matches today's behavior."""
        self._update_canvas_toolbar_state(has_doc)
        self._update_tool_enabled(has_doc)
        self._save_act.setEnabled(has_doc)
        self._export_current_act.setEnabled(has_doc)
        self._export_selected_act.setEnabled(has_doc)
        self._insert_images_act.setEnabled(has_doc)
        self._balloon_key_act.setEnabled(has_doc)
        self._print_act.setEnabled(has_doc)
        self._fmt_bar.setVisible(has_doc)
        if not has_doc:
            self._set_fmt_bar_enabled(False)
            self.page_list.blockSignals(True)
            self.page_list.clear()
            self.page_list.blockSignals(False)
            self._set_pages_count(0)
            self._update_crumb(None, None)
            self._set_status_page(0, 0)
            self._set_status_tool("—")

    def _update_window_title(self) -> None:
        tab = self._active_tab()
        if tab is None:
            self.setWindowTitle(f"Cove PDF Editor v{__version__}")
            return
        marker = " ●" if tab.doc.dirty else ""
        self.setWindowTitle(
            f"Cove PDF Editor v{__version__} — {tab.doc.source.name}{marker}",
        )

    def _on_tab_close_requested(self, idx: int) -> None:
        if not (0 <= idx < len(self._tabs)):
            return
        tab = self._tabs[idx]
        if not self._confirm_discard_changes(tab):
            return
        self._close_tab(tab)

    # ---- recent documents ------------------------------------------

    @staticmethod
    def _settings() -> QSettings:
        # Org/app names match what __main__.py installs on the
        # QApplication, so QSettings() defaults would also work — but
        # being explicit keeps the store readable independent of init
        # order.
        return QSettings("Cove", "PdfEditor")

    def _recent_files(self) -> list[Path]:
        raw = self._settings().value(_RECENT_KEY, [])
        if isinstance(raw, str):
            # Single-value writes round-trip as a bare string on some
            # Qt platforms.
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        out: list[Path] = []
        seen: set[str] = set()
        for s in raw:
            text = str(s).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(Path(text))
            if len(out) >= _RECENT_CAP:
                break
        return out

    def _write_recent(self, items: list[Path]) -> None:
        # QSettings stores the list as bare strings — never anything
        # beyond the path. Capped at _RECENT_CAP entries.
        self._settings().setValue(
            _RECENT_KEY, [str(p) for p in items[:_RECENT_CAP]],
        )

    def _remember_recent(self, path: Path) -> None:
        """Push ``path`` to the top of the MRU. Dedup by string match;
        cap at _RECENT_CAP."""
        if path is None:
            return
        try:
            resolved = Path(path).expanduser()
        except (OSError, RuntimeError):
            resolved = Path(path)
        items = [p for p in self._recent_files() if str(p) != str(resolved)]
        items.insert(0, resolved)
        self._write_recent(items)
        self._refresh_recent_menu()
        self._refresh_drop_card_recents()

    def _forget_recent(self, path: Path) -> None:
        items = [p for p in self._recent_files() if str(p) != str(path)]
        self._write_recent(items)
        self._refresh_recent_menu()
        self._refresh_drop_card_recents()

    def _open_recent(self, path: Path) -> None:
        """Click handler for any Recent entry. Missing files are
        non-fatal: warn in the status bar and drop the entry from the
        list."""
        if not path.exists():
            self._status.showMessage(
                f"File no longer exists: {path.name}", 6000,
            )
            self._forget_recent(path)
            return
        self._load(path)

    def _clear_recent(self) -> None:
        self._settings().remove(_RECENT_KEY)
        self._refresh_recent_menu()
        self._refresh_drop_card_recents()

    def _refresh_drop_card_recents(self) -> None:
        """Render the visible chunk of recent files on the drop card.
        Hidden when there are no recents — keeps the first-launch view
        identical to the pre-recent UI."""
        layout = getattr(self, "_recent_btn_layout", None)
        section = getattr(self, "_recent_card_section", None)
        if layout is None or section is None:
            return
        # Drop any previously rendered buttons.
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        files = self._recent_files()[:_RECENT_CARD_VISIBLE]
        if not files:
            section.setVisible(False)
            return
        section.setVisible(True)
        for p in files:
            btn = QPushButton(p.name)
            btn.setObjectName("RecentBtn")
            btn.setToolTip(str(p))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFlat(True)
            btn.clicked.connect(lambda _=False, path=p: self._open_recent(path))
            layout.addWidget(btn)

    def _refresh_recent_menu(self) -> None:
        """Repopulate the File → Open Recent submenu from QSettings."""
        menu = getattr(self, "_recent_menu", None)
        if menu is None:
            return
        menu.clear()
        files = self._recent_files()
        if not files:
            empty_act = QAction("(no recent files)", self)
            empty_act.setEnabled(False)
            menu.addAction(empty_act)
        else:
            for p in files:
                act = QAction(p.name, self)
                act.setToolTip(str(p))
                act.triggered.connect(lambda _=False, path=p: self._open_recent(path))
                menu.addAction(act)
            menu.addSeparator()
            clear_act = QAction("Clear Recent", self)
            clear_act.triggered.connect(self._clear_recent)
            menu.addAction(clear_act)

    def _close_tab(self, tab: _Tab) -> None:
        try:
            idx = self._tabs.index(tab)
        except ValueError:
            return
        # Pop our list FIRST so the currentChanged signal that
        # ``removeTab`` may emit (when closing the active tab) sees the
        # post-close list and points _doc / _canvas at the right
        # neighbor. The QTabWidget index always tracks self._tabs index.
        self._tabs.pop(idx)
        self._tab_widget.removeTab(idx)
        # Reap the tab-owned tempdir (blank-PDF or post-import working
        # copy). Then drop the canvas widget — Qt removed it from the
        # tab widget; deleteLater frees its scene + resources.
        self._discard_blank_tmp_dir(tab)
        tab.canvas.deleteLater()
        self._sync_canvas_stack()
        # ``removeTab`` may not fire currentChanged when the tab being
        # closed is not the active one — re-trigger the active-tab
        # handler so any title / sidebar drift is corrected.
        self._on_active_tab_changed(self._tab_widget.currentIndex())

    def _on_save(self) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        self._save_tab(tab)

    def _save_tab(self, tab: _Tab) -> None:
        # Capture any in-flight inline edit before serializing — without
        # this, Ctrl+S during typing would drop the typed text on the
        # floor (the EditableTextItem hadn't yet emitted ``committed``,
        # so the dataclass field powering ``Document.edits`` would still
        # carry the pre-edit value, and the subsequent
        # ``reset_for_saved_source`` would tear the editor down).
        tab.canvas.commit_active_editor()
        default = str(
            tab.doc.source.with_name(tab.doc.source.stem + "-edited.pdf"),
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF", default, "PDF (*.pdf);;All files (*)",
        )
        if not path:
            return
        saved_path = Path(path)
        try:
            save(tab.doc, saved_path)
        except (OSError, RuntimeError, ValueError) as exc:
            log.error("Save failed for %s: %s", saved_path, exc)
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        # Rebase the in-memory document onto the saved file. Without
        # this the canvas keeps reading from the original input — which
        # for a "New" doc is a temp file under /tmp that systemd-tmpfiles
        # may reap — and a second Save would re-bake every prior edit on
        # top of the already-baked output.
        prior_source = tab.doc.source
        tab.doc.source = saved_path
        # Drop visual edits — they're now part of the saved bitmap. But
        # keep ``BubbleEdit`` entries around because their ``text``
        # field is in-session metadata for "Append Balloon Key Page"
        # and is NOT written into the saved PDF. Each surviving bubble
        # is marked ``baked=True`` so the next save / canvas refresh
        # doesn't draw its circle again on top of the already-baked
        # graphic.
        # Hard redactions just applied during this save also strip any
        # baked balloon graphic that intersects them (apply_redactions
        # with images=2, graphics=1). Drop those bubbles from the
        # preserved metadata so the Balloon Key page doesn't list a
        # callout that's no longer visible on the page.
        redacts_by_page: dict[int, list] = {}
        for e in tab.doc.edits:
            if isinstance(e, RedactionEdit):
                redacts_by_page.setdefault(e.page, []).append(e.bbox)
        preserved: list = []
        for e in tab.doc.edits:
            if isinstance(e, BubbleEdit):
                page_redacts = redacts_by_page.get(e.page, [])
                if page_redacts and _bubble_redacted_by(e, page_redacts):
                    continue
                e.baked = True
                preserved.append(e)
        tab.doc.edits = preserved
        tab.doc._rebuild_index()
        tab.doc.dirty = False
        # The canvas still holds EditObjectItems and undo / redo
        # snapshots that reference the just-cleared edits. Reset it so
        # the displayed scene matches the now-empty model and a stray
        # Ctrl+Z can't replay edits already baked into ``saved_path``.
        tab.canvas.reset_for_saved_source()
        # If the prior source was inside this tab's blank-PDF tempdir,
        # that dir is now stale and should be reaped — UNLESS the user
        # accepted the default Save path, which lives inside the same
        # tempdir. Deleting the dir in that case would take the just-
        # saved PDF with it. When the user has parked a real file
        # inside the tempdir we release ownership instead, so neither
        # tab close nor app close destroys their save.
        if tab.blank_tmp_dir is not None:
            if tab.blank_tmp_dir in saved_path.parents:
                tab.blank_tmp_dir = None
            elif (
                prior_source != saved_path
                and tab.blank_tmp_dir in prior_source.parents
            ):
                self._discard_blank_tmp_dir(tab)
        self._refresh_tab_label(tab)
        if tab is self._active_tab():
            self._update_crumb(saved_path.name, self._crumb_page.text())
            self._update_window_title()
        self._status.showMessage(f"Saved {saved_path.name}", 8000)
        self._remember_recent(saved_path)

    # ---------------------------------------------------------- print

    def _on_print(self) -> None:
        tab = self._active_tab()
        if tab is None:
            return
        # Bake any pending in-flight inline edit and the in-memory edit
        # list into a temp PDF so the printout matches what's on screen
        # — Save isn't required first.
        tab.canvas.commit_active_editor()
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="cove-print-", suffix=".pdf")
        os.close(tmp_fd)
        tmp = Path(tmp_name)
        try:
            save(tab.doc, tmp)
        except (OSError, RuntimeError, ValueError) as exc:
            log.error("Print pre-render save failed: %s", exc)
            QMessageBox.critical(self, "Print failed", str(exc))
            tmp.unlink(missing_ok=True)
            return

        # Local imports keep startup cost down for users who never print.
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        from PySide6.QtWidgets import QDialog

        printer = QPrinter(QPrinter.HighResolution)
        printer.setDocName(tab.doc.source.stem)
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("Print")
        if dlg.exec() != QDialog.Accepted:
            tmp.unlink(missing_ok=True)
            return

        if printer.printRange() == QPrinter.PageRange:
            first = max(1, printer.fromPage()) - 1
            last = min(tab.doc.page_count, printer.toPage() or tab.doc.page_count) - 1
        else:
            first, last = 0, tab.doc.page_count - 1
        if first > last:
            tmp.unlink(missing_ok=True)
            return

        # "Print to File" with a PDF target: bypass the raster pipeline
        # and copy the (already vector-clean) temp PDF straight to the
        # output. Rasterizing here turned 875 KB sources into ~75 MB
        # printouts because every page got re-encoded as a high-res
        # bitmap.
        if printer.outputFormat() == QPrinter.PdfFormat:
            out_path_str = printer.outputFileName()
            if out_path_str:
                out_path = Path(out_path_str)
                try:
                    if first == 0 and last == tab.doc.page_count - 1:
                        shutil.copyfile(str(tmp), str(out_path))
                    else:
                        sub = pymupdf.open(str(tmp))
                        sub.select(list(range(first, last + 1)))
                        sub.save(str(out_path), garbage=4, deflate=True)
                        sub.close()
                    self._status.showMessage(f"Saved {out_path.name}", 8000)
                except (OSError, RuntimeError, ValueError) as exc:
                    log.error("Print page-range export failed: %s", exc)
                    QMessageBox.critical(self, "Print failed", str(exc))
                finally:
                    tmp.unlink(missing_ok=True)
                return
            # No output filename — fall through to raster path (rare).

        painter = QPainter()
        if not painter.begin(printer):
            QMessageBox.critical(self, "Print failed", "Could not begin printing.")
            tmp.unlink(missing_ok=True)
            return
        try:
            target_rect = printer.pageLayout().paintRectPixels(
                printer.resolution(),
            )
            for i in range(first, last + 1):
                if i > first:
                    printer.newPage()
                info = page_info(tmp, i)
                # Cap render scale at 4× (~288 dpi at 72 dpi base) so
                # 1200 dpi printers don't allocate a 600 MB intermediate
                # bitmap per page. drawImage scales it up to the
                # printer's pixel rect; visual quality at 288 dpi is
                # plenty for paper.
                if info.width > 0:
                    scale = min(4.0, max(2.0, target_rect.width() / info.width))
                else:
                    scale = 2.0
                image = render_page(tmp, i, scale=scale)
                # Letterbox the page bitmap inside the printer's paint
                # rect so a landscape source on portrait paper (or any
                # mismatched aspect ratio in a mixed-size doc) prints
                # centered without horizontal/vertical squash.
                fitted = _fit_centered(target_rect, image.width(), image.height())
                painter.drawImage(fitted, image)
        finally:
            painter.end()
        tmp.unlink(missing_ok=True)
        self._status.showMessage(
            f"Printed {tab.doc.source.name}",
            6000,
        )

    # ----------------------------------------------- insert images as pages

    def _on_insert_images_as_pages(self) -> None:
        if self._doc is None:
            self._status.showMessage("Open or create a PDF first.", 6000)
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Insert Images as Pages", "", _IMAGE_FILTER,
        )
        if not paths:
            return
        self._insert_image_paths_as_pages([Path(p) for p in paths])

    def _insert_image_paths_as_pages(self, paths: list[Path]) -> None:
        """Append one new PDF page per image to the active document.

        The source PDF on disk is not mutated; we write the modified copy
        to a freshly-allocated temp dir, point ``Document.source`` at it,
        and mark the doc dirty so the user must Save (As) to publish.
        Existing edits are preserved because they reference page indices
        on the original pages, which are not reordered or replaced.
        """
        if self._doc is None or self._canvas is None:
            return
        clean = [p for p in paths if p.suffix.lower() in _IMAGE_EXTS]
        if not clean:
            self._status.showMessage("No supported images selected.", 6000)
            return
        if len(clean) > _MAX_IMAGE_IMPORT:
            self._status.showMessage(
                f"Importing first {_MAX_IMAGE_IMPORT} of {len(clean)} images.",
                8000,
            )
            clean = clean[:_MAX_IMAGE_IMPORT]

        # Capture any in-flight inline edit so the user's typed text is
        # not dropped when we re-render the canvas from the new source.
        self._canvas.commit_active_editor()

        new_dir = Path(tempfile.mkdtemp(prefix="cove-imp-"))
        new_path = new_dir / self._doc.source.name
        appended = 0
        skipped: list[str] = []
        try:
            with pymupdf.open(str(self._doc.source)) as src:
                for path in clean:
                    rect_pw_ph = self._image_page_rect(path)
                    if rect_pw_ph is None:
                        skipped.append(path.name)
                        continue
                    pw, ph, rect = rect_pw_ph
                    page = src.new_page(width=pw, height=ph)
                    try:
                        data = path.read_bytes()
                        page.insert_image(rect, stream=data, keep_proportion=False)
                        appended += 1
                    except (OSError, RuntimeError, ValueError) as exc:
                        log.warning("Could not insert image %s: %s", path.name, exc)
                        skipped.append(path.name)
                        # Drop the empty page so we don't ship a blank.
                        src.delete_page(src.page_count - 1)
                if appended == 0:
                    raise RuntimeError("no images could be imported")
                src.save(str(new_path), garbage=4, deflate=True)
        except (OSError, RuntimeError, ValueError) as exc:
            log.error("Image import failed: %s", exc)
            shutil.rmtree(new_dir, ignore_errors=True)
            QMessageBox.critical(self, "Could not insert images", str(exc))
            return

        # Rebase the active tab's source onto the new file. Reap the
        # tab's prior owned tempdir if any — the tab now owns
        # ``new_dir`` until the next rebase, tab close, or app exit.
        tab = self._active_tab()
        if tab is None:
            shutil.rmtree(new_dir, ignore_errors=True)
            return
        self._discard_blank_tmp_dir(tab)
        tab.blank_tmp_dir = new_dir
        old_count = self._doc.page_count
        self._doc.source = new_path
        self._doc.page_count = old_count + appended
        self._doc.dirty = True

        self.page_list.blockSignals(True)
        for i in range(old_count, self._doc.page_count):
            self.page_list.addItem(QListWidgetItem(f"Page {i + 1}"))
        self.page_list.blockSignals(False)
        self._set_pages_count(self._doc.page_count)

        # Re-render from the new source. ``reset_for_saved_source``
        # clears undo / redo — the page-count change can't be cleanly
        # reverted by the snapshot-of-edits machinery, so we draw a
        # line under it the same way Save does.
        self._canvas.reset_for_saved_source()
        self._refresh_tab_label(tab)
        self._update_window_title()

        msg = f"Inserted {appended} image{'s' if appended != 1 else ''} as pages"
        if skipped:
            msg += f" ({len(skipped)} skipped)"
        self._status.showMessage(msg, 8000)

    # ----------------------------------------------- balloon key page

    def _on_append_balloon_key(self) -> None:
        """Append a Balloon Key page (or pages) summarizing every
        numbered balloon's description. Behaves like the image-import
        rebase: writes the modified copy to a fresh temp dir, points
        the doc at it, marks dirty so the user must Save (As) to
        publish.
        """
        if self._doc is None or self._canvas is None:
            return
        bubbles = [e for e in self._doc.edits if isinstance(e, BubbleEdit)]
        if not bubbles:
            self._status.showMessage(
                "No balloons on this document yet.", 6000,
            )
            return

        self._canvas.commit_active_editor()
        new_dir = Path(tempfile.mkdtemp(prefix="cove-key-"))
        new_path = new_dir / self._doc.source.name
        try:
            with pymupdf.open(str(self._doc.source)) as src:
                added = append_balloon_key_pages(src, bubbles)
                src.save(str(new_path), garbage=4, deflate=True)
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(new_dir, ignore_errors=True)
            QMessageBox.critical(self, "Could not append key page", str(exc))
            return
        if added == 0:
            shutil.rmtree(new_dir, ignore_errors=True)
            return

        tab = self._active_tab()
        if tab is None:
            shutil.rmtree(new_dir, ignore_errors=True)
            return
        self._discard_blank_tmp_dir(tab)
        tab.blank_tmp_dir = new_dir
        old_count = self._doc.page_count
        self._doc.source = new_path
        self._doc.page_count = old_count + added
        self._doc.dirty = True

        self.page_list.blockSignals(True)
        for i in range(old_count, self._doc.page_count):
            self.page_list.addItem(QListWidgetItem(f"Page {i + 1}"))
        self.page_list.blockSignals(False)
        self._set_pages_count(self._doc.page_count)

        self._canvas.reset_for_saved_source()
        self._refresh_tab_label(tab)
        self._update_window_title()
        msg = (
            f"Appended Balloon Key (1 page)"
            if added == 1
            else f"Appended Balloon Key ({added} pages)"
        )
        self._status.showMessage(msg, 8000)

    @staticmethod
    def _image_page_rect(
        path: Path,
    ) -> tuple[float, float, "pymupdf.Rect"] | None:
        """Return ``(page_width_pt, page_height_pt, image_rect_on_page)``
        for one image, or ``None`` if the file is not a readable image.

        Sizing rule (per acceptance criteria): 1 px = 1 pt at 72 dpi. If
        the image fits within Letter at 72 dpi, center it on a Letter
        page; otherwise size the page to the image and fill it edge to
        edge.
        """
        img = QImage(str(path))
        if img.isNull():
            return None
        iw, ih = float(img.width()), float(img.height())
        if iw <= 0 or ih <= 0:
            return None
        default_w, default_h = _DEFAULT_PAGE_PT
        if iw <= default_w and ih <= default_h:
            # Smaller-than-default image: keep it at 1:1 (1 px = 1 pt at
            # 72 dpi) and center it on a Letter page.
            pw, ph = default_w, default_h
            rw, rh = iw, ih
        else:
            # Larger-than-default: size the page to the image at 72 dpi
            # and fill the page edge to edge with aspect preserved.
            pw, ph = iw, ih
            rw, rh = iw, ih
        x0 = (pw - rw) / 2
        y0 = (ph - rh) / 2
        return pw, ph, pymupdf.Rect(x0, y0, x0 + rw, y0 + rh)

    # ------------------------------------------------------- export ops

    @staticmethod
    def _parse_page_range(text: str, page_count: int) -> list[int]:
        pages: list[int] = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-", 1)
                lo_i, hi_i = int(lo), int(hi)
                if lo_i < 1 or hi_i < lo_i or hi_i > page_count:
                    raise ValueError(f"Invalid range: {part}")
                pages.extend(range(lo_i - 1, hi_i))
            else:
                p = int(part)
                if p < 1 or p > page_count:
                    raise ValueError(f"Page {p} out of range (1–{page_count})")
                pages.append(p - 1)
        if not pages:
            raise ValueError("No pages specified")
        seen: set[int] = set()
        result: list[int] = []
        for p in pages:
            if p not in seen:
                seen.add(p)
                result.append(p)
        return result

    def _on_export_current(self) -> None:
        if self._doc is None or self._canvas is None:
            return
        idx = self._canvas.page_index()
        default = str(
            self._doc.source.with_name(
                f"{self._doc.source.stem}-page{idx + 1}.pdf"
            )
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Current Page", default, "PDF (*.pdf);;All files (*)",
        )
        if not path:
            return
        try:
            export_pages(self._doc, [idx], Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._status.showMessage(f"Exported page {idx + 1} → {Path(path).name}", 8000)

    def _on_export_selected(self) -> None:
        if self._doc is None:
            return
        n = self._doc.page_count
        text, ok = QInputDialog.getText(
            self,
            "Export Selected Pages",
            f"Page range (1–{n}), e.g. 1-3,5,8-10:",
        )
        if not ok or not text.strip():
            return
        try:
            pages = self._parse_page_range(text, n)
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Invalid page range", str(exc))
            return
        default = str(
            self._doc.source.with_name(
                f"{self._doc.source.stem}-pages.pdf"
            )
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Selected Pages", default, "PDF (*.pdf);;All files (*)",
        )
        if not path:
            return
        try:
            export_pages(self._doc, pages, Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        labels = text.strip()
        self._status.showMessage(f"Exported pages {labels} → {Path(path).name}", 8000)

    # --------------------------------------------------------- tool ops

    _TOOL_PROMPTS = {
        "select":    "Click to select. Drag to move. Use handles to resize. Delete to remove.",
        "edit_text": "Double-click any searchable text on the page to replace it.",
        "freetext":  "Drag a rectangle, then type to add a text box.",
        "text_plus": "Click anywhere to drop a small text entry. Click again for the next one.",
        "image":     "Drag a rectangle to place the image.",
        "signature": "Drag a rectangle to place your signature.",
        "bubble":    "Drag from a feature to drop a numbered balloon. Just-click drops one with no leader.",
        "redact":    "Drag a rectangle to redact. On save, content inside the box is permanently removed.",
    }

    def _select_tool(self, key: str, factory) -> None:  # noqa: ANN001
        if self._canvas is None:
            return
        tool = factory()
        # Tools may ask the user something before they become active
        # (Add Image / Signature pick the image file). Any tool with a
        # prime() method gets that path; declining the prompt cancels
        # tool activation and re-checks whatever was active before.
        prime = getattr(tool, "prime", None)
        if prime is not None and not prime(self._canvas):
            btn = self._tool_buttons.get(key)
            if btn is not None:
                btn.setChecked(False)
            return
        self._canvas.set_tool(tool)
        prompt = self._TOOL_PROMPTS.get(key, "")
        if prompt:
            self._status.showMessage(prompt, 6000)

    def _on_page_changed(self, row: int) -> None:
        if self._canvas is not None and row >= 0:
            self._canvas.set_page(row)
        if self._doc is not None and row >= 0:
            self._set_status_page(row + 1, self._doc.page_count)
            self._update_crumb(self._doc.source.name, f"page {row + 1}")

    def _on_canvas_page_changed(self, idx: int) -> None:
        if self._doc is None or idx < 0 or idx >= self._doc.page_count:
            return
        self.page_list.blockSignals(True)
        self.page_list.setCurrentRow(idx)
        self.page_list.blockSignals(False)
        self._set_status_page(idx + 1, self._doc.page_count)
        self._update_crumb(self._doc.source.name, f"page {idx + 1}")

    # --------------------------------------------------------- helpers

    def _update_tool_enabled(self, enabled: bool) -> None:
        for btn in self._tool_buttons.values():
            btn.setEnabled(enabled)

    # --------------------------------------------------------- formatting

    def _build_format_bar(self) -> QToolBar:
        bar = QToolBar("Formatting")
        bar.setMovable(False)
        bar.setIconSize(bar.iconSize())  # let Qt pick a sensible default
        # Visual styling lives in theme.GLOBAL_QSS (QToolBar +
        # QToolBar QToolButton selectors). No local QSS so the bar
        # stays in step with the rest of the chrome automatically.

        bar.addWidget(QLabel("FORMAT"))

        # Curated font dropdown. Editable so a stored fontname that isn't
        # in the curated list (e.g. an exotic font from a re-edited
        # FreeText) still displays correctly; NoInsert prevents the
        # combo from polluting itself with whatever the user types.
        self._family_combo = QComboBox()
        self._family_combo.setEditable(True)
        self._family_combo.setInsertPolicy(QComboBox.NoInsert)
        self._family_combo.setMaximumWidth(220)
        self._family_combo.setToolTip("Font family")
        self._populate_family_combo()
        self._family_combo.currentTextChanged.connect(self._on_family_changed)
        bar.addWidget(self._family_combo)

        self._size_spin = QSpinBox()
        self._size_spin.setRange(6, 200)
        self._size_spin.setValue(12)
        self._size_spin.setSuffix(" pt")
        self._size_spin.setToolTip("Font size")
        self._size_spin.valueChanged.connect(self._on_size_changed)
        bar.addWidget(self._size_spin)

        bar.addSeparator()

        # B/I/U buttons display their actual style on the label so the
        # affordance is obvious at a glance.
        self._bold_btn = QToolButton(text="B")
        self._bold_btn.setCheckable(True)
        self._bold_btn.setToolTip("Bold (Ctrl+B)")
        f = QFont(); f.setBold(True); f.setPointSize(f.pointSize() + 1)
        self._bold_btn.setFont(f)
        self._bold_btn.toggled.connect(self._on_bold_toggled)
        bar.addWidget(self._bold_btn)

        self._italic_btn = QToolButton(text="I")
        self._italic_btn.setCheckable(True)
        self._italic_btn.setToolTip("Italic (Ctrl+I)")
        f = QFont(); f.setItalic(True); f.setPointSize(f.pointSize() + 1)
        self._italic_btn.setFont(f)
        self._italic_btn.toggled.connect(self._on_italic_toggled)
        bar.addWidget(self._italic_btn)

        self._underline_btn = QToolButton(text="U")
        self._underline_btn.setCheckable(True)
        self._underline_btn.setToolTip("Underline (Ctrl+U)")
        f = QFont(); f.setUnderline(True); f.setPointSize(f.pointSize() + 1)
        self._underline_btn.setFont(f)
        self._underline_btn.toggled.connect(self._on_underline_toggled)
        bar.addWidget(self._underline_btn)

        bar.addSeparator()

        self._color_btn = QToolButton(text="A")
        self._color_btn.setToolTip("Text color")
        # The 'A' shows the chosen color; populated by _refresh_color_swatch.
        f = QFont(); f.setBold(True); f.setPointSize(f.pointSize() + 1)
        self._color_btn.setFont(f)
        self._color_btn.clicked.connect(self._on_color_clicked)
        bar.addWidget(self._color_btn)

        bar.addSeparator()

        # ASCII labels for alignment buttons so they always render — Linux
        # default font stacks frequently miss the U+2BC7/U+2BCC/U+2BC8
        # alignment-arrow glyphs.
        self._align_group = QButtonGroup(self)
        self._align_group.setExclusive(True)
        for label, value, tip in (
            ("Left",   "left",   "Align left"),
            ("Center", "center", "Align center"),
            ("Right",  "right",  "Align right"),
        ):
            btn = QToolButton(text=label)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setProperty("align", value)
            btn.clicked.connect(lambda _, v=value: self._on_align_changed(v))
            self._align_group.addButton(btn)
            bar.addWidget(btn)

        return bar

    def _on_canvas_tool_changed(self, name: str) -> None:
        """Keep the sidebar's checked tool button in sync with the canvas
        when the active tool changes from outside the sidebar — e.g. a
        placement tool calling ``canvas.return_to_select()`` after
        committing an edit."""
        self._set_status_tool(name)
        btn = self._tool_buttons.get(name)
        if btn is None or btn.isChecked():
            return
        # The QButtonGroup is exclusive, so checking this button
        # unchecks the previously active one automatically.
        btn.blockSignals(True)
        btn.setChecked(True)
        btn.blockSignals(False)
        self._sync_tool_row_active(btn, True)

    def _on_canvas_selection(self, edit) -> None:  # noqa: ANN001
        is_text = isinstance(edit, FreeText)
        self._selected_edit = edit if is_text else None
        self._set_fmt_bar_enabled(is_text)
        if is_text:
            self._populate_fmt_bar(edit)
        # Selection-change is the cheapest, most-frequent place to keep
        # the dirty marker in sync with the doc — selection happens with
        # essentially every user gesture that could mutate state.
        tab = self._active_tab()
        if tab is not None:
            self._refresh_tab_label(tab)
        self._update_window_title()

    def _set_fmt_bar_enabled(self, on: bool) -> None:
        for w in (
            self._family_combo, self._size_spin,
            self._bold_btn, self._italic_btn, self._underline_btn,
            self._color_btn,
            *self._align_group.buttons(),
        ):
            w.setEnabled(on)
        # Re-sync the color swatch so the 'A' picks up the right palette
        # for the new enabled/disabled state.
        if on and self._selected_edit is not None:
            self._refresh_color_swatch()
        else:
            self._color_btn.setStyleSheet("")  # inherit toolbar QSS (dimmed)

    def _populate_fmt_bar(self, edit: FreeText) -> None:
        self._family_combo.blockSignals(True)
        # If the stored fontname isn't in our curated dropdown, just
        # display the literal name in the line edit — don't append to
        # the dropdown (NoInsert) and don't substitute a different
        # family.
        idx = self._family_combo.findText(edit.fontname)
        if idx >= 0:
            self._family_combo.setCurrentIndex(idx)
        else:
            self._family_combo.setEditText(edit.fontname)
        self._family_combo.blockSignals(False)
        self._size_spin.blockSignals(True)
        self._size_spin.setValue(int(round(edit.fontsize)))
        self._size_spin.blockSignals(False)
        self._bold_btn.blockSignals(True)
        self._bold_btn.setChecked(edit.bold)
        self._bold_btn.blockSignals(False)
        self._italic_btn.blockSignals(True)
        self._italic_btn.setChecked(edit.italic)
        self._italic_btn.blockSignals(False)
        self._underline_btn.blockSignals(True)
        self._underline_btn.setChecked(edit.underline)
        self._underline_btn.blockSignals(False)
        for btn in self._align_group.buttons():
            btn.blockSignals(True)
            btn.setChecked(btn.property("align") == edit.align)
            btn.blockSignals(False)
        self._refresh_color_swatch()

    def _refresh_color_swatch(self) -> None:
        """Show the chosen text color on the 'A' label. Falls back to the
        inherited toolbar style when nothing is selected so a disabled
        Color button doesn't shout a stale color."""
        if self._selected_edit is None or not self._color_btn.isEnabled():
            self._color_btn.setStyleSheet("")  # inherit toolbar QSS
            return
        r, g, b = self._selected_edit.color
        # Per-button QSS — keeps the toolbar geometry from theme.py
        # while overriding just the foreground color of the 'A'.
        self._color_btn.setStyleSheet(theme.color_swatch_qss(r, g, b))

    def _apply_change(self) -> None:
        if self._selected_edit is None or self._canvas is None:
            return
        self._canvas.refresh_item_for(self._selected_edit)
        self._canvas.document().dirty = True

    def _populate_family_combo(self) -> None:
        installed = set(QFontDatabase.families())
        # Curated tier: list of (friendly_display_name, installed_family).
        # The dropdown shows the friendly name; userData holds the
        # actual installed family so Qt renders real glyphs without
        # bundling proprietary fonts.
        curated = _resolve_curated(installed)
        curated_installed = {fam for _, fam in curated}
        others = sorted(
            f for f in installed
            if f not in curated_installed and _is_text_font(f)
        )
        self._family_combo.blockSignals(True)
        self._family_combo.clear()
        for friendly, installed_family in curated:
            self._family_combo.addItem(friendly, installed_family)
        if curated and others:
            self._family_combo.insertSeparator(len(curated))
        for f in others:
            self._family_combo.addItem(f, f)
        self._family_combo.blockSignals(False)

    def _begin_format_edit(self) -> None:
        """Snapshot the document state before a single formatting change
        so each toolbar tweak is one Ctrl+Z step."""
        if self._canvas is not None:
            self._canvas.take_snapshot()

    def _on_family_changed(self, family: str) -> None:
        if self._selected_edit is None or not family:
            return
        # Prefer the installed family stored on userData over the
        # friendly display name so Qt renders the actual glyphs and
        # save resolves correctly.
        idx = self._family_combo.currentIndex()
        installed_family = self._family_combo.itemData(idx) if idx >= 0 else None
        chosen = installed_family or family
        if self._selected_edit.fontname == chosen:
            return
        self._begin_format_edit()
        self._selected_edit.fontname = chosen
        self._apply_change()

    def _on_size_changed(self, size: int) -> None:
        if self._selected_edit is None:
            return
        if self._selected_edit.fontsize == float(size):
            return
        self._begin_format_edit()
        self._selected_edit.fontsize = float(size)
        self._apply_change()

    def _on_bold_toggled(self, on: bool) -> None:
        if self._selected_edit is None or self._selected_edit.bold == on:
            return
        self._begin_format_edit()
        self._selected_edit.bold = on
        self._apply_change()

    def _on_italic_toggled(self, on: bool) -> None:
        if self._selected_edit is None or self._selected_edit.italic == on:
            return
        self._begin_format_edit()
        self._selected_edit.italic = on
        self._apply_change()

    def _on_underline_toggled(self, on: bool) -> None:
        if self._selected_edit is None or self._selected_edit.underline == on:
            return
        self._begin_format_edit()
        self._selected_edit.underline = on
        self._apply_change()

    def _on_color_clicked(self) -> None:
        if self._selected_edit is None:
            return
        initial = QColor(*self._selected_edit.color)
        color = QColorDialog.getColor(initial, self, "Text color")
        if not color.isValid():
            return
        new_color = (color.red(), color.green(), color.blue())
        if new_color == self._selected_edit.color:
            return
        self._begin_format_edit()
        self._selected_edit.color = new_color
        self._refresh_color_swatch()
        self._apply_change()

    def _on_align_changed(self, value: str) -> None:
        if self._selected_edit is None or self._selected_edit.align == value:
            return
        self._begin_format_edit()
        self._selected_edit.align = value
        self._apply_change()

    # --------------------------------------------------------- DnD

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        # PDF drops on the main window always open in a new tab — they
        # never replace the active document. Non-PDF drops are ignored
        # here; image drops onto the page list are handled by _PageList.
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and Path(p).suffix.lower() == ".pdf":
                self._load(Path(p))
                event.acceptProposedAction()
                return
        event.ignore()

    # ----------------------------------------------- frameless resize

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._frameless_resizer.try_press(event):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._frameless_resizer.try_move(event):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._frameless_resizer.try_release(event):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self._frameless_resizer.clear_hover()
        super().leaveEvent(event)

    def closeEvent(self, event) -> None:  # noqa: ANN001
        # Confirm each dirty tab in turn so the user can save / discard
        # / cancel without losing work. Cancel from any tab aborts the
        # close.
        for tab in list(self._tabs):
            if not self._confirm_discard_changes(tab):
                event.ignore()
                return
        # Reap any tab-owned blank-PDF tempdirs on the way out.
        for tab in self._tabs:
            self._discard_blank_tmp_dir(tab)
        super().closeEvent(event)


class _StatusShim:
    """Tiny showMessage(text, ms) shim that targets a QLabel so existing
    QStatusBar-style call sites keep working with the custom status bar."""

    def __init__(self, parent) -> None:  # noqa: ANN001
        self._target: QLabel | None = None
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._clear)

    def set_target(self, label: QLabel) -> None:
        self._target = label

    def showMessage(self, text: str, timeout_ms: int = 0) -> None:  # noqa: N802
        if self._target is None:
            return
        self._target.setText(text or "")
        self._timer.stop()
        if timeout_ms > 0:
            self._timer.start(timeout_ms)

    def clearMessage(self) -> None:  # noqa: N802
        self._clear()

    def _clear(self) -> None:
        if self._target is not None:
            self._target.setText("")
