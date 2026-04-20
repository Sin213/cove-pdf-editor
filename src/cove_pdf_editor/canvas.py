"""Interactive page canvas.

A ``QGraphicsView`` hosting the rendered page bitmap plus an overlay
layer that previews pending edits. The active tool receives translated
mouse events (Qt → PDF coordinates) and produces ``Edit`` entries that
get appended to the :class:`Document`.

Coordinate convention: Qt is top-left origin, y grows downward. PDF is
bottom-left origin, y grows upward. All tools speak PDF coords so the
Edits in ``Document.edits`` are directly consumable by the overlay
writer without further transforms.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsItemGroup,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QWidget,
)

from .document import Document, EditText
from .render import PageChar, extract_chars, page_info, render_page, span_bbox, span_text


RENDER_SCALE = 2.0   # pypdfium2 scale; 2.0 = 144dpi which reads crisply


@dataclass
class CoordMap:
    """Converts between PDF points and Qt scene pixels for one page."""
    page_width_pt: float
    page_height_pt: float
    scale: float = RENDER_SCALE

    def qt_to_pdf(self, p: QPointF) -> tuple[float, float]:
        x_pt = p.x() / self.scale
        y_pt = (self.page_height_pt * self.scale - p.y()) / self.scale
        return x_pt, y_pt

    def pdf_to_qt(self, x_pt: float, y_pt: float) -> QPointF:
        return QPointF(x_pt * self.scale,
                       (self.page_height_pt - y_pt) * self.scale)

    def pdf_rect_to_qt(self, x0: float, y0: float, x1: float, y1: float) -> QRectF:
        # Flip Y. In Qt the top y is smaller; in PDF the top y is larger.
        qt_top = (self.page_height_pt - y1) * self.scale
        qt_bottom = (self.page_height_pt - y0) * self.scale
        return QRectF(x0 * self.scale, qt_top,
                      (x1 - x0) * self.scale, qt_bottom - qt_top)


class Tool(Protocol):
    name: str
    def press(self, canvas: "PageCanvas", qt: QPointF) -> None: ...
    def move(self, canvas: "PageCanvas", qt: QPointF) -> None: ...
    def release(self, canvas: "PageCanvas", qt: QPointF) -> None: ...


class EditableTextItem(QGraphicsTextItem):
    """A QGraphicsTextItem that behaves like a real inline text editor.

    Enter (without Shift) commits. Escape cancels. Clicking elsewhere
    (focus-out) also commits — same behaviour Foxit / Word / Acrobat all
    use. The caller connects to ``committed`` or ``cancelled`` to apply
    the result.
    """

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFlag(QGraphicsItem.ItemIsFocusable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, False)
        self._done = False

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() == Qt.Key_Escape:
            self._finalize(commit=False)
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
            event.modifiers() & Qt.ShiftModifier
        ):
            self._finalize(commit=True)
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: ANN001
        super().focusOutEvent(event)
        self._finalize(commit=True)

    def _finalize(self, *, commit: bool) -> None:
        if self._done:
            return
        self._done = True
        if commit:
            self.committed.emit(self.toPlainText())
        else:
            self.cancelled.emit()


class PageCanvas(QGraphicsView):
    """Single-page canvas. The main window swaps pages by calling
    :meth:`set_page`."""

    pageChanged = Signal(int)
    editAdded = Signal(object)

    def __init__(self, document: Document, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self._doc = document
        self._page_index = 0
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setStyleSheet("QGraphicsView { background:#1a1d24; border:none; }")

        self._bg_item: QGraphicsPixmapItem | None = None
        self._overlay_group: QGraphicsItemGroup | None = None
        self._chars: list[PageChar] = []
        self._coord = CoordMap(1.0, 1.0)
        self._tool: Tool | None = None
        # Active inline edit (if any). While set, the corresponding edit's
        # static preview is suppressed so the user sees only their editable
        # text, not both the old static render plus the live editor.
        self._editing_edit: EditText | None = None

        self._load_page(0)

    # --- page control ----------------------------------------------

    def document(self) -> Document:
        return self._doc

    def page_index(self) -> int:
        return self._page_index

    def coord_map(self) -> CoordMap:
        return self._coord

    def chars(self) -> list[PageChar]:
        return self._chars

    def set_page(self, idx: int) -> None:
        if idx == self._page_index or not (0 <= idx < self._doc.page_count):
            return
        self._load_page(idx)
        self.pageChanged.emit(idx)

    def _load_page(self, idx: int) -> None:
        self._page_index = idx
        info = page_info(self._doc.source, idx)
        self._coord = CoordMap(info.width, info.height, RENDER_SCALE)
        image = render_page(self._doc.source, idx, scale=RENDER_SCALE)
        pix = QPixmap.fromImage(image)
        self._scene.clear()
        self._scene.setSceneRect(QRectF(0, 0, pix.width(), pix.height()))
        self._bg_item = self._scene.addPixmap(pix)
        self._overlay_group = self._scene.createItemGroup([])
        self._chars = extract_chars(self._doc.source, idx)
        self._refresh_overlay()
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def refresh(self) -> None:
        """Repaint the overlay layer (call after a new edit is added)."""
        self._refresh_overlay()

    def _refresh_overlay(self) -> None:
        if self._overlay_group is None:
            return
        # Wipe old overlay items.
        self._scene.removeItem(self._overlay_group)
        self._overlay_group = self._scene.createItemGroup([])
        for edit in self._doc.edits_for_page(self._page_index):
            if edit is self._editing_edit:
                # Hide the static preview while the user is live-editing it.
                continue
            self._draw_edit_preview(edit)

    def _draw_edit_preview(self, edit) -> None:
        from .document import (
            EditText,
            FreeText,
            Ink,
            Markup,
            Note,
            Shape,
            Stamp,
        )
        cm = self._coord
        if isinstance(edit, EditText):
            self._draw_edit_text_preview(edit)
            return
        pen = QPen(QColor(100, 180, 255))
        pen.setWidth(1)
        pen.setStyle(Qt.DashLine)
        if isinstance(edit, (FreeText, Shape, Stamp, Markup)):
            rect = cm.pdf_rect_to_qt(*edit.bbox)
            item = self._scene.addRect(rect, pen)
            self._overlay_group.addToGroup(item)
            if isinstance(edit, Markup):
                color = QColor(*edit.color, 110)
                fill = self._scene.addRect(rect, QPen(Qt.NoPen), QBrush(color))
                self._overlay_group.addToGroup(fill)
            elif isinstance(edit, FreeText):
                font = _qt_font_from_pdf("Helvetica", edit.fontsize * RENDER_SCALE)
                text_item = self._scene.addSimpleText(edit.text, font)
                text_item.setBrush(QBrush(QColor(*edit.color)))
                text_item.setPos(rect.x() + 2, rect.y() + 2)
                self._overlay_group.addToGroup(text_item)
        elif isinstance(edit, Ink):
            pen2 = QPen(QColor(*edit.color))
            pen2.setWidthF(edit.width * RENDER_SCALE)
            pen2.setCapStyle(Qt.RoundCap)
            pen2.setJoinStyle(Qt.RoundJoin)
            for i in range(1, len(edit.points)):
                p0 = cm.pdf_to_qt(*edit.points[i - 1])
                p1 = cm.pdf_to_qt(*edit.points[i])
                line = self._scene.addLine(p0.x(), p0.y(), p1.x(), p1.y(), pen2)
                self._overlay_group.addToGroup(line)
        elif isinstance(edit, Note):
            marker = self._scene.addEllipse(
                edit.x * RENDER_SCALE - 6,
                (cm.page_height_pt - edit.y) * RENDER_SCALE - 6,
                12, 12,
                QPen(QColor(200, 140, 0)), QBrush(QColor(255, 220, 120)),
            )
            self._overlay_group.addToGroup(marker)

    def _draw_edit_text_preview(self, edit) -> None:
        """White out the original text on the canvas and draw the new text
        at that position in a Qt font that approximates the captured PDF
        font. This is what makes committed edits appear live without a save."""
        cm = self._coord
        rect = cm.pdf_rect_to_qt(*edit.bbox)
        pad = 1.0
        wo = self._scene.addRect(
            rect.x() - pad, rect.y() - pad,
            rect.width() + 2 * pad, rect.height() + 2 * pad,
            QPen(Qt.NoPen), QBrush(Qt.white),
        )
        self._overlay_group.addToGroup(wo)
        if not edit.new_text:
            return
        font = _qt_font_from_pdf(edit.fontname, edit.fontsize * RENDER_SCALE)
        # Shrink to fit if the new text is wider than the original bbox.
        max_w = rect.width()
        metrics = QFontMetricsF(font)
        size_px = float(font.pixelSize())
        while metrics.horizontalAdvance(edit.new_text) > max_w and size_px > 6:
            size_px -= 0.5
            font.setPixelSize(max(6, int(size_px)))
            metrics = QFontMetricsF(font)
        text_item = self._scene.addSimpleText(edit.new_text, font)
        text_item.setBrush(QBrush(QColor(*edit.color)))
        # QGraphicsSimpleTextItem position is the top-left of the text's
        # bounding rect. The PDF bbox top (rect.y()) lines up close enough
        # visually; the exact offset varies by font metrics.
        text_item.setPos(rect.x(), rect.y())
        self._overlay_group.addToGroup(text_item)

    # --- tool dispatch ----------------------------------------------

    def set_tool(self, tool: Tool | None) -> None:
        self._tool = tool
        if tool is None:
            self.setCursor(Qt.ArrowCursor)
        else:
            self.setCursor(Qt.CrossCursor)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if self._tool is None or event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._tool.press(self, self.mapToScene(event.pos()))

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._tool is None:
            super().mouseMoveEvent(event)
            return
        self._tool.move(self, self.mapToScene(event.pos()))

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if self._tool is None or event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        self._tool.release(self, self.mapToScene(event.pos()))

    def add_edit(self, edit) -> None:
        self._doc.add(edit)
        self._refresh_overlay()
        self.editAdded.emit(edit)

    # --- inline text editing ---------------------------------------

    def find_edit_at_pdf_point(self, x_pt: float, y_pt: float) -> EditText | None:
        """Return the EditText edit on the current page whose bbox contains
        the PDF-space point, or None. Used so clicking back on already-edited
        text re-opens with the current text, not the original."""
        for e in self._doc.edits:
            if isinstance(e, EditText) and e.page == self._page_index:
                x0, y0, x1, y1 = e.bbox
                if x0 <= x_pt <= x1 and y0 <= y_pt <= y1:
                    return e
        return None

    def start_inline_edit(
        self,
        *,
        initial_text: str,
        bbox_pdf: tuple[float, float, float, float],
        fontname: str,
        fontsize: float,
        color: tuple[int, int, int] = (0, 0, 0),
        existing_edit: EditText | None = None,
        on_commit_new,   # callback(new_text: str) for creating a new edit
    ) -> EditableTextItem:
        """Drop an editable text item into the scene at ``bbox_pdf``.

        The user can click, select, type, backspace — full text-editor
        interaction. Enter / focus-out commits; Escape cancels.

        If ``existing_edit`` is given, we're re-editing that edit: on
        commit we mutate its ``new_text`` in place. Otherwise a new
        EditText is created via ``on_commit_new``.
        """
        cm = self._coord
        rect = cm.pdf_rect_to_qt(*bbox_pdf)
        pad = 1.0

        # Whiteout under the editor so the original text doesn't show through.
        whiteout = self._scene.addRect(
            rect.x() - pad, rect.y() - pad,
            rect.width() + 2 * pad, rect.height() + 2 * pad,
            QPen(Qt.NoPen), QBrush(Qt.white),
        )
        # Subtle dashed blue border → visual cue that this frame is being edited.
        border_pen = QPen(QColor(95, 180, 255))
        border_pen.setStyle(Qt.DashLine)
        border_pen.setWidthF(1.2)
        border = self._scene.addRect(
            rect.x() - pad, rect.y() - pad,
            rect.width() + 2 * pad, rect.height() + 2 * pad,
            border_pen, QBrush(Qt.NoBrush),
        )

        item = EditableTextItem()
        self._scene.addItem(item)
        font = _qt_font_from_pdf(fontname, fontsize * RENDER_SCALE)
        item.setFont(font)
        item.setDefaultTextColor(QColor(*color))
        item.setPlainText(initial_text)
        item.setPos(rect.x(), rect.y() - 2)

        # Suppress the static preview for the edit being edited.
        self._editing_edit = existing_edit
        if existing_edit is not None:
            self._refresh_overlay()

        # Give it focus and select all so the user can just start typing to
        # replace, or click to place a caret — same as Foxit.
        item.setFocus(Qt.MouseFocusReason)
        cursor = item.textCursor()
        from PySide6.QtGui import QTextCursor
        cursor.select(QTextCursor.Document)
        item.setTextCursor(cursor)

        def _cleanup() -> None:
            for it in (item, whiteout, border):
                try:
                    self._scene.removeItem(it)
                except Exception:
                    pass
            self._editing_edit = None
            self._refresh_overlay()

        def _commit(text: str) -> None:
            if existing_edit is not None:
                existing_edit.new_text = text
                self._doc.dirty = True
                _cleanup()
            else:
                _cleanup()
                if text and text != initial_text:
                    on_commit_new(text)

        def _cancel() -> None:
            _cleanup()

        item.committed.connect(_commit)
        item.cancelled.connect(_cancel)
        return item

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        if self._scene.sceneRect().width() > 0:
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)


def _qt_font_from_pdf(pdf_name: str, pixel_size: float) -> QFont:
    """Map a captured PDF font name to a Qt QFont. Most PDFs use one of
    the base-14 fonts (Helvetica / Times / Courier) or a derivative; we
    pick the nearest Qt family by name and set bold / italic flags based
    on the name."""
    n = pdf_name.lower()
    if "courier" in n or "mono" in n:
        family = "Courier New"
    elif "times" in n or "serif" in n:
        family = "Times New Roman"
    else:
        # Helvetica, Arial, DejaVu Sans, Liberation Sans → Arial works on
        # Windows; Linux has Liberation Sans aliased to Arial by fontconfig.
        family = "Arial"
    font = QFont(family)
    font.setPixelSize(max(6, int(pixel_size)))
    if "bold" in n or "black" in n or "heavy" in n:
        font.setBold(True)
    if "italic" in n or "oblique" in n:
        font.setItalic(True)
    return font
