"""Page rendering + text-span extraction.

Rendering goes through pypdfium2 for the bitmap. Searchable text spans
come from PyMuPDF, which gives us a per-span bbox + font + size + flags
in one pass — exactly what double-click text editing needs.

If the PDF is image-only (a scan with no extractable text layer),
``extract_spans`` simply returns an empty list and clicks fall through
to a "no editable text here" message at the tool layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pypdfium2 as pdfium
from PIL import Image
from PySide6.QtGui import QImage


# PyMuPDF font flag bits (matches mupdf docs).
_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4


@dataclass(frozen=True)
class PageSpan:
    text: str
    bbox: tuple[float, float, float, float]  # PDF points, bottom-left origin
    fontname: str
    fontsize: float
    color: tuple[int, int, int]
    bold: bool
    italic: bool


@dataclass(frozen=True)
class PageInfo:
    width: float    # points
    height: float   # points


@dataclass(frozen=True)
class PageImage:
    """An image XObject placed on a source PDF page. Bbox in PDF points
    (bottom-left origin). ``image_bytes`` holds the raw image file
    contents (PNG / JPEG / etc.) so we can write it to a temp file and
    treat it like any other inserted image."""
    bbox: tuple[float, float, float, float]
    xref: int
    image_bytes: bytes
    ext: str


def page_info(source: Path, page_index: int) -> PageInfo:
    with pdfium.PdfDocument(str(source)) as doc:
        page = doc[page_index]
        return PageInfo(width=page.get_width(), height=page.get_height())


def render_page(source: Path, page_index: int, scale: float = 2.0) -> QImage:
    with pdfium.PdfDocument(str(source)) as doc:
        page = doc[page_index]
        pil = page.render(scale=scale).to_pil().convert("RGB")
        return _pil_to_qimage(pil)


def extract_spans(source: Path, page_index: int) -> list[PageSpan]:
    """Per-span text + style info, with bboxes in PDF points (bottom-left
    origin) so they line up with everything else in :class:`Document`."""
    out: list[PageSpan] = []
    with pymupdf.open(str(source)) as doc:
        page = doc[page_index]
        page_h = page.rect.height
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:  # 0 = text block, 1 = image
                continue
            for line in block.get("lines", []):
                for s in line.get("spans", []):
                    text = s.get("text", "")
                    if not text.strip():
                        continue  # whitespace-only spans — nothing to edit
                    x0, y_top, x1, y_bot = s["bbox"]
                    # MuPDF bbox is top-left origin; flip to PDF convention.
                    bbox = (x0, page_h - y_bot, x1, page_h - y_top)
                    color_int = int(s.get("color", 0))
                    color = (
                        (color_int >> 16) & 0xFF,
                        (color_int >> 8) & 0xFF,
                        color_int & 0xFF,
                    )
                    flags = int(s.get("flags", 0))
                    out.append(PageSpan(
                        text=text,
                        bbox=bbox,
                        fontname=str(s.get("font", "Helvetica")),
                        fontsize=float(s.get("size", 11.0)),
                        color=color,
                        bold=bool(flags & _FLAG_BOLD),
                        italic=bool(flags & _FLAG_ITALIC),
                    ))
    return out


def span_at(spans: list[PageSpan], x: float, y: float) -> PageSpan | None:
    """Return the span whose bbox contains the PDF-space point, or None."""
    for span in spans:
        x0, y0, x1, y1 = span.bbox
        if x0 <= x <= x1 and y0 <= y <= y1:
            return span
    return None


def extract_images(source: Path, page_index: int) -> list[PageImage]:
    """Per-image XObject info on the page: bbox in PDF points, xref, and
    the raw image bytes. Used to make existing PDF images promotable
    into editable :class:`document.ImageEdit` objects."""
    out: list[PageImage] = []
    seen_xrefs: dict[int, dict] = {}
    with pymupdf.open(str(source)) as doc:
        page = doc[page_index]
        page_h = page.rect.height
        for entry in page.get_images(full=True):
            xref = entry[0]
            if xref not in seen_xrefs:
                try:
                    seen_xrefs[xref] = doc.extract_image(xref)
                except Exception:
                    continue
            data = seen_xrefs[xref]
            for rect in page.get_image_rects(xref):
                # MuPDF rect → PDF coords (bottom-left origin).
                bbox = (rect.x0, page_h - rect.y1, rect.x1, page_h - rect.y0)
                out.append(PageImage(
                    bbox=bbox,
                    xref=xref,
                    image_bytes=data.get("image", b""),
                    ext=str(data.get("ext", "png")),
                ))
    return out


def image_at(images: list[PageImage], x: float, y: float) -> PageImage | None:
    """Return the topmost image whose bbox contains the PDF-space point.
    Topmost = last one in extraction order, which is render order."""
    hit: PageImage | None = None
    for img in images:
        x0, y0, x1, y1 = img.bbox
        if x0 <= x <= x1 and y0 <= y <= y1:
            hit = img
    return hit


def _pil_to_qimage(img: Image.Image) -> QImage:
    img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    qi = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
    return qi.copy()
