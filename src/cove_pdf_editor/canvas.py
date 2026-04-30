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

import copy
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsItemGroup,
    QGraphicsObject,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
)

from . import theme
from .document import Document, EditText, FreeText, ImageEdit
from .render import (
    PageImage,
    PageSpan,
    extract_images,
    extract_spans,
    image_at,
    page_info,
    render_page,
    span_at,
)


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

    Single-line mode (``multiline=False``): Enter commits, Shift+Enter
    inserts a newline. Used by Edit Text, where the typical replacement
    is a one-liner.

    Multi-line mode (``multiline=True``): Enter inserts a newline,
    Ctrl+Enter commits. Used by Add Text / Text Plus, which behave like
    real text boxes.

    Escape always cancels. Focus-out (clicking elsewhere) always
    commits. The caller connects to ``committed`` or ``cancelled``.
    """

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, multiline: bool = False) -> None:
        super().__init__()
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFlag(QGraphicsItem.ItemIsFocusable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, False)
        self._multiline = multiline
        self._done = False

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() == Qt.Key_Escape:
            # In this app Esc commits (Foxit-style), so the user's typed
            # text is never lost by reflex. Power-user discard is on
            # Ctrl+Esc.
            ctrl = bool(event.modifiers() & Qt.ControlModifier)
            self._finalize(commit=not ctrl)
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            ctrl = bool(event.modifiers() & Qt.ControlModifier)
            shift = bool(event.modifiers() & Qt.ShiftModifier)
            if self._multiline:
                # Plain / Shift Enter inserts a newline; Ctrl+Enter commits.
                if ctrl:
                    self._finalize(commit=True)
                    return
            else:
                # Plain Enter commits; Shift+Enter still inserts a newline.
                if not shift:
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

    def commit_now(self) -> None:
        """Fire the commit path synchronously, regardless of focus state.

        Used when the canvas needs to harvest the current text before
        tearing the editor down — focus-out is unreliable when the
        widget never had keyboard focus, but the user's typed text is
        still recoverable via ``toPlainText()``.
        """
        self._finalize(commit=True)


class EditObjectItem(QGraphicsObject):
    """Selectable / movable / resizable wrapper around an Edit dataclass.

    Owns its bbox in scene-pixel coords; mouse-driven move and resize
    write the new geometry back to the underlying ``edit.bbox`` so the
    document model stays the source of truth at save time.
    """

    HANDLE_PX = 10
    MIN_PX = 16
    HANDLE_COLOR = theme.HANDLE_BORDER
    HANDLE_FILL = theme.HANDLE_FILL

    def __init__(self, edit, canvas: "PageCanvas") -> None:  # noqa: ANN001
        super().__init__()
        self._edit = edit
        self._canvas = canvas
        rect = canvas.coord_map().pdf_rect_to_qt(*edit.bbox)
        self.setPos(rect.x(), rect.y())
        self._size = QSizeF(rect.width(), rect.height())
        self._drag_handle: int | None = None
        self._press_origin: QPointF | None = None
        self._press_pos: QPointF | None = None
        self._press_size: QSizeF | None = None
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemIsFocusable,
        )
        # Belt-and-suspenders so drag works on every Qt configuration:
        # explicitly accept the left button (default but worth pinning),
        # opt out of group-event handling, and place items above the page
        # bitmap with a stable Z so itemAt always finds them.
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setHandlesChildEvents(False)
        self.setZValue(10)
        self.setAcceptHoverEvents(True)

    def edit(self):  # noqa: ANN201
        return self._edit

    def boundingRect(self) -> QRectF:
        h = self.HANDLE_PX
        return QRectF(-h, -h, self._size.width() + 2 * h, self._size.height() + 2 * h)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        body = QRectF(0, 0, self._size.width(), self._size.height())
        self._paint_body(painter, body)
        if self.isSelected():
            self._paint_selection(painter, body)

    def _paint_body(self, painter: QPainter, rect: QRectF) -> None:
        raise NotImplementedError

    def _paint_selection(self, painter: QPainter, rect: QRectF) -> None:
        # Dashed selection frame.
        pen = QPen(self.HANDLE_COLOR)
        pen.setStyle(Qt.DashLine)
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)
        # White-filled handles with a solid blue border read clearly against
        # both light and dark page content.
        painter.setPen(QPen(self.HANDLE_COLOR, 1.0))
        painter.setBrush(QBrush(self.HANDLE_FILL))
        for handle_rect in self._handle_rects():
            painter.drawRect(handle_rect)

    # Handle indices: 0 NW, 1 N, 2 NE, 3 W, 4 E, 5 SW, 6 S, 7 SE.
    def _handle_rects(self) -> list[QRectF]:
        h = self.HANDLE_PX
        w, ht = self._size.width(), self._size.height()
        anchors = (
            (0, 0), (w / 2, 0), (w, 0),
            (0, ht / 2),         (w, ht / 2),
            (0, ht), (w / 2, ht), (w, ht),
        )
        return [QRectF(x - h / 2, y - h / 2, h, h) for x, y in anchors]

    def _handle_at(self, pos: QPointF) -> int | None:
        for i, rect in enumerate(self._handle_rects()):
            if rect.contains(pos):
                return i
        return None

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.LeftButton and self.isSelected():
            handle = self._handle_at(event.pos())
            if handle is not None:
                self._drag_handle = handle
                self._press_origin = event.scenePos()
                self._press_pos = QPointF(self.pos())
                self._press_size = QSizeF(self._size)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._drag_handle is not None:
            self._do_resize(event.scenePos(), event.modifiers())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if self._drag_handle is not None:
            self._drag_handle = None
            self._write_back_geometry()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        self._write_back_geometry()

    def _do_resize(self, scene_pos: QPointF, modifiers=Qt.NoModifier) -> None:  # noqa: ANN001
        delta = scene_pos - self._press_origin
        h = self._drag_handle
        new_pos = QPointF(self._press_pos)
        new_w = self._press_size.width()
        new_h = self._press_size.height()
        if h in (0, 3, 5):  # left edge
            shift = min(delta.x(), self._press_size.width() - self.MIN_PX)
            new_pos.setX(self._press_pos.x() + shift)
            new_w = self._press_size.width() - shift
        if h in (2, 4, 7):  # right edge
            new_w = max(self.MIN_PX, self._press_size.width() + delta.x())
        if h in (0, 1, 2):  # top edge
            shift = min(delta.y(), self._press_size.height() - self.MIN_PX)
            new_pos.setY(self._press_pos.y() + shift)
            new_h = self._press_size.height() - shift
        if h in (5, 6, 7):  # bottom edge
            new_h = max(self.MIN_PX, self._press_size.height() + delta.y())

        # Shift held → uniform scale (preserve aspect). Pick the dominant
        # axis and rebuild the rect, anchoring whichever edge is opposite
        # the dragged handle so the resize feels natural.
        if (modifiers & Qt.ShiftModifier
                and self._press_size.width() > 0
                and self._press_size.height() > 0):
            ratio_w = new_w / self._press_size.width()
            ratio_h = new_h / self._press_size.height()
            scale = ratio_w if abs(ratio_w - 1) >= abs(ratio_h - 1) else ratio_h
            min_scale = self.MIN_PX / max(
                self._press_size.width(), self._press_size.height(),
            )
            scale = max(scale, min_scale)
            new_w = self._press_size.width() * scale
            new_h = self._press_size.height() * scale
            # Re-anchor the side opposite the dragged handle.
            if h in (0, 3, 5):
                new_pos.setX(self._press_pos.x() + self._press_size.width() - new_w)
            else:
                new_pos.setX(self._press_pos.x())
            if h in (0, 1, 2):
                new_pos.setY(self._press_pos.y() + self._press_size.height() - new_h)
            else:
                new_pos.setY(self._press_pos.y())

        self.prepareGeometryChange()
        self.setPos(new_pos)
        self._size = QSizeF(new_w, new_h)
        self.update()

    def hoverMoveEvent(self, event) -> None:  # noqa: ANN001
        if self.isSelected():
            handle = self._handle_at(event.pos())
            cursors = {
                0: Qt.SizeFDiagCursor, 1: Qt.SizeVerCursor, 2: Qt.SizeBDiagCursor,
                3: Qt.SizeHorCursor,                        4: Qt.SizeHorCursor,
                5: Qt.SizeBDiagCursor, 6: Qt.SizeVerCursor, 7: Qt.SizeFDiagCursor,
            }
            self.setCursor(cursors.get(handle, Qt.SizeAllCursor))
        else:
            self.setCursor(Qt.ArrowCursor)
        super().hoverMoveEvent(event)

    def _write_back_geometry(self) -> None:
        cm = self._canvas.coord_map()
        scale = cm.scale
        x_qt, y_qt = self.pos().x(), self.pos().y()
        w_qt, h_qt = self._size.width(), self._size.height()
        x0 = x_qt / scale
        x1 = (x_qt + w_qt) / scale
        y1 = cm.page_height_pt - y_qt / scale
        y0 = cm.page_height_pt - (y_qt + h_qt) / scale
        new_bbox = (x0, y0, x1, y1)
        if new_bbox != self._edit.bbox:
            # Snapshot before the bbox mutation so Ctrl+Z reverts to the
            # pre-drag/resize position.
            self._canvas.take_snapshot()
            self._edit.bbox = new_bbox
            self._canvas.document().dirty = True


class FreeTextItem(EditObjectItem):
    """Movable / resizable free-text box; double-click opens inline edit."""

    def _paint_body(self, painter: QPainter, rect: QRectF) -> None:
        pen = QPen(theme.FREETEXT_BORDER)
        pen.setStyle(Qt.DashLine)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)
        edit = self._edit
        font = _qt_font_from_pdf(edit.fontname, edit.fontsize * RENDER_SCALE)
        if edit.bold:
            font.setBold(True)
        if edit.italic:
            font.setItalic(True)
        if edit.underline:
            font.setUnderline(True)
        painter.setFont(font)
        painter.setPen(QColor(*edit.color))
        flags = {
            "left": Qt.AlignLeft,
            "center": Qt.AlignHCenter,
            "right": Qt.AlignRight,
        }[edit.align] | Qt.AlignTop | Qt.TextWordWrap
        painter.drawText(rect.adjusted(2, 2, -2, -2), flags, edit.text)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.LeftButton:
            self._canvas.start_freetext_edit(self._edit)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class EditTextItem(EditObjectItem):
    """Movable / resizable wrapper around an EditText replacement.

    Renders a white background (so the original baked-in glyphs in the
    page bitmap don't bleed through) plus the replacement text in the
    captured font / size / style. Double-click reopens the inline editor
    to amend the text.
    """

    def _paint_body(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QPen(Qt.NoPen))
        painter.setBrush(QBrush(Qt.white))
        painter.drawRect(rect)
        edit = self._edit
        if not edit.new_text:
            return
        font = _qt_font_from_pdf(edit.fontname, edit.fontsize * RENDER_SCALE)
        if edit.bold:
            font.setBold(True)
        if edit.italic:
            font.setItalic(True)
        # Shrink to fit if the replacement is wider than the bbox.
        max_w = rect.width()
        metrics = QFontMetricsF(font)
        size_px = float(font.pixelSize())
        while metrics.horizontalAdvance(edit.new_text) > max_w and size_px > 6:
            size_px -= 0.5
            font.setPixelSize(max(6, int(size_px)))
            metrics = QFontMetricsF(font)
        painter.setFont(font)
        painter.setPen(QColor(*edit.color))
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignTop, edit.new_text)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.LeftButton:
            self._canvas.start_edittext_reedit(self._edit)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ImageObjectItem(EditObjectItem):
    """Movable / resizable image."""

    def __init__(self, edit, canvas: "PageCanvas") -> None:  # noqa: ANN001
        super().__init__(edit, canvas)
        self._pixmap = QPixmap(str(edit.image_path))

    def _paint_body(self, painter: QPainter, rect: QRectF) -> None:
        if self._pixmap.isNull():
            pen = QPen(QColor(180, 100, 100))
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)
            return
        painter.drawPixmap(rect, self._pixmap, QRectF(self._pixmap.rect()))


class PageCanvas(QGraphicsView):
    """Single-page canvas. The main window swaps pages by calling
    :meth:`set_page`."""

    pageChanged = Signal(int)
    editAdded = Signal(object)
    selectionChanged = Signal(object)  # carries the selected Edit, or None
    statusMessage = Signal(str)
    toolChanged = Signal(str)  # name of the now-active tool (e.g. "select")

    UNDO_LIMIT = 50

    def __init__(self, document: Document, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self._doc = document
        self._page_index = 0
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setStyleSheet(
            f"QGraphicsView {{ background: {theme.VIEW_BG_HEX}; border: none; }}"
        )

        self._bg_item: QGraphicsPixmapItem | None = None
        self._overlay_group: QGraphicsItemGroup | None = None
        self._object_items: list[EditObjectItem] = []
        self._spans: list[PageSpan] = []
        self._page_images: list[PageImage] = []
        # Cache of extracted source-image bytes written to temp files,
        # keyed by xref so promoting the same image twice doesn't write
        # twice.
        self._promoted_image_paths: dict[int, "Path"] = {}
        # Snapshot-based undo / redo. Each entry is a deep copy of
        # ``self._doc.edits``. ``take_snapshot`` pushes onto the undo
        # stack and clears redo; ``undo`` moves a snapshot from undo to
        # redo; ``redo`` does the inverse. Both bounded to UNDO_LIMIT.
        self._undo_stack: list[list] = []
        self._redo_stack: list[list] = []
        self._coord = CoordMap(1.0, 1.0)
        self._tool: Tool | None = None
        # Active inline edit (if any). While set, the corresponding edit's
        # static preview is suppressed so the user sees only their editable
        # text, not both the old static render plus the live editor.
        self._editing_edit: object | None = None
        self._active_editor: EditableTextItem | None = None
        # Tracks where the current mouse press was routed (scene vs tool)
        # so move/release follow the same path. Set in mousePressEvent.
        self._press_target: str = "none"

        self.setFocusPolicy(Qt.StrongFocus)
        self._scene.selectionChanged.connect(self._emit_selection)
        self._load_page(0)

    def is_inline_editing(self) -> bool:
        return self._active_editor is not None

    def _emit_selection(self) -> None:
        # While the user is inline-editing an existing FreeText, expose
        # that edit so the formatting toolbar stays visible during typing.
        if self._editing_edit is not None:
            self.selectionChanged.emit(self._editing_edit)
            return
        for item in self._scene.selectedItems():
            if isinstance(item, EditObjectItem):
                self.selectionChanged.emit(item.edit())
                return
        self.selectionChanged.emit(None)

    # --- page control ----------------------------------------------

    def document(self) -> Document:
        return self._doc

    def page_index(self) -> int:
        return self._page_index

    def coord_map(self) -> CoordMap:
        return self._coord

    def spans(self) -> list[PageSpan]:
        return self._spans

    def span_at_pdf_point(self, x_pt: float, y_pt: float) -> PageSpan | None:
        return span_at(self._spans, x_pt, y_pt)

    def image_at_pdf_point(self, x_pt: float, y_pt: float) -> PageImage | None:
        return image_at(self._page_images, x_pt, y_pt)

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
        # scene.clear() destroyed every item; clear our caches too.
        self._object_items.clear()
        self._scene.setSceneRect(QRectF(0, 0, pix.width(), pix.height()))
        self._bg_item = self._scene.addPixmap(pix)
        self._overlay_group = self._scene.createItemGroup([])
        self._spans = extract_spans(self._doc.source, idx)
        self._page_images = extract_images(self._doc.source, idx)
        self._refresh_overlay()
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def refresh(self) -> None:
        """Repaint the overlay layer (call after a new edit is added)."""
        self._refresh_overlay()

    def _refresh_overlay(self) -> None:
        if self._overlay_group is None:
            return
        # Tear down interactive items.
        for item in self._object_items:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
        self._object_items.clear()
        # Wipe the static overlay group (used by the EditText whiteout/redraw).
        self._scene.removeItem(self._overlay_group)
        self._overlay_group = self._scene.createItemGroup([])
        for edit in self._doc.edits_for_page(self._page_index):
            if edit is self._editing_edit:
                # Hide the static preview while the user is live-editing it.
                continue
            if isinstance(edit, EditText):
                # Whiteout the original glyphs at original_bbox so they
                # don't peek through if the user moved the replacement.
                self._whiteout_source_area(edit.original_bbox or edit.bbox)
                self._add_object_item(EditTextItem(edit, self))
            elif isinstance(edit, FreeText):
                self._add_object_item(FreeTextItem(edit, self))
            elif isinstance(edit, ImageEdit):
                # Promoted source image: mask the original bake-in so the
                # user doesn't see two copies of it (one from the page
                # bitmap, one from the new ImageObjectItem).
                if edit.original_bbox is not None:
                    self._whiteout_source_area(edit.original_bbox)
                if edit.image_path is not None:
                    self._add_object_item(ImageObjectItem(edit, self))

    def _whiteout_source_area(self, bbox_pdf: tuple[float, float, float, float]) -> None:
        rect = self._coord.pdf_rect_to_qt(*bbox_pdf)
        # Slight outward pad so we don't leave a thin sliver of the
        # original image's edge peeking from under the whiteout.
        pad = 1.5
        rect.adjust(-pad, -pad, pad, pad)
        item = self._scene.addRect(rect, QPen(Qt.NoPen), QBrush(Qt.white))
        self._overlay_group.addToGroup(item)

    def _add_object_item(self, item: EditObjectItem) -> None:
        self._scene.addItem(item)
        self._object_items.append(item)

    # --- tool dispatch ----------------------------------------------

    def set_tool(self, tool: Tool | None) -> None:
        self._tool = tool
        # Select / no-tool gets the default arrow cursor so the canvas
        # doesn't look like it's still asking for a placement gesture.
        # Placement tools get the crosshair.
        self.setCursor(Qt.ArrowCursor if self._passthrough() else Qt.CrossCursor)
        self.toolChanged.emit(tool.name if tool is not None else "select")

    def return_to_select(self) -> None:
        """Switch the canvas back to Select / Edit mode. Placement tools
        (Add Text, Add Image) call this after a successful placement so
        the user can immediately drag/resize the new object without
        first switching tools by hand."""
        from .tools import SelectTool  # local import to avoid cycle
        self.set_tool(SelectTool())

    def _passthrough(self) -> bool:
        """In Select / Edit mode mouse events fall through to the scene so
        items can be selected, dragged, and resized."""
        return self._tool is None or self._tool.name == "select"

    def _scene_press_target(self, scene_pos: QPointF):  # noqa: ANN201
        """Return the scene-side target the press should be routed to,
        or ``None`` if the active tool should handle it. Two cases steal
        the click:

        - The active inline editor (so the user can place their caret or
          select text without ending the edit).
        - An already-selected ``EditObjectItem`` (so the user can drag or
          resize it without switching back to the Select tool first).
        """
        if self._active_editor is not None:
            if self._active_editor.sceneBoundingRect().contains(scene_pos):
                return self._active_editor
        item = self._scene.itemAt(scene_pos, self.transform())
        if isinstance(item, EditObjectItem) and item.isSelected():
            return item
        return None

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        self._press_target = "none"
        if event.button() != Qt.LeftButton:
            self._press_target = "scene"
            super().mousePressEvent(event)
            return
        scene_pos = self.mapToScene(event.pos())
        if self._passthrough():
            # Select mode. If the click lands on the page bitmap (not
            # an existing overlay item), try to promote a source PDF
            # image OR text span at that point into an editable object.
            # In either case, forward the press to ``super`` so the
            # newly-promoted item grabs the mouse for an immediate
            # drag — the user shouldn't need a second click to start
            # moving what they just clicked on.
            item = self._scene.itemAt(scene_pos, self.transform())
            if not isinstance(item, EditObjectItem):
                x_pt, y_pt = self._coord.qt_to_pdf(scene_pos)
                if (self._try_promote_image_at(x_pt, y_pt)
                        or self._try_promote_text_at(x_pt, y_pt)):
                    self._press_target = "scene"
                    super().mousePressEvent(event)
                    return
            self._press_target = "scene"
            super().mousePressEvent(event)
            return
        # Tool active.
        if self._scene_press_target(scene_pos) is not None:
            # Already-selected page object → scene handles drag/resize.
            self._press_target = "scene"
            super().mousePressEvent(event)
            return
        # Click misses any selected object: commit any active inline
        # editor (so the user's pending text is captured), drop selection,
        # then dispatch to the active tool.
        focused = self._scene.focusItem()
        if isinstance(focused, EditableTextItem):
            focused.clearFocus()  # fires focusOutEvent → finalize commit
        self._scene.clearSelection()
        self._press_target = "tool"
        self._tool.press(self, scene_pos)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._press_target == "scene" or self._passthrough():
            super().mouseMoveEvent(event)
            return
        if self._press_target == "tool":
            self._tool.move(self, self.mapToScene(event.pos()))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        target = self._press_target
        self._press_target = "none"
        if target == "scene" or self._passthrough() or event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if target == "tool":
            self._tool.release(self, self.mapToScene(event.pos()))
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001
        if event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        scene_pos = self.mapToScene(event.pos())
        item = self._scene.itemAt(scene_pos, self.transform())
        # A double-click on a page object always reaches the item (e.g.
        # FreeTextItem.mouseDoubleClickEvent reopens the text editor),
        # regardless of which tool is active.
        if isinstance(item, EditObjectItem):
            super().mouseDoubleClickEvent(event)
            return
        # Outside any overlay object: try to edit a source-PDF text
        # span. Same behavior in Select mode and in any tool that
        # doesn't define its own double_click handler — so users don't
        # have to switch to Edit Text mode just to fix a typo.
        if self._try_edit_text_at(scene_pos):
            return
        # Defer to the active tool's own double-click handler if it
        # has one (EditTextTool.double_click does the same span
        # search — kept for symmetry).
        handler = getattr(self._tool, "double_click", None) if self._tool else None
        if handler is not None:
            handler(self, scene_pos)
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        # If the inline editor has scene focus, defer EVERY key event
        # to the scene → item path so the editor handles Esc, Enter,
        # Backspace, Ctrl+Z, etc. natively. The view-level shortcuts
        # below only apply when no editor is active.
        focused = self._scene.focusItem()
        if isinstance(focused, EditableTextItem):
            super().keyPressEvent(event)
            return
        if event.matches(QKeySequence.Undo):
            if self.undo():
                event.accept()
                return
        if event.matches(QKeySequence.Redo):
            if self.redo():
                event.accept()
                return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            if self._delete_selected():
                event.accept()
                return
        if event.key() == Qt.Key_Escape:
            self._scene.clearSelection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _delete_selected(self) -> bool:
        # Take one snapshot covering the whole batch before any mutation.
        if not any(it.isSelected() for it in self._object_items):
            return False
        self.take_snapshot()
        removed = False
        for item in list(self._object_items):
            if not item.isSelected():
                continue
            edit = item.edit()
            if isinstance(edit, ImageEdit) and edit.original_bbox is not None:
                # Promoted source image: leave a tombstone in the doc so
                # the original baked-in pixels still get whiteouted on
                # save, but stop drawing the moved image.
                edit.image_path = None
                edit.bbox = edit.original_bbox
                self._doc.dirty = True
                self._scene.removeItem(item)
                self._object_items.remove(item)
                self._refresh_overlay()
            else:
                self._doc.remove(edit)
                self._scene.removeItem(item)
                self._object_items.remove(item)
            removed = True
        return removed

    # --- undo (snapshot-based) -------------------------------------

    def take_snapshot(self) -> None:
        """Push a deep copy of ``Document.edits`` onto the undo stack and
        clear the redo stack (a fresh edit invalidates any pending
        redos). Bounded to ``UNDO_LIMIT``."""
        self._undo_stack.append(copy.deepcopy(self._doc.edits))
        if len(self._undo_stack) > self.UNDO_LIMIT:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self) -> bool:
        """Pop the top undo snapshot and replace ``Document.edits``,
        pushing the current state onto the redo stack first."""
        if not self._undo_stack:
            return False
        self._redo_stack.append(copy.deepcopy(self._doc.edits))
        if len(self._redo_stack) > self.UNDO_LIMIT:
            self._redo_stack.pop(0)
        self._doc.edits = self._undo_stack.pop()
        self._doc.dirty = True
        self._refresh_overlay()
        self._emit_selection()
        return True

    def redo(self) -> bool:
        """Replay the most recently undone change. Pushes the current
        state back onto the undo stack so Ctrl+Z immediately works again."""
        if not self._redo_stack:
            return False
        self._undo_stack.append(copy.deepcopy(self._doc.edits))
        if len(self._undo_stack) > self.UNDO_LIMIT:
            self._undo_stack.pop(0)
        self._doc.edits = self._redo_stack.pop()
        self._doc.dirty = True
        self._refresh_overlay()
        self._emit_selection()
        return True

    def add_edit(self, edit) -> None:
        self.take_snapshot()
        self._doc.add(edit)
        self._refresh_overlay()
        self.editAdded.emit(edit)

    def commit_active_editor(self) -> None:
        """Force any in-flight inline editor to finalize.

        Drives the same commit path as Enter / Ctrl+Enter, which fires
        each editor's ``on_commit`` callback — the place where
        dataclass fields like ``FreeText.text`` / ``EditText.new_text``
        are written, and where a fresh placement is appended via
        ``add_edit``. Call this immediately before a save so the user's
        typed-but-unsubmitted text is captured into ``Document.edits``
        rather than dropped. Safe to call when no editor is active.
        """
        editor = self._active_editor
        if editor is None:
            return
        editor.commit_now()

    def reset_for_saved_source(self) -> None:
        """Re-sync the canvas after the document has been saved + rebased.

        Drops any active inline editor, clears the undo / redo stacks
        (their snapshots describe pre-save pending edits that are now
        baked into ``self._doc.source`` — replaying them would double-
        apply), forgets cached promoted-image extractions, and reloads
        the current page from the saved PDF. Because ``_load_page``
        rebuilds the overlay from ``self._doc.edits`` (which the caller
        has cleared on save), all stale ``EditObjectItem``s disappear
        with the rebuild.
        """
        if self._active_editor is not None:
            try:
                self._scene.removeItem(self._active_editor)
            except Exception:  # noqa: BLE001
                pass
        self._active_editor = None
        self._editing_edit = None
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._promoted_image_paths.clear()
        idx = self._page_index if 0 <= self._page_index < self._doc.page_count else 0
        self._load_page(idx)
        self._emit_selection()

    # --- source-PDF promotion --------------------------------------

    def _try_promote_text_at(self, x_pt: float, y_pt: float) -> bool:
        """If the click landed on a source-PDF text span, turn that span
        into an editable :class:`EditText` (or re-select the existing
        promoted edit) so the user can drag/resize/edit it like an added
        text box. Returns True if the click was handled.

        The new EditText starts with ``original_bbox == bbox`` and
        ``new_text == old_text == span.text`` — a no-op until the user
        modifies it. Save will redact ``original_bbox`` and draw the
        (possibly moved/resized) ``new_text`` at the current ``bbox``.
        """
        span = self.span_at_pdf_point(x_pt, y_pt)
        if span is None:
            return False
        # Already promoted at this exact source bbox? Re-select it.
        for e in self._doc.edits:
            if (isinstance(e, EditText) and e.original_bbox is not None
                    and e.original_bbox == span.bbox):
                self.select_edit(e)
                return True
        edit = EditText(
            page=self._page_index,
            bbox=span.bbox,
            old_text=span.text,
            new_text=span.text,
            fontname=span.fontname,
            fontsize=span.fontsize,
            color=span.color,
            bold=span.bold,
            italic=span.italic,
            original_bbox=span.bbox,
        )
        self.add_edit(edit)
        self.select_edit(edit)
        return True

    def _try_promote_image_at(self, x_pt: float, y_pt: float) -> bool:
        """If the click landed on a source-PDF image, turn that image
        into an editable :class:`ImageEdit` (or re-select the existing
        promoted edit) so the user can drag/resize/delete it like an
        added image. Returns True if the click was handled."""
        src = self.image_at_pdf_point(x_pt, y_pt)
        if src is None:
            return False
        # Already promoted? Re-select the existing edit so we don't
        # stack duplicates.
        for e in self._doc.edits:
            if (isinstance(e, ImageEdit) and e.original_bbox is not None
                    and e.original_bbox == src.bbox):
                self.select_edit(e)
                return True
        path = self._extract_promoted_image(src)
        if path is None:
            return False
        edit = ImageEdit(
            page=self._page_index,
            bbox=src.bbox,
            image_path=path,
            original_bbox=src.bbox,
        )
        self.add_edit(edit)
        self.select_edit(edit)
        return True

    def _extract_promoted_image(self, src: PageImage) -> "Path | None":
        cached = self._promoted_image_paths.get(src.xref)
        if cached is not None and cached.exists():
            return cached
        if not src.image_bytes:
            return None
        import tempfile
        from pathlib import Path
        ext = src.ext or "png"
        tmp = Path(tempfile.gettempdir()) / f"cove-promoted-{id(self)}-{src.xref}.{ext}"
        try:
            tmp.write_bytes(src.image_bytes)
        except Exception:
            return None
        self._promoted_image_paths[src.xref] = tmp
        return tmp

    # --- source-PDF text editing -----------------------------------

    def _try_edit_text_at(self, scene_pos: QPointF) -> bool:
        """Open the inline editor on whatever editable text the click hit
        — a previously-replaced span (re-edit) or a fresh PyMuPDF span
        from the source PDF. Returns True if something was opened."""
        x_pt, y_pt = self._coord.qt_to_pdf(scene_pos)
        existing = self.find_edit_at_pdf_point(x_pt, y_pt)
        if existing is not None:
            def commit_existing(text: str) -> None:
                if text != existing.new_text:
                    self.take_snapshot()
                existing.new_text = text
                self._doc.dirty = True
            self.start_inline_edit(
                initial_text=existing.new_text,
                bbox_pdf=existing.bbox,
                fontname=existing.fontname,
                fontsize=existing.fontsize,
                color=existing.color,
                bold=existing.bold,
                italic=existing.italic,
                suppress_edit=existing,
                on_commit=commit_existing,
            )
            return True
        span = self.span_at_pdf_point(x_pt, y_pt)
        if span is None:
            return False
        page = self._page_index
        def commit_new(text: str) -> None:
            if text and text != span.text:
                self.add_edit(EditText(
                    page=page, bbox=span.bbox,
                    old_text=span.text, new_text=text,
                    fontname=span.fontname, fontsize=span.fontsize,
                    color=span.color, bold=span.bold, italic=span.italic,
                ))
        self.start_inline_edit(
            initial_text=span.text,
            bbox_pdf=span.bbox,
            fontname=span.fontname,
            fontsize=span.fontsize,
            color=span.color,
            bold=span.bold,
            italic=span.italic,
            on_commit=commit_new,
        )
        return True

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
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        align: str = "left",
        multiline: bool = False,
        suppress_edit: object | None = None,
        on_commit,        # callback(text: str) -> None
    ) -> EditableTextItem:
        """Drop an editable text item into the scene at ``bbox_pdf``.

        Enter / focus-out commits → calls ``on_commit(text)``.
        Escape cancels → ``on_commit`` is not called.

        If ``suppress_edit`` is given, that edit's static preview is
        hidden while the editor is open (so the user doesn't see two
        layers of the same text).
        """
        cm = self._coord
        rect = cm.pdf_rect_to_qt(*bbox_pdf)
        pad = 1.0

        whiteout = self._scene.addRect(
            rect.x() - pad, rect.y() - pad,
            rect.width() + 2 * pad, rect.height() + 2 * pad,
            QPen(Qt.NoPen), QBrush(Qt.white),
        )
        # Darker, slightly thicker dashed border so the editing rect is
        # clearly visible on white pages. UI-only — never drawn into the
        # saved PDF.
        border_pen = QPen(theme.INLINE_EDIT_BORDER)
        border_pen.setStyle(Qt.DashLine)
        border_pen.setWidthF(1.6)
        border = self._scene.addRect(
            rect.x() - pad, rect.y() - pad,
            rect.width() + 2 * pad, rect.height() + 2 * pad,
            border_pen, QBrush(Qt.NoBrush),
        )

        item = EditableTextItem(multiline=multiline)
        self._scene.addItem(item)
        font = _qt_font_from_pdf(fontname, fontsize * RENDER_SCALE)
        if bold:
            font.setBold(True)
        if italic:
            font.setItalic(True)
        if underline:
            font.setUnderline(True)
        item.setFont(font)
        item.setDefaultTextColor(QColor(*color))
        item.setPlainText(initial_text)
        item.setPos(rect.x(), rect.y() - 2)
        if multiline:
            # Visual word-wrap inside the placement rectangle. Save-side
            # wrapping (overlay._wrap_lines) matches.
            item.setTextWidth(max(rect.width() - 4, 16))
        _apply_align_to_doc(item, align)

        self._editing_edit = suppress_edit
        self._active_editor = item
        if suppress_edit is not None:
            self._refresh_overlay()

        item.setFocus(Qt.MouseFocusReason)
        cursor = item.textCursor()
        from PySide6.QtGui import QTextCursor
        if suppress_edit is None:
            # Fresh placement (Add Text / Text Plus / new EditText replacement):
            # select all so the first keystroke replaces any placeholder text.
            cursor.select(QTextCursor.Document)
        else:
            # Re-editing existing text: drop the caret at the end so the user
            # can append or correct without nuking the whole content.
            cursor.movePosition(QTextCursor.End)
        item.setTextCursor(cursor)

        def _cleanup() -> None:
            edited = self._editing_edit
            for it in (item, whiteout, border):
                try:
                    self._scene.removeItem(it)
                except Exception:
                    pass
            self._editing_edit = None
            self._active_editor = None
            self._refresh_overlay()
            # Restore selection on the just-edited object so the formatting
            # toolbar stays focused on it; otherwise sync the toolbar to the
            # actual (possibly empty) scene selection.
            if edited is not None:
                self.select_edit(edited)
            else:
                self._emit_selection()

        def _commit(text: str) -> None:
            _cleanup()
            on_commit(text)

        def _cancel() -> None:
            _cleanup()

        item.committed.connect(_commit)
        item.cancelled.connect(_cancel)
        return item

    def start_freetext_edit(self, edit) -> None:  # noqa: ANN001
        """Re-edit an existing FreeText (e.g. after double-click)."""
        def commit(text: str) -> None:
            if text != edit.text:
                self.take_snapshot()
            edit.text = text
            self._doc.dirty = True

        self.start_inline_edit(
            initial_text=edit.text,
            bbox_pdf=edit.bbox,
            fontname=edit.fontname,
            fontsize=edit.fontsize,
            color=edit.color,
            bold=edit.bold,
            italic=edit.italic,
            underline=edit.underline,
            align=edit.align,
            multiline=True,
            suppress_edit=edit,
            on_commit=commit,
        )

    def start_edittext_reedit(self, edit) -> None:  # noqa: ANN001
        """Re-edit an existing EditText replacement (after double-click).
        Opens at the *current* bbox (which may have been moved); the
        original_bbox stays pinned for the source-area whiteout."""
        def commit(text: str) -> None:
            if text != edit.new_text:
                self.take_snapshot()
            edit.new_text = text
            self._doc.dirty = True

        self.start_inline_edit(
            initial_text=edit.new_text,
            bbox_pdf=edit.bbox,
            fontname=edit.fontname,
            fontsize=edit.fontsize,
            color=edit.color,
            bold=edit.bold,
            italic=edit.italic,
            suppress_edit=edit,
            on_commit=commit,
        )

    def refresh_item_for(self, edit) -> None:  # noqa: ANN001
        """Repaint the scene item bound to ``edit`` (cheap; doesn't rebuild).
        Also restyles the inline editor live if ``edit`` is the one being
        edited, so the user sees formatting changes while typing."""
        for item in self._object_items:
            if item.edit() is edit:
                item.update()
                break
        if self._editing_edit is edit and self._active_editor is not None:
            _apply_style_to_editor(self._active_editor, edit)

    def select_edit(self, edit) -> None:  # noqa: ANN001
        """Programmatically select the scene item bound to ``edit``."""
        self._scene.clearSelection()
        for item in self._object_items:
            if item.edit() is edit:
                item.setSelected(True)
                return

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        if self._scene.sceneRect().width() > 0:
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)


_ALIGN_QT = {
    "left":   Qt.AlignLeft,
    "center": Qt.AlignHCenter,
    "right":  Qt.AlignRight,
}


def _apply_align_to_doc(item: QGraphicsTextItem, align: str) -> None:
    """Set paragraph alignment on a QGraphicsTextItem's underlying document."""
    opt = item.document().defaultTextOption()
    opt.setAlignment(_ALIGN_QT.get(align, Qt.AlignLeft))
    item.document().setDefaultTextOption(opt)


def _apply_style_to_editor(editor: "EditableTextItem", edit) -> None:  # noqa: ANN001
    """Push the FreeText edit's current style into the live inline editor
    so toolbar changes are visible while the user is typing."""
    font = _qt_font_from_pdf(edit.fontname, edit.fontsize * RENDER_SCALE)
    if edit.bold:
        font.setBold(True)
    if edit.italic:
        font.setItalic(True)
    if edit.underline:
        font.setUnderline(True)
    editor.setFont(font)
    editor.setDefaultTextColor(QColor(*edit.color))
    _apply_align_to_doc(editor, edit.align)


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
