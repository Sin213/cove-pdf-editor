from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pymupdf
import pypdfium2 as pdfium
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontDatabase,
    QIcon,
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
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__, theme, updater
from .canvas import PageCanvas
from .chrome import CoveTitleBar, FramelessResizer
from .document import Document, FreeText
from .overlay import export_pages, save
from .tools import (
    AddImageTool,
    EditTextTool,
    FreeTextTool,
    SelectTool,
    TextPlusTool,
)


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICON_PATH = ASSETS_DIR / "cove_icon.png"

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
        self._doc: Document | None = None
        self._canvas: PageCanvas | None = None
        self._tool_buttons: dict[str, QPushButton] = {}
        # Per-session temp dir backing a "New" blank PDF. We own this
        # path and reap it on next New / on a Save As that rebases the
        # document off the temp file / on app close. Without this the
        # /tmp/cove-* dirs accumulate forever and (when /tmp is reaped
        # externally) the canvas's source-of-truth path disappears
        # mid-session.
        self._blank_tmp_dir: Path | None = None
        self._build_ui()
        self._build_menu()
        self._install_global_shortcuts()
        self.setAcceptDrops(True)
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
        self.page_list = QListWidget()
        self.page_list.setObjectName("PageList")
        self.page_list.currentRowChanged.connect(self._on_page_changed)
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

        self._canvas_stack = QStackedWidget()
        self._canvas_stack.setObjectName("CanvasStack")
        self._drop_wrap = self._build_drop_card()
        self._canvas_stack.addWidget(self._drop_wrap)
        lay.addWidget(self._canvas_stack, stretch=1)
        return wrap

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

        card_lay.addWidget(glyph, alignment=Qt.AlignCenter)
        card_lay.addWidget(title)
        card_lay.addWidget(body, alignment=Qt.AlignCenter)
        card_lay.addLayout(actions)
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

    def _confirm_discard_changes(self) -> bool:
        """Save / Discard / Cancel prompt before replacing the open document.

        Returns ``True`` when it's safe to load a different document
        (either there were no unsaved changes, the user discarded them,
        or the user picked Save and the save completed). Returns
        ``False`` when the user cancelled or the save did not complete —
        in which case the caller must keep the current document
        untouched.

        Captures any in-flight inline editor first so typed-but-
        unsubmitted text counts as unsaved state. Without this, opening
        a different PDF or drag-dropping one onto the window during an
        active inline edit would silently throw away whatever the user
        was typing — ``Document.dirty`` only flips when the editor
        commits.
        """
        if self._canvas is not None:
            self._canvas.commit_active_editor()
        if self._doc is None or not self._doc.dirty:
            return True
        reply = QMessageBox.question(
            self,
            "Unsaved Changes",
            "The current document has unsaved changes.",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply == QMessageBox.Cancel:
            return False
        if reply == QMessageBox.Save:
            self._on_save()
            if self._doc is not None and self._doc.dirty:
                return False
        return True

    def _on_new(self) -> None:
        if not self._confirm_discard_changes():
            return
        self._create_and_load_blank()

    def _create_and_load_blank(self) -> None:
        # Reap the previous blank temp dir before allocating a new one
        # so File → New repeatedly doesn't leave a trail in /tmp.
        self._discard_blank_tmp_dir()
        tmp_dir = Path(tempfile.mkdtemp(prefix="cove-"))
        tmp = tmp_dir / "Untitled.pdf"
        doc = pymupdf.open()
        doc.new_page(width=612, height=792)
        doc.save(str(tmp))
        doc.close()
        self._blank_tmp_dir = tmp_dir
        self._load(tmp)

    def _discard_blank_tmp_dir(self) -> None:
        """Remove the per-session blank-PDF tempdir if we own one."""
        if self._blank_tmp_dir is None:
            return
        shutil.rmtree(self._blank_tmp_dir, ignore_errors=True)
        self._blank_tmp_dir = None

    def _on_open(self) -> None:
        if not self._confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", "", "PDF files (*.pdf);;All files (*)",
        )
        if path:
            self._load(Path(path))

    def _load(self, path: Path) -> None:
        try:
            with pdfium.PdfDocument(str(path)) as doc:
                n = len(doc)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Could not open PDF", str(exc))
            return
        self._doc = Document(source=path, page_count=n)
        if self._canvas is not None:
            self._canvas_stack.removeWidget(self._canvas)
            self._canvas.deleteLater()
        self._canvas = PageCanvas(self._doc)
        self._canvas.selectionChanged.connect(self._on_canvas_selection)
        self._canvas.statusMessage.connect(
            lambda msg: self._status.showMessage(msg, 5000),
        )
        self._canvas.toolChanged.connect(self._on_canvas_tool_changed)
        self._canvas_stack.addWidget(self._canvas)
        self._canvas_stack.setCurrentWidget(self._canvas)
        self.page_list.blockSignals(True)
        self.page_list.clear()
        for i in range(n):
            self.page_list.addItem(QListWidgetItem(f"Page {i + 1}"))
        self.page_list.setCurrentRow(0)
        self.page_list.blockSignals(False)
        self._set_pages_count(n)
        self._update_canvas_toolbar_state(True)
        self._update_crumb(path.name, "page 1")
        self._set_status_page(1, n)
        self._status.showMessage(f"{path.name} • {n} page(s)", 6000)
        self._update_tool_enabled(True)
        self._save_act.setEnabled(True)
        self._export_current_act.setEnabled(True)
        self._export_selected_act.setEnabled(True)
        # The formatting toolbar is now part of the standard surface — show
        # it for the rest of the session and toggle controls based on what
        # is selected, instead of hiding the whole bar.
        self._fmt_bar.setVisible(True)
        self._set_fmt_bar_enabled(False)
        # Default to Select mode so the user can click objects right away.
        select_btn = self._tool_buttons.get("select")
        if select_btn is not None:
            select_btn.setChecked(True)
        self._canvas.set_tool(SelectTool())

    def _on_save(self) -> None:
        if self._doc is None:
            return
        # Capture any in-flight inline edit before serializing — without
        # this, Ctrl+S during typing would drop the typed text on the
        # floor (the EditableTextItem hadn't yet emitted ``committed``,
        # so the dataclass field powering ``Document.edits`` would still
        # carry the pre-edit value, and the subsequent
        # ``reset_for_saved_source`` would tear the editor down).
        if self._canvas is not None:
            self._canvas.commit_active_editor()
        default = str(self._doc.source.with_name(self._doc.source.stem + "-edited.pdf"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF", default, "PDF (*.pdf);;All files (*)",
        )
        if not path:
            return
        saved_path = Path(path)
        try:
            save(self._doc, saved_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        # Rebase the in-memory document onto the saved file. Without
        # this the canvas keeps reading from the original input — which
        # for a "New" doc is a temp file under /tmp that systemd-tmpfiles
        # may reap — and a second Save would re-bake every prior edit on
        # top of the already-baked output.
        prior_source = self._doc.source
        self._doc.source = saved_path
        self._doc.edits = []
        self._doc.dirty = False
        # The canvas still holds EditObjectItems and undo / redo
        # snapshots that reference the just-cleared edits. Reset it so
        # the displayed scene matches the now-empty model and a stray
        # Ctrl+Z can't replay edits already baked into ``saved_path``.
        if self._canvas is not None:
            self._canvas.reset_for_saved_source()
        # If the prior source was inside our blank-PDF tempdir, that
        # dir is now stale and should be reaped — UNLESS the user
        # accepted the default Save path, which lives inside the same
        # tempdir. Deleting the dir in that case would take the just-
        # saved PDF with it. When the user has parked a real file
        # inside the tempdir we release ownership instead, so neither
        # the next File → New nor closeEvent destroys their save.
        if self._blank_tmp_dir is not None:
            if self._blank_tmp_dir in saved_path.parents:
                self._blank_tmp_dir = None
            elif (
                prior_source != saved_path
                and self._blank_tmp_dir in prior_source.parents
            ):
                self._discard_blank_tmp_dir()
        self._update_crumb(saved_path.name, self._crumb_page.text())
        self._status.showMessage(f"Saved {saved_path.name}", 8000)

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
    }

    def _select_tool(self, key: str, factory) -> None:  # noqa: ANN001
        if self._canvas is None:
            return
        tool = factory()
        # Image tool needs to ask for its image file up-front.
        if key == "image":
            if not tool.prime(self._canvas):
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

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and Path(p).suffix.lower() == ".pdf":
                if not self._confirm_discard_changes():
                    event.ignore()
                    return
                self._load(Path(p))
                event.acceptProposedAction()
                return

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
        # Reap the per-session blank-PDF tempdir on app exit so we
        # don't leave /tmp/cove-* directories behind.
        self._discard_blank_tmp_dir()
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
