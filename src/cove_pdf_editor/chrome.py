"""Custom window chrome — Windows-style titlebar.

Matches the cove-gif-maker / cove-nexus pattern: small icon + centered
title, three Windows-style controls flush right (minimise, maximise,
close). The whole bar is the drag region; buttons sit in no-drag zones.
Edge resizing is driven by FramelessResizer.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from . import theme


_TITLEBAR_HEIGHT = 36
_RESIZE_MARGIN = 6
_BTN_W = 46


def _hidpi_pixmap(path: str, size: int, widget: QWidget) -> QPixmap:
    dpr = float(widget.devicePixelRatioF()) if widget is not None else 1.0
    if dpr <= 0:
        dpr = 1.0
    actual = max(1, int(round(size * dpr)))
    pix = QPixmap(path).scaled(
        actual, actual, Qt.KeepAspectRatio, Qt.SmoothTransformation,
    )
    pix.setDevicePixelRatio(dpr)
    return pix


class _WinButton(QPushButton):
    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind
        self.setObjectName(f"winbtn-{kind}")
        self.setFixedSize(_BTN_W, _TITLEBAR_HEIGHT)
        self.setCursor(Qt.ArrowCursor)
        self.setFocusPolicy(Qt.NoFocus)
        if kind == "close":
            self.setStyleSheet(
                f"QPushButton#winbtn-{kind} {{"
                f" background: transparent; border: none; padding: 0; }}"
                f"QPushButton#winbtn-{kind}:hover {{ background: #e81123; }}"
            )
        else:
            self.setStyleSheet(
                f"QPushButton#winbtn-{kind} {{"
                f" background: transparent; border: none; padding: 0; }}"
                f"QPushButton#winbtn-{kind}:hover {{"
                f" background: rgba(255,255,255,0.06); }}"
            )

    def paintEvent(self, event) -> None:  # noqa: ANN001
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        if self._kind == "close" and self.underMouse():
            color = QColor("#ffffff")
        elif self.underMouse():
            color = QColor(theme.TEXT)
        else:
            color = QColor(theme.TEXT_DIM)
        pen = p.pen()
        pen.setColor(color)
        pen.setWidth(1)
        p.setPen(pen)
        cx = self.width() / 2
        cy = self.height() / 2
        s = 5
        if self._kind == "min":
            p.drawLine(int(cx - s), int(cy), int(cx + s), int(cy))
        elif self._kind == "max":
            p.drawRect(int(cx - s), int(cy - s), int(s * 2), int(s * 2))
        elif self._kind == "close":
            p.drawLine(int(cx - s), int(cy - s), int(cx + s), int(cy + s))
            p.drawLine(int(cx - s), int(cy + s), int(cx + s), int(cy - s))
        p.end()


class CoveTitleBar(QWidget):
    def __init__(self, window: QWidget, *, icon_path: str | None = None,
                 title: str = "", version: str = "") -> None:
        super().__init__(window)
        self._window = window
        self._fallback_offset: QPoint | None = None
        self.setFixedHeight(_TITLEBAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setObjectName("cove-titlebar")
        self.setStyleSheet(
            f"QWidget#cove-titlebar {{"
            f"  background: {theme.BG};"
            f"  border-bottom: 1px solid {theme.BORDER};"
            f"}}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if icon_path:
            self._icon_path = icon_path
            self._brand = QLabel()
            self._brand.setFixedSize(16, 16)
            self._brand.setAttribute(Qt.WA_TransparentForMouseEvents)
            self._brand.setPixmap(_hidpi_pixmap(icon_path, 16, self._brand))
            self._brand.setStyleSheet("background: transparent;")
            self._brand.setParent(self)
            self._brand.move(14, (_TITLEBAR_HEIGHT - 16) // 2)
        else:
            self._icon_path = None
            self._brand = None

        layout.addStretch(1)
        self._btn_min = _WinButton("min", self)
        self._btn_max = _WinButton("max", self)
        self._btn_close = _WinButton("close", self)
        self._btn_min.clicked.connect(self._on_minimize)
        self._btn_max.clicked.connect(self._on_max_restore)
        self._btn_close.clicked.connect(self._window.close)
        layout.addWidget(self._btn_min)
        layout.addWidget(self._btn_max)
        layout.addWidget(self._btn_close)

        self._title_block = QWidget(self)
        self._title_block.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._title_block.setStyleSheet("background: transparent;")
        title_layout = QHBoxLayout(self._title_block)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(8)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12px; font-weight: 500;"
            f" background: transparent;"
        )
        title_layout.addWidget(title_lbl)
        if version:
            ver_lbl = QLabel(version)
            ver_lbl.setStyleSheet(
                f"color: {theme.TEXT_FAINT}; font-size: 10.5px;"
                f" font-family: '{theme.FONT_MONO}', monospace;"
                f" background: transparent;"
            )
            title_layout.addWidget(ver_lbl)
        self._title_block.adjustSize()

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        block = self._title_block
        block.adjustSize()
        x = (self.width() - block.width()) // 2
        y = (self.height() - block.height()) // 2
        block.move(x, y)
        block.raise_()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if self._brand is not None and self._icon_path:
            self._brand.setPixmap(_hidpi_pixmap(self._icon_path, 16, self._brand))

    def _on_minimize(self) -> None:
        self._window.showMinimized()

    def _on_max_restore(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def _hits_window_button(self, pos: QPoint) -> bool:
        for btn in (self._btn_min, self._btn_max, self._btn_close):
            if btn.geometry().contains(pos):
                return True
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (event.button() == Qt.LeftButton
                and not self._hits_window_button(event.position().toPoint())):
            handle = self._window.windowHandle()
            if handle is not None and hasattr(handle, "startSystemMove"):
                if handle.startSystemMove():
                    event.accept()
                    return
            self._fallback_offset = (
                event.globalPosition().toPoint()
                - self._window.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._fallback_offset and event.buttons() & Qt.LeftButton:
            if self._window.isMaximized():
                self._window.showNormal()
                self._fallback_offset = QPoint(
                    self.width() // 2, _TITLEBAR_HEIGHT // 2,
                )
            self._window.move(
                event.globalPosition().toPoint() - self._fallback_offset
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._fallback_offset is not None:
            self._fallback_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if (event.button() == Qt.LeftButton
                and not self._hits_window_button(event.position().toPoint())):
            self._on_max_restore()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class FramelessResizer:
    _CURSORS = {
        "l":  Qt.SizeHorCursor, "r":  Qt.SizeHorCursor,
        "t":  Qt.SizeVerCursor, "b":  Qt.SizeVerCursor,
        "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
        "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
    }

    def __init__(self, window) -> None:  # noqa: ANN001
        self._w = window
        self._resizing_edge: str | None = None
        self._press_global: QPoint | None = None
        self._press_geom: QRect | None = None
        self._hover_edge: str | None = None

    def try_press(self, event) -> bool:  # noqa: ANN001
        if event.button() != Qt.LeftButton:
            return False
        edge = self._edge_for(event.position().toPoint())
        if not edge:
            return False
        self._resizing_edge = edge
        self._press_global = event.globalPosition().toPoint()
        self._press_geom = self._w.geometry()
        return True

    def try_move(self, event) -> bool:  # noqa: ANN001
        if not (event.buttons() & Qt.LeftButton):
            self._update_cursor(event.position().toPoint())
            return False
        if not self._resizing_edge:
            return False
        self._do_resize(event.globalPosition().toPoint())
        return True

    def try_release(self, _event) -> bool:  # noqa: ANN001
        if not self._resizing_edge:
            return False
        self._resizing_edge = None
        self._press_global = None
        self._press_geom = None
        self.clear_hover()
        return True

    def clear_hover(self) -> None:
        if self._hover_edge is not None:
            QGuiApplication.restoreOverrideCursor()
            self._hover_edge = None

    def _edge_for(self, pos: QPoint) -> str | None:
        if self._w.isMaximized():
            return None
        m = _RESIZE_MARGIN
        w, h = self._w.width(), self._w.height()
        x, y = pos.x(), pos.y()
        on_l = x <= m
        on_r = x >= w - m
        on_t = y <= m
        on_b = y >= h - m
        if on_t and on_l: return "tl"
        if on_t and on_r: return "tr"
        if on_b and on_l: return "bl"
        if on_b and on_r: return "br"
        if on_l: return "l"
        if on_r: return "r"
        if on_t: return "t"
        if on_b: return "b"
        return None

    def _update_cursor(self, pos: QPoint) -> None:
        edge = self._edge_for(pos)
        if edge == self._hover_edge:
            return
        if self._hover_edge is not None:
            QGuiApplication.restoreOverrideCursor()
        if edge is not None:
            QGuiApplication.setOverrideCursor(self._CURSORS[edge])
        self._hover_edge = edge

    def _do_resize(self, global_pos: QPoint) -> None:
        if not (self._press_global and self._press_geom and self._resizing_edge):
            return
        dx = global_pos.x() - self._press_global.x()
        dy = global_pos.y() - self._press_global.y()
        g = QRect(self._press_geom)
        edge = self._resizing_edge
        min_w = max(self._w.minimumSize().width(), 720)
        min_h = max(self._w.minimumSize().height(), 480)
        if "l" in edge:
            new_x = min(g.x() + dx, g.right() - min_w)
            g.setLeft(new_x)
        if "r" in edge:
            g.setRight(g.right() + dx)
        if "t" in edge:
            new_y = min(g.y() + dy, g.bottom() - min_h)
            g.setTop(new_y)
        if "b" in edge:
            g.setBottom(g.bottom() + dy)
        self._w.setGeometry(g)
