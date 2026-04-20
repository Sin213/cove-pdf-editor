"""Page rendering + text extraction.

pypdfium2 does the rendering; pdfplumber does the positional text
extraction for Edit Text + text-markup tools. Everything runs on the
main thread for simplicity (most PDFs render a page in <100ms at
screen scale). If that proves slow for large pages we can move
rendering to a worker later.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image
from PySide6.QtGui import QImage


@dataclass(frozen=True)
class PageChar:
    text: str
    x0: float
    y0: float       # from page bottom (PDF convention)
    x1: float
    y1: float
    fontname: str
    fontsize: float


@dataclass(frozen=True)
class PageInfo:
    width: float    # points
    height: float   # points


def page_info(source: Path, page_index: int) -> PageInfo:
    with pdfium.PdfDocument(str(source)) as doc:
        page = doc[page_index]
        return PageInfo(width=page.get_width(), height=page.get_height())


def render_page(source: Path, page_index: int, scale: float = 2.0) -> QImage:
    with pdfium.PdfDocument(str(source)) as doc:
        page = doc[page_index]
        pil = page.render(scale=scale).to_pil().convert("RGB")
        return _pil_to_qimage(pil)


def extract_chars(source: Path, page_index: int) -> list[PageChar]:
    """Per-character bounding box info. Coordinates in PDF points,
    origin bottom-left."""
    out: list[PageChar] = []
    with pdfplumber.open(source) as pdf:
        page = pdf.pages[page_index]
        for c in page.chars:
            out.append(PageChar(
                text=c.get("text", ""),
                x0=float(c.get("x0", 0)),
                y0=float(c.get("y0", 0)),
                x1=float(c.get("x1", 0)),
                y1=float(c.get("y1", 0)),
                fontname=str(c.get("fontname", "Helvetica")),
                fontsize=float(c.get("size", 11.0)),
            ))
    return out


def word_span_at(chars: list[PageChar], x: float, y: float) -> list[PageChar] | None:
    """Given PDF-space coords (points, bottom-left origin), return the
    list of chars forming the word or contiguous text run under that point.
    We expand left/right as long as the next char is on the same baseline
    and separated by less than one space-width.
    """
    # Find the char whose bbox contains (x, y). Note y0 is bottom.
    hit = None
    for i, c in enumerate(chars):
        if c.x0 <= x <= c.x1 and c.y0 <= y <= c.y1:
            hit = i
            break
    if hit is None:
        return None
    # Walk left and right to build a run on the same baseline. We treat
    # anything on roughly the same y0 (within half font size) as part of
    # the same line; we break on whitespace if the gap is big.
    line_tol = max(1.0, chars[hit].fontsize * 0.4)
    # Sort by x0 on the same line to find neighbors
    line = [c for c in chars if abs(c.y0 - chars[hit].y0) <= line_tol]
    line.sort(key=lambda c: c.x0)
    # locate hit in sorted line
    hit_ref = chars[hit]
    try:
        hit_idx = line.index(hit_ref)
    except ValueError:
        return [hit_ref]
    # Walk left / right stopping when gap > half-em (treat as word boundary)
    gap_tol = hit_ref.fontsize * 0.4
    start = hit_idx
    while start > 0:
        prev = line[start - 1]
        curr = line[start]
        if curr.x0 - prev.x1 > gap_tol:
            break
        start -= 1
    end = hit_idx
    while end < len(line) - 1:
        curr = line[end]
        nxt = line[end + 1]
        if nxt.x0 - curr.x1 > gap_tol:
            break
        end += 1
    return line[start:end + 1]


def line_span_at(chars: list[PageChar], x: float, y: float) -> list[PageChar] | None:
    """Full line under the point (ignores word boundaries). Good for
    Edit Text where you usually want to edit a whole label like
    ``TOTAL DUE: $1,000.00``."""
    line_tol = 6.0  # points
    # Find any char on the clicked line.
    on_line = [c for c in chars if c.y0 - line_tol <= y <= c.y1 + line_tol]
    if not on_line:
        return None
    on_line.sort(key=lambda c: c.x0)
    # Group into contiguous runs on this line; return the run containing x.
    runs: list[list[PageChar]] = []
    current: list[PageChar] = []
    prev: PageChar | None = None
    for c in on_line:
        gap_tol = c.fontsize * 2.0
        if prev is not None and c.x0 - prev.x1 > gap_tol:
            runs.append(current)
            current = []
        current.append(c)
        prev = c
    if current:
        runs.append(current)
    for run in runs:
        if run[0].x0 - 2.0 <= x <= run[-1].x1 + 2.0:
            return run
    return None


def span_bbox(span: list[PageChar]) -> tuple[float, float, float, float]:
    x0 = min(c.x0 for c in span)
    y0 = min(c.y0 for c in span)
    x1 = max(c.x1 for c in span)
    y1 = max(c.y1 for c in span)
    return x0, y0, x1, y1


def span_text(span: list[PageChar]) -> str:
    return "".join(c.text for c in span)


def _pil_to_qimage(img: Image.Image) -> QImage:
    img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    qi = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
    return qi.copy()
