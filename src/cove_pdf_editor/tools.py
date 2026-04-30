"""Tool implementations.

The ``Tool`` protocol is defined in :mod:`canvas`; tools here just need
``name``, ``press``, ``move``, ``release`` methods.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QFileDialog

from . import theme
from .canvas import PageCanvas
from .document import EditText, FreeText, ImageEdit


# ---------------------------------------------------------------------------
# Select / Edit — no-op tool. Mouse events fall through to the scene so
# QGraphicsItem-based page objects receive selection/move/resize.
# ---------------------------------------------------------------------------

class SelectTool:
    name = "select"

    def press(self, canvas: PageCanvas, qt: QPointF) -> None: pass  # noqa: D401, ANN001
    def move(self, canvas, qt) -> None: pass  # noqa: D401, ANN001
    def release(self, canvas, qt) -> None: pass  # noqa: D401, ANN001


# ---------------------------------------------------------------------------
# Edit Text — the flagship tool.
# ---------------------------------------------------------------------------

class EditTextTool:
    """Double-click an existing PDF text run to replace it in place.

    Single-click is intentionally a no-op: PDFs are dense and a single
    click would too easily commit edits the user didn't intend.
    """

    name = "edit_text"
    NO_SPAN_MSG = "No editable text found here. Use Add Text or Text Plus."

    def press(self, canvas: PageCanvas, qt: QPointF) -> None: pass  # noqa: D401, ANN001
    def move(self, canvas, qt) -> None: pass  # noqa: D401, ANN001
    def release(self, canvas, qt) -> None: pass  # noqa: D401, ANN001

    def double_click(self, canvas: PageCanvas, qt: QPointF) -> None:
        x_pt, y_pt = canvas.coord_map().qt_to_pdf(qt)

        # Re-edit a previously-replaced text: open the editor with the
        # current replacement, not the source PDF's original.
        existing = canvas.find_edit_at_pdf_point(x_pt, y_pt)
        if existing is not None:
            def commit_existing(text: str) -> None:
                if text != existing.new_text:
                    canvas.take_snapshot()
                existing.new_text = text
                canvas.document().dirty = True
            canvas.start_inline_edit(
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
            return

        # Fresh edit: ask PyMuPDF for the text span under the click point.
        span = canvas.span_at_pdf_point(x_pt, y_pt)
        if span is None:
            canvas.statusMessage.emit(self.NO_SPAN_MSG)
            return

        page = canvas.page_index()

        def commit_new(text: str) -> None:
            if text and text != span.text:
                canvas.add_edit(EditText(
                    page=page,
                    bbox=span.bbox,
                    old_text=span.text,
                    new_text=text,
                    fontname=span.fontname,
                    fontsize=span.fontsize,
                    color=span.color,
                    bold=span.bold,
                    italic=span.italic,
                ))

        canvas.start_inline_edit(
            initial_text=span.text,
            bbox_pdf=span.bbox,
            fontname=span.fontname,
            fontsize=span.fontsize,
            color=span.color,
            bold=span.bold,
            italic=span.italic,
            on_commit=commit_new,
        )


# ---------------------------------------------------------------------------
# Drag-rect base: common helper for tools that care about a drag box.
# ---------------------------------------------------------------------------

class _DragRectTool:
    name = "drag_rect"

    # Visible drag preview so the user can see the box while dragging it
    # out — most PDFs are white, so an invisible rect was unusable.
    _PREVIEW_PEN_COLOR = theme.DRAG_PREVIEW_PEN
    _PREVIEW_FILL_COLOR = theme.DRAG_PREVIEW_FILL

    def __init__(self) -> None:
        self._start_pdf: tuple[float, float] | None = None
        self._end_pdf: tuple[float, float] | None = None
        self._preview_item = None  # QGraphicsRectItem while dragging

    def press(self, canvas: PageCanvas, qt: QPointF) -> None:
        self._start_pdf = canvas.coord_map().qt_to_pdf(qt)
        self._end_pdf = self._start_pdf
        pen = QPen(self._PREVIEW_PEN_COLOR)
        pen.setStyle(Qt.DashLine)
        pen.setWidthF(1.5)
        self._preview_item = canvas.scene().addRect(
            self._qt_rect_from_drag(canvas),
            pen,
            QBrush(self._PREVIEW_FILL_COLOR),
        )
        # Keep the preview above page content + overlay items so the
        # outline stays visible regardless of what's underneath.
        self._preview_item.setZValue(1000)

    def move(self, canvas: PageCanvas, qt: QPointF) -> None:
        if self._start_pdf is None:
            return
        self._end_pdf = canvas.coord_map().qt_to_pdf(qt)
        if self._preview_item is not None:
            self._preview_item.setRect(self._qt_rect_from_drag(canvas))

    def release(self, canvas: PageCanvas, qt: QPointF) -> None:
        if self._start_pdf is None:
            return
        self._end_pdf = canvas.coord_map().qt_to_pdf(qt)
        x0, y0 = self._start_pdf
        x1, y1 = self._end_pdf
        self._start_pdf = None
        self._end_pdf = None
        if self._preview_item is not None:
            try:
                canvas.scene().removeItem(self._preview_item)
            except Exception:
                pass
            self._preview_item = None
        bbox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if bbox[2] - bbox[0] < 2 or bbox[3] - bbox[1] < 2:
            return
        self._commit(canvas, bbox)

    def _qt_rect_from_drag(self, canvas: PageCanvas) -> QRectF:
        """Return the current drag bbox mapped to scene-pixel coords."""
        if self._start_pdf is None or self._end_pdf is None:
            return QRectF()
        x0, y0 = self._start_pdf
        x1, y1 = self._end_pdf
        return canvas.coord_map().pdf_rect_to_qt(
            min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
        )

    def _commit(self, canvas: PageCanvas, bbox) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Free-text box.
# ---------------------------------------------------------------------------

class FreeTextTool(_DragRectTool):
    name = "freetext"

    def _commit(self, canvas: PageCanvas, bbox) -> None:
        page = canvas.page_index()

        def commit_new(text: str) -> None:
            if not text.strip():
                return
            edit = FreeText(page=page, bbox=bbox, text=text, fontsize=12.0)
            canvas.add_edit(edit)
            canvas.select_edit(edit)
            # Hand the canvas back to Select mode so the user can drag,
            # resize, format, or delete the new box without switching
            # tools manually.
            canvas.return_to_select()

        canvas.start_inline_edit(
            initial_text="",
            bbox_pdf=bbox,
            fontname="Helvetica",
            fontsize=12.0,
            multiline=True,
            on_commit=commit_new,
        )


# ---------------------------------------------------------------------------
# Text Plus — click anywhere to drop a small editable text. The tool stays
# active after each commit, so it's good for rapidly filling print-only
# form fields without dragging a box every time.
# ---------------------------------------------------------------------------

class TextPlusTool:
    name = "text_plus"

    DEFAULT_W = 150.0   # PDF points
    DEFAULT_H = 18.0    # tall enough for a single 12pt line

    def press(self, canvas: PageCanvas, qt: QPointF) -> None:
        x_pt, y_pt = canvas.coord_map().qt_to_pdf(qt)
        # Anchor the editor's top-left at the click point.
        bbox = (x_pt, y_pt - self.DEFAULT_H, x_pt + self.DEFAULT_W, y_pt)
        page = canvas.page_index()

        def commit(text: str) -> None:
            if not text.strip():
                return
            canvas.add_edit(FreeText(
                page=page, bbox=bbox, text=text, fontsize=12.0,
            ))
            # Note: do NOT select the new edit. Tool stays active for the
            # next click; the user's next click on empty space drops
            # another entry rather than dragging this one.

        canvas.start_inline_edit(
            initial_text="",
            bbox_pdf=bbox,
            fontname="Helvetica",
            fontsize=12.0,
            multiline=True,
            on_commit=commit,
        )

    def move(self, canvas, qt) -> None: pass  # noqa: D401, ANN001
    def release(self, canvas, qt) -> None: pass  # noqa: D401, ANN001


# ---------------------------------------------------------------------------
# Add Image — file picker, then drag-rect to place.
# ---------------------------------------------------------------------------

class AddImageTool(_DragRectTool):
    name = "image"

    def __init__(self) -> None:
        super().__init__()
        self._image_path: Path | None = None

    def prime(self, canvas: PageCanvas) -> bool:
        """Ask for the image to place. Called by the main window before
        the tool becomes active."""
        path, _ = QFileDialog.getOpenFileName(
            canvas, "Pick image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp);;All files (*)",
        )
        if not path:
            return False
        self._image_path = Path(path)
        return True

    def _commit(self, canvas: PageCanvas, bbox) -> None:
        if self._image_path is None:
            return
        edit = ImageEdit(
            page=canvas.page_index(), bbox=bbox, image_path=self._image_path,
        )
        canvas.add_edit(edit)
        canvas.select_edit(edit)
        # Hand the canvas back to Select mode so the user can drag,
        # resize, or delete the new image without switching tools.
        canvas.return_to_select()
