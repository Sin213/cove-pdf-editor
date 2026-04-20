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
    QImage,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsItemGroup,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
)

from .document import Document
from .render import PageChar, extract_chars, page_info, render_page


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
        pen = QPen(QColor(100, 180, 255))
        pen.setWidth(1)
        pen.setStyle(Qt.DashLine)
        if isinstance(edit, (EditText, FreeText, Shape, Stamp, Markup)):
            rect = cm.pdf_rect_to_qt(*edit.bbox)
            item = self._scene.addRect(rect, pen)
            self._overlay_group.addToGroup(item)
            if isinstance(edit, EditText):
                # Draw a translucent yellow fill so the user sees the edit mark.
                fill = self._scene.addRect(rect, QPen(Qt.NoPen), QBrush(QColor(255, 240, 120, 80)))
                self._overlay_group.addToGroup(fill)
            elif isinstance(edit, Markup):
                color = QColor(*edit.color, 110)
                fill = self._scene.addRect(rect, QPen(Qt.NoPen), QBrush(color))
                self._overlay_group.addToGroup(fill)
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

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        if self._scene.sceneRect().width() > 0:
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
