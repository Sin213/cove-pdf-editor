"""All tool implementations share one file — each one is tiny.

The ``Tool`` protocol is defined in :mod:`canvas`; tools here just need
``name``, ``press``, ``move``, ``release`` methods.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMessageBox,
)

from .canvas import PageCanvas
from .document import (
    EditText,
    FreeText,
    Ink,
    Markup,
    Note,
    Shape,
    Stamp,
)
from .render import line_span_at, span_bbox, span_text


# ---------------------------------------------------------------------------
# Edit Text — the flagship tool.
# ---------------------------------------------------------------------------

class EditTextTool:
    name = "edit_text"

    def press(self, canvas: PageCanvas, qt: QPointF) -> None:
        x_pt, y_pt = canvas.coord_map().qt_to_pdf(qt)
        span = line_span_at(canvas.chars(), x_pt, y_pt)
        if not span:
            return
        old = span_text(span)
        new, ok = QInputDialog.getText(
            canvas, "Edit text", f"Replace:",
            QLineEdit.Normal, old,
        )
        if not ok or new == old:
            return
        bbox = span_bbox(span)
        canvas.add_edit(EditText(
            page=canvas.page_index(),
            bbox=bbox,
            old_text=old,
            new_text=new,
            fontname=span[0].fontname,
            fontsize=span[0].fontsize,
        ))

    def move(self, canvas, qt) -> None: pass  # noqa: D401, ANN001
    def release(self, canvas, qt) -> None: pass  # noqa: D401, ANN001


# ---------------------------------------------------------------------------
# Drag-rect base: common helper for tools that care about a drag box.
# ---------------------------------------------------------------------------

class _DragRectTool:
    name = "drag_rect"

    def __init__(self) -> None:
        self._start_pdf: tuple[float, float] | None = None
        self._end_pdf: tuple[float, float] | None = None

    def press(self, canvas: PageCanvas, qt: QPointF) -> None:
        self._start_pdf = canvas.coord_map().qt_to_pdf(qt)
        self._end_pdf = self._start_pdf

    def move(self, canvas: PageCanvas, qt: QPointF) -> None:
        if self._start_pdf is None:
            return
        self._end_pdf = canvas.coord_map().qt_to_pdf(qt)

    def release(self, canvas: PageCanvas, qt: QPointF) -> None:
        if self._start_pdf is None:
            return
        self._end_pdf = canvas.coord_map().qt_to_pdf(qt)
        x0, y0 = self._start_pdf
        x1, y1 = self._end_pdf
        self._start_pdf = None
        self._end_pdf = None
        bbox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if bbox[2] - bbox[0] < 2 or bbox[3] - bbox[1] < 2:
            return
        self._commit(canvas, bbox)

    def _commit(self, canvas: PageCanvas, bbox) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Markup tools (highlight / strikethrough / underline).
# ---------------------------------------------------------------------------

class MarkupTool(_DragRectTool):
    def __init__(self, style: Literal["highlight", "strike", "underline"]) -> None:
        super().__init__()
        self.name = style
        self._style = style

    def _commit(self, canvas: PageCanvas, bbox) -> None:
        color = {"highlight": (255, 230, 0), "strike": (220, 40, 40), "underline": (60, 120, 255)}[self._style]
        canvas.add_edit(Markup(
            page=canvas.page_index(), bbox=bbox, style=self._style, color=color,
        ))


# ---------------------------------------------------------------------------
# Free-text box.
# ---------------------------------------------------------------------------

class FreeTextTool(_DragRectTool):
    name = "freetext"

    def _commit(self, canvas: PageCanvas, bbox) -> None:
        text, ok = QInputDialog.getMultiLineText(
            canvas, "Add text", "Text to add:", "",
        )
        if not ok or not text.strip():
            return
        canvas.add_edit(FreeText(
            page=canvas.page_index(), bbox=bbox, text=text, fontsize=12,
        ))


# ---------------------------------------------------------------------------
# Shape tools.
# ---------------------------------------------------------------------------

class ShapeTool(_DragRectTool):
    def __init__(self, style: Literal["rect", "circle", "line", "arrow"]) -> None:
        super().__init__()
        self.name = style
        self._style = style

    def _commit(self, canvas: PageCanvas, bbox) -> None:
        canvas.add_edit(Shape(
            page=canvas.page_index(), bbox=bbox, style=self._style,
        ))


# ---------------------------------------------------------------------------
# Sticky note — single click.
# ---------------------------------------------------------------------------

class NoteTool:
    name = "note"

    def press(self, canvas: PageCanvas, qt: QPointF) -> None:
        x_pt, y_pt = canvas.coord_map().qt_to_pdf(qt)
        text, ok = QInputDialog.getMultiLineText(
            canvas, "Sticky note", "Comment:", "",
        )
        if not ok or not text.strip():
            return
        canvas.add_edit(Note(
            page=canvas.page_index(), x=x_pt, y=y_pt, text=text,
        ))

    def move(self, canvas, qt) -> None: pass  # noqa: ANN001
    def release(self, canvas, qt) -> None: pass  # noqa: ANN001


# ---------------------------------------------------------------------------
# Ink / freehand drawing.
# ---------------------------------------------------------------------------

class InkTool:
    name = "ink"

    def __init__(self) -> None:
        self._points: list[tuple[float, float]] = []

    def press(self, canvas: PageCanvas, qt: QPointF) -> None:
        self._points = [canvas.coord_map().qt_to_pdf(qt)]

    def move(self, canvas: PageCanvas, qt: QPointF) -> None:
        if not self._points:
            return
        self._points.append(canvas.coord_map().qt_to_pdf(qt))
        # Live preview: temporarily draw onto the scene as we go.
        # Simpler: just add a partial ink edit and refresh. Instead, we
        # commit on release to avoid churning the overlay layer.

    def release(self, canvas: PageCanvas, qt: QPointF) -> None:
        if len(self._points) < 2:
            self._points = []
            return
        pts = list(self._points)
        self._points = []
        canvas.add_edit(Ink(
            page=canvas.page_index(), points=pts,
        ))


# ---------------------------------------------------------------------------
# Image stamp — file picker, then drag-rect to place.
# ---------------------------------------------------------------------------

class StampTool(_DragRectTool):
    name = "stamp"

    def __init__(self) -> None:
        super().__init__()
        self._image_path: Path | None = None

    def prime(self, canvas: PageCanvas) -> bool:
        """Ask for the image to stamp. Called by the main window before
        the tool becomes active."""
        path, _ = QFileDialog.getOpenFileName(
            canvas, "Pick stamp image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp);;All files (*)",
        )
        if not path:
            return False
        self._image_path = Path(path)
        return True

    def _commit(self, canvas: PageCanvas, bbox) -> None:
        if self._image_path is None:
            return
        canvas.add_edit(Stamp(
            page=canvas.page_index(), bbox=bbox, image_path=self._image_path,
        ))
