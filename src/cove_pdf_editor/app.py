from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
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
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from . import __version__, updater
from .canvas import PageCanvas
from .dialogs import (
    BookmarkDialog,
    FormFillDialog,
    HeaderFooterDialog,
    HyperlinkDialog,
    WatermarkDialog,
    prompt_signature,
)
from .document import Bookmark, Document, Hyperlink, Stamp
from .overlay import save
from .tools import (
    EditTextTool,
    FreeTextTool,
    InkTool,
    MarkupTool,
    NoteTool,
    ShapeTool,
    StampTool,
)


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICON_PATH = ASSETS_DIR / "cove_icon.png"


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
        open_act.triggered.connect(self._on_open)
        bar.addAction(open_act)
        save_act = QAction("Save as…", self)
        save_act.setShortcut(QKeySequence.Save)
        save_act.triggered.connect(lambda: self._on_save(flatten=False))
        bar.addAction(save_act)
        save_flat = QAction("Save flattened…", self)
        save_flat.triggered.connect(lambda: self._on_save(flatten=True))
        bar.addAction(save_flat)

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

        def add(label: str, key: str, factory=None, handler=None):  # noqa: ANN001
            btn = QPushButton(label)
            btn.setCheckable(factory is not None)
            btn.setStyleSheet(_TOOL_BTN_STYLE)
            btn.setCursor(Qt.PointingHandCursor)
            if factory is not None:
                self._tool_group.addButton(btn)
                btn.clicked.connect(lambda: self._select_tool(key, factory))
            if handler is not None:
                btn.clicked.connect(handler)
            self._tool_buttons[key] = btn
            side_layout.addWidget(btn)
            return btn

        add("📝  Edit text", "edit_text", EditTextTool)
        add("🖍  Highlight", "highlight", lambda: MarkupTool("highlight"))
        add("∼  Strikethrough", "strike", lambda: MarkupTool("strike"))
        add("﹍  Underline", "underline", lambda: MarkupTool("underline"))
        add("🗨  Sticky note", "note", NoteTool)
        add("🅰  Text box", "freetext", FreeTextTool)
        add("▭  Rectangle", "rect", lambda: ShapeTool("rect"))
        add("◯  Circle", "circle", lambda: ShapeTool("circle"))
        add("╱  Line", "line", lambda: ShapeTool("line"))
        add("➜  Arrow", "arrow", lambda: ShapeTool("arrow"))
        add("✎  Freehand", "ink", InkTool)
        add("🖼  Image stamp", "stamp", StampTool)
        add("✍  Signature", "signature", handler=self._on_signature)

        side_layout.addSpacing(12)
        side_layout.addWidget(_section_header("Document"))
        add("📋  Fill form", "form", handler=self._on_form_fill)
        add("🧾  Header / footer", "header", handler=self._on_header_footer)
        add("💧  Watermark", "watermark", handler=self._on_watermark)
        add("🔖  Add bookmark", "bookmark", handler=self._on_bookmark)
        add("🔗  Add link", "hyperlink", handler=self._on_hyperlink)

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

    def _on_save(self, *, flatten: bool) -> None:
        if self._doc is None:
            return
        default = str(self._doc.source.with_name(self._doc.source.stem + "-edited.pdf"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF", default, "PDF (*.pdf);;All files (*)",
        )
        if not path:
            return
        try:
            save(self._doc, Path(path), mode="flatten" if flatten else "preserve")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._doc.dirty = False
        self._status.showMessage(
            f"Saved {Path(path).name}  ({'flattened' if flatten else 'editable'})", 8000,
        )

    # --------------------------------------------------------- tool ops

    def _select_tool(self, key: str, factory) -> None:  # noqa: ANN001
        if self._canvas is None:
            return
        tool = factory()
        # Stamp tool needs to ask for its image up-front.
        if key == "stamp":
            if not tool.prime(self._canvas):
                # Un-check its button.
                btn = self._tool_buttons.get(key)
                if btn is not None:
                    btn.setChecked(False)
                return
        self._canvas.set_tool(tool)
        self._status.showMessage(f"Tool: {key}", 3000)

    def _on_page_changed(self, row: int) -> None:
        if self._canvas is not None and row >= 0:
            self._canvas.set_page(row)

    # --------------------------------------------------------- dialogs

    def _on_signature(self) -> None:
        if self._canvas is None:
            return
        sig_path = prompt_signature(self)
        if sig_path is None:
            return
        tool = StampTool()
        tool._image_path = sig_path  # noqa: SLF001
        self._canvas.set_tool(tool)
        self._status.showMessage("Drag a box where you want your signature.", 6000)

    def _on_form_fill(self) -> None:
        if self._canvas is None or self._doc is None:
            return
        dlg = FormFillDialog(self._doc.source, self)
        if dlg.exec() and dlg.result_fills:
            for f in dlg.result_fills:
                self._doc.add(f)
            self._status.showMessage(f"Queued {len(dlg.result_fills)} form fill(s)", 4000)

    def _on_header_footer(self) -> None:
        if self._doc is None:
            return
        dlg = HeaderFooterDialog(self)
        if dlg.exec() and dlg.result_edit is not None:
            self._doc.add(dlg.result_edit)
            if self._canvas is not None:
                self._canvas.refresh()
            self._status.showMessage("Header/footer queued", 3000)

    def _on_watermark(self) -> None:
        if self._doc is None:
            return
        dlg = WatermarkDialog(self)
        if dlg.exec() and dlg.result_edit is not None:
            self._doc.add(dlg.result_edit)
            if self._canvas is not None:
                self._canvas.refresh()
            self._status.showMessage("Watermark queued", 3000)

    def _on_bookmark(self) -> None:
        if self._doc is None or self._canvas is None:
            return
        dlg = BookmarkDialog(self._canvas.page_index(), self)
        if dlg.exec() and dlg.result_title:
            self._doc.add(Bookmark(title=dlg.result_title, page=dlg.result_page))
            self._status.showMessage("Bookmark queued", 3000)

    def _on_hyperlink(self) -> None:
        if self._canvas is None or self._doc is None:
            return
        dlg = HyperlinkDialog(self)
        if dlg.exec() and dlg.result_uri:
            self._status.showMessage(
                "Now drag a rectangle where the link should go.", 6000,
            )
            tool = _HyperlinkPlaceTool(dlg.result_uri)
            self._canvas.set_tool(tool)

    # --------------------------------------------------------- helpers

    def _update_tool_enabled(self, enabled: bool) -> None:
        for btn in self._tool_buttons.values():
            btn.setEnabled(enabled)

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


from .tools import _DragRectTool  # noqa: E402


class _HyperlinkPlaceTool(_DragRectTool):
    name = "hyperlink_place"

    def __init__(self, uri: str) -> None:
        super().__init__()
        self._uri = uri

    def _commit(self, canvas, bbox):  # noqa: ANN001
        canvas.add_edit(Hyperlink(
            page=canvas.page_index(), bbox=bbox, uri=self._uri,
        ))
