"""Dialogs that don't fit cleanly into a tool: signature canvas, form
fill panel, header/footer editor, watermark editor, bookmark/hyperlink.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image, ImageDraw
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .document import FormFill, HeaderFooter, Hyperlink, Watermark
from .overlay import list_form_fields


# ---------------------------------------------------------------------------
# Signature canvas: draw with mouse, returns a PNG file path when accepted.
# ---------------------------------------------------------------------------

class SignatureCanvas(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(500, 200)
        self._pix = QPixmap(self.size())
        self._pix.fill(Qt.white)
        self._last: QPointF | None = None
        self._strokes: list[list[QPointF]] = []
        self._current: list[QPointF] = []

    def clear(self) -> None:
        self._pix.fill(Qt.white)
        self._strokes.clear()
        self._current.clear()
        self.update()

    def is_empty(self) -> bool:
        return not self._strokes

    def save_png(self, path: Path) -> None:
        # Re-render at higher resolution for sharpness.
        scale = 4
        img = Image.new("RGBA", (self.width() * scale, self.height() * scale), (255, 255, 255, 0))
        draw = ImageDraw.Draw(img)
        for stroke in self._strokes:
            if len(stroke) < 2:
                continue
            pts = [(p.x() * scale, p.y() * scale) for p in stroke]
            draw.line(pts, fill=(0, 0, 0, 255), width=int(2.5 * scale), joint="curve")
        img.save(path)

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pix)
        pen = QPen(QColor(0, 0, 0))
        pen.setWidthF(2.5)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setRenderHint(QPainter.Antialiasing)
        for stroke in [*self._strokes, self._current]:
            for i in range(1, len(stroke)):
                painter.drawLine(stroke[i - 1], stroke[i])

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.LeftButton:
            self._current = [QPointF(event.position())]

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if event.buttons() & Qt.LeftButton:
            self._current.append(QPointF(event.position()))
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.LeftButton and self._current:
            self._strokes.append(self._current)
            self._current = []
            self.update()


def prompt_signature(parent: QWidget) -> Path | None:
    dlg = QDialog(parent)
    dlg.setWindowTitle("Draw signature")
    layout = QVBoxLayout(dlg)
    info = QLabel("Draw your signature below, then click Use.")
    info.setStyleSheet("color:#7a8294;")
    layout.addWidget(info)
    canvas = SignatureCanvas()
    canvas.setStyleSheet("border:1px solid #2a2f3a; background:white;")
    layout.addWidget(canvas)
    row = QHBoxLayout()
    clear_btn = QPushButton("Clear")
    clear_btn.clicked.connect(canvas.clear)
    row.addWidget(clear_btn)
    row.addStretch(1)
    btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    btns.button(QDialogButtonBox.Ok).setText("Use")
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    row.addWidget(btns)
    layout.addLayout(row)
    if dlg.exec() != QDialog.Accepted or canvas.is_empty():
        return None
    tmp = Path(tempfile.mkstemp(prefix="cove-sig-", suffix=".png")[1])
    canvas.save_png(tmp)
    return tmp


# ---------------------------------------------------------------------------
# Form fill panel.
# ---------------------------------------------------------------------------

class FormFillDialog(QDialog):
    def __init__(self, source: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fill form fields")
        self.setMinimumWidth(500)
        self._fields = list_form_fields(source)
        self._widgets: dict[str, QWidget] = {}
        self.result_fills: list[FormFill] = []
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        if not self._fields:
            root.addWidget(QLabel("This PDF has no AcroForm fields."))
            btns = QDialogButtonBox(QDialogButtonBox.Close)
            btns.rejected.connect(self.reject)
            btns.accepted.connect(self.reject)
            root.addWidget(btns)
            return

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        for field in self._fields:
            name = field["name"]
            ft = field["type"]
            if ft == "Btn":
                w: QWidget = QCheckBox()
                w.setChecked(field["value"].lower() in ("yes", "on", "true", "1"))
            else:
                w = QLineEdit()
                w.setText(field["value"])
            self._widgets[name] = w
            form.addRow(name, w)
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _on_accept(self) -> None:
        for name, widget in self._widgets.items():
            if isinstance(widget, QCheckBox):
                self.result_fills.append(FormFill(field_name=name, value=widget.isChecked()))
            elif isinstance(widget, QLineEdit):
                if widget.text():
                    self.result_fills.append(FormFill(field_name=name, value=widget.text()))
        self.accept()


# ---------------------------------------------------------------------------
# Header / footer / watermark editors.
# ---------------------------------------------------------------------------

class HeaderFooterDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add header / footer")
        self.result_edit: HeaderFooter | None = None
        root = QFormLayout(self)
        self.text_edit = QLineEdit()
        self.pos_combo = QComboBox()
        for p in ["header-left", "header-center", "header-right",
                  "footer-left", "footer-center", "footer-right"]:
            self.pos_combo.addItem(p)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(6, 36)
        self.size_spin.setValue(10)
        self.pages_edit = QLineEdit()
        self.pages_edit.setText("all")
        self.pages_edit.setPlaceholderText("all, or 1,3,5-7")
        root.addRow("Text", self.text_edit)
        root.addRow("Position", self.pos_combo)
        root.addRow("Size", self.size_spin)
        root.addRow("Pages", self.pages_edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addRow(btns)

    def _on_accept(self) -> None:
        text = self.text_edit.text().strip()
        if not text:
            self.reject()
            return
        self.result_edit = HeaderFooter(
            text=text,
            position=self.pos_combo.currentText(),
            fontsize=self.size_spin.value(),
            pages=self.pages_edit.text() or "all",
        )
        self.accept()


class WatermarkDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add watermark")
        self.result_edit: Watermark | None = None
        root = QFormLayout(self)
        self.text_edit = QLineEdit()
        self.text_edit.setText("CONFIDENTIAL")
        self.size_spin = QSpinBox()
        self.size_spin.setRange(12, 200)
        self.size_spin.setValue(72)
        self.rot_spin = QSpinBox()
        self.rot_spin.setRange(-90, 90)
        self.rot_spin.setValue(45)
        self.opacity_spin = QSpinBox()
        self.opacity_spin.setRange(5, 100)
        self.opacity_spin.setSuffix(" %")
        self.opacity_spin.setValue(30)
        self.pages_edit = QLineEdit()
        self.pages_edit.setText("all")
        root.addRow("Text", self.text_edit)
        root.addRow("Size", self.size_spin)
        root.addRow("Rotation", self.rot_spin)
        root.addRow("Opacity", self.opacity_spin)
        root.addRow("Pages", self.pages_edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addRow(btns)

    def _on_accept(self) -> None:
        text = self.text_edit.text().strip()
        if not text:
            self.reject()
            return
        self.result_edit = Watermark(
            text=text,
            fontsize=self.size_spin.value(),
            rotation=self.rot_spin.value(),
            opacity=self.opacity_spin.value() / 100.0,
            pages=self.pages_edit.text() or "all",
        )
        self.accept()


class HyperlinkDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add hyperlink")
        self.result_uri: str | None = None
        root = QFormLayout(self)
        self.uri_edit = QLineEdit()
        self.uri_edit.setPlaceholderText("https://example.com")
        root.addRow("URL", self.uri_edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addRow(btns)

    def _on_accept(self) -> None:
        uri = self.uri_edit.text().strip()
        if not uri:
            self.reject()
            return
        self.result_uri = uri
        self.accept()


class BookmarkDialog(QDialog):
    def __init__(self, current_page: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add bookmark")
        self.result_title: str | None = None
        self.result_page: int = current_page
        root = QFormLayout(self)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Bookmark title")
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 9999)
        self.page_spin.setValue(current_page + 1)
        root.addRow("Title", self.title_edit)
        root.addRow("Page", self.page_spin)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addRow(btns)

    def _on_accept(self) -> None:
        t = self.title_edit.text().strip()
        if not t:
            self.reject()
            return
        self.result_title = t
        self.result_page = self.page_spin.value() - 1
        self.accept()
