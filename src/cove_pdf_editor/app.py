from __future__ import annotations

from pathlib import Path

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
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__, updater
from .canvas import PageCanvas
from .document import Document, FreeText
from .overlay import save
from .tools import (
    AddImageTool,
    EditTextTool,
    FreeTextTool,
    SelectTool,
    TextPlusTool,
)


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICON_PATH = ASSETS_DIR / "cove_icon.png"


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


_TOOL_BTN_STYLE = """
QPushButton {
    text-align: left;
    padding: 8px 12px;
    border: none;
    border-radius: 4px;
    color: #cfd0d4;
    background: transparent;
    font-size: 12px;
}
QPushButton:hover { background: #1b2330; }
QPushButton:checked { background: #1f3a5c; color: #ffffff; font-weight: 600; }
QPushButton:disabled { color: #5a616f; }
"""


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Cove PDF Editor v{__version__}")
        self.resize(1300, 820)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self._doc: Document | None = None
        self._canvas: PageCanvas | None = None
        self._tool_buttons: dict[str, QPushButton] = {}
        self._build_ui()
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
        self._status = QStatusBar()
        self.setStatusBar(self._status)

        # Top toolbar with open/save
        bar = QToolBar()
        bar.setMovable(False)
        bar.setStyleSheet("QToolBar { background:#14181f; border-bottom:1px solid #2a2f3a; padding:4px; }")
        self.addToolBar(bar)
        open_act = QAction("Open PDF…", self)
        open_act.setShortcut(QKeySequence.Open)
        open_act.setToolTip("Open a PDF (Ctrl+O)")
        open_act.triggered.connect(self._on_open)
        bar.addAction(open_act)
        self._save_act = QAction("Save As…", self)
        self._save_act.setShortcut(QKeySequence.Save)
        self._save_act.setToolTip("Save the edited PDF (Ctrl+S)")
        self._save_act.setEnabled(False)
        self._save_act.triggered.connect(self._on_save)
        bar.addAction(self._save_act)

        # Formatting toolbar (second row, hidden until a text object is selected).
        self.addToolBarBreak()
        self._fmt_bar = self._build_format_bar()
        self.addToolBar(self._fmt_bar)
        self._fmt_bar.setVisible(False)
        self._selected_edit: FreeText | None = None

        # Main: left sidebar (tools + pages) | canvas
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QFrame()
        side.setFixedWidth(200)
        side.setStyleSheet("QFrame { background:#14181f; border-right:1px solid #2a2f3a; }")
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 10, 8, 10)
        side_layout.setSpacing(4)

        title = QLabel("Tools")
        title.setStyleSheet("color:#cfd0d4; font-weight:600; padding:6px 8px;")
        side_layout.addWidget(title)

        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)

        def add(label: str, key: str, factory=None, handler=None, tooltip: str = ""):  # noqa: ANN001
            btn = QPushButton(label)
            btn.setCheckable(factory is not None)
            btn.setStyleSheet(_TOOL_BTN_STYLE)
            btn.setCursor(Qt.PointingHandCursor)
            if tooltip:
                btn.setToolTip(tooltip)
            if factory is not None:
                self._tool_group.addButton(btn)
                btn.clicked.connect(lambda: self._select_tool(key, factory))
            if handler is not None:
                btn.clicked.connect(handler)
            self._tool_buttons[key] = btn
            side_layout.addWidget(btn)
            return btn

        add("👆  Select",     "select",    SelectTool,
            tooltip="Select objects to move, resize, or delete")
        add("📝  Edit Text",  "edit_text", EditTextTool,
            tooltip="Double-click searchable PDF text to replace it")
        add("🅰  Add Text",   "freetext",  FreeTextTool,
            tooltip="Drag a rectangle to add a new text box")
        add("➕  Text Plus",  "text_plus", TextPlusTool,
            tooltip="Click to drop quick text entries — good for filling forms")
        add("🖼  Add Image",  "image",     AddImageTool,
            tooltip="Pick a PNG or JPG and drag a rectangle to place it")

        side_layout.addSpacing(12)
        side_layout.addWidget(_section_header("Pages"))
        self.page_list = QListWidget()
        self.page_list.setStyleSheet(
            "QListWidget { background:#0e1116; color:#cfd0d4; "
            "border:1px solid #2a2f3a; border-radius:4px; }"
            "QListWidget::item:selected { background:#1f3a5c; }"
        )
        self.page_list.currentRowChanged.connect(self._on_page_changed)
        self.page_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        side_layout.addWidget(self.page_list, stretch=1)

        version = QLabel("v1.0.0 · offline")
        version.setStyleSheet("color:#5a616f; font-size:10px; padding:4px 8px;")
        side_layout.addWidget(version)

        root.addWidget(side)

        # Canvas container
        self._canvas_stack = QStackedWidget()
        self._canvas_stack.setStyleSheet("QStackedWidget { background:#0a0c11; }")
        self._placeholder = QLabel(
            "Drop a PDF here, or press Ctrl+O to open one.\n"
            "Then pick a tool on the left and click / drag on the page."
        )
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color:#7a8294; font-size:14px;")
        self._canvas_stack.addWidget(self._placeholder)
        root.addWidget(self._canvas_stack, stretch=1)

        self._update_tool_enabled(False)

    # --------------------------------------------------------- file ops

    def _install_global_shortcuts(self) -> None:
        """Install Undo/Redo as application-wide ``QShortcut``s. Earlier
        we used ``QAction`` with ``Qt.ApplicationShortcut``, but on some
        Linux window managers the QAction shortcut path didn't fire
        ``triggered`` reliably — particularly for Ctrl+Y. ``QShortcut``
        binds at the Qt shortcut-event layer directly and fires
        regardless of which child widget has focus."""
        from PySide6.QtGui import QShortcut

        def add(seq: str, handler) -> "QShortcut":  # noqa: ANN001
            sh = QShortcut(QKeySequence(seq), self)
            sh.setContext(Qt.ApplicationShortcut)
            sh.activated.connect(handler)
            return sh

        self._sc_undo = add("Ctrl+Z", self._do_undo)
        # Bind both common Redo bindings explicitly.
        self._sc_redo_y = add("Ctrl+Y", self._do_redo)
        self._sc_redo_shift_z = add("Ctrl+Shift+Z", self._do_redo)

    def _do_undo(self) -> None:
        if self._canvas is not None:
            self._canvas.undo()

    def _do_redo(self) -> None:
        if self._canvas is not None:
            self._canvas.redo()

    def _on_open(self) -> None:
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
        self._status.showMessage(f"{path.name} • {n} page(s)", 6000)
        self._update_tool_enabled(True)
        self._save_act.setEnabled(True)
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
        default = str(self._doc.source.with_name(self._doc.source.stem + "-edited.pdf"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF", default, "PDF (*.pdf);;All files (*)",
        )
        if not path:
            return
        try:
            save(self._doc, Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._doc.dirty = False
        self._status.showMessage(f"Saved {Path(path).name}", 8000)

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

    # --------------------------------------------------------- helpers

    def _update_tool_enabled(self, enabled: bool) -> None:
        for btn in self._tool_buttons.values():
            btn.setEnabled(enabled)

    # --------------------------------------------------------- formatting

    def _build_format_bar(self) -> QToolBar:
        bar = QToolBar("Formatting")
        bar.setMovable(False)
        bar.setIconSize(bar.iconSize())  # let Qt pick a sensible default
        # Lighter background than the rest of the chrome so the bar visibly
        # reads as a separate row, plus stronger disabled vs enabled
        # contrast so the controls don't fade into the dark theme.
        bar.setStyleSheet(
            "QToolBar { background:#1d2330; border-top:1px solid #2a3142; "
            "border-bottom:1px solid #2a3142; padding:6px 8px; }"
            "QToolBar QLabel { color:#9aa3b8; padding:0 10px 0 2px; "
            "font-size:11px; font-weight:700; }"
            "QToolBar QToolButton { color:#e2e6ee; padding:6px 10px; "
            "margin:0 1px; border:1px solid transparent; border-radius:3px; "
            "font-size:13px; min-width:24px; }"
            "QToolBar QToolButton:hover:!disabled { background:#2a3a55; "
            "border-color:#3a4a65; }"
            "QToolBar QToolButton:checked:!disabled { background:#3a5a8c; "
            "color:#ffffff; border-color:#4a6aa0; }"
            "QToolBar QToolButton:disabled { color:#5a6275; }"
            "QToolBar QFontComboBox, QToolBar QSpinBox { background:#0e1116; "
            "color:#e2e6ee; border:1px solid #2a3142; border-radius:3px; "
            "padding:3px 4px; min-height:22px; }"
            "QToolBar QFontComboBox:disabled, QToolBar QSpinBox:disabled "
            "{ color:#5a6275; background:#161a22; }"
        )

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
        btn = self._tool_buttons.get(name)
        if btn is None or btn.isChecked():
            return
        # The QButtonGroup is exclusive, so checking this button
        # unchecks the previously active one automatically.
        btn.blockSignals(True)
        btn.setChecked(True)
        btn.blockSignals(False)

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
        # Per-button QSS — keeps the toolbar's spacing/border/min-width
        # while overriding just the foreground color of the 'A'.
        self._color_btn.setStyleSheet(
            f"QToolButton {{ color: rgb({r},{g},{b}); padding:6px 10px; "
            f"margin:0 1px; border:1px solid transparent; border-radius:3px; "
            f"min-width:24px; }}"
            f"QToolButton:hover {{ background:#2a3a55; border-color:#3a4a65; }}"
        )

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
                self._load(Path(p))
                event.acceptProposedAction()
                return


def _section_header(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("color:#7a8294; font-size:10px; font-weight:600; padding:6px 8px 2px 8px;")
    return label
