"""Bake a Document's edits into a saved PDF.

PyMuPDF opens the source PDF, draws each pending edit directly onto the
matching page (whiteout + replacement glyphs for ``EditText``; positioned
text for ``FreeText``; placed bitmap for ``ImageEdit``), and writes the
result. One library, one pass, no overlay+merge dance.

Edits are baked into the page content stream. The saved PDF carries no
annotations, sticky notes, ink, shapes, signatures, form-field updates,
bookmarks, hyperlinks, watermarks, or headers/footers — the editor never
produces those.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf

from .document import Document, EditText, FreeText, ImageEdit


def save(doc: Document, out: Path) -> Path:
    """Render all pending edits into a new PDF at ``out``.

    Per page: queue a PDF redaction for each ``EditText`` (so the original
    glyphs are removed from the content stream, not just visually
    covered), apply the redactions, then draw replacement text + any
    ``FreeText`` / ``ImageEdit`` on top.
    """
    if not doc.edits:
        # No work to do; copy bytes verbatim so the saved file is byte-exact.
        out.write_bytes(doc.source.read_bytes())
        return out
    with pymupdf.open(str(doc.source)) as pdf:
        for page_idx in range(doc.page_count):
            page = pdf[page_idx]
            page_edits = doc.edits_for_page(page_idx)
            redacted = False
            for edit in page_edits:
                if isinstance(edit, EditText):
                    _queue_redaction(page, edit)
                    redacted = True
            if redacted:
                # Remove the underlying text and stamp a white rect; leave
                # images and vector graphics alone (`images=0`,
                # `graphics=0`).
                page.apply_redactions(images=0, graphics=0)
            # Whiteout the original location of any image promoted from
            # the source PDF, so the moved/resized/deleted version isn't
            # ghosted by the baked-in original.
            for edit in page_edits:
                if isinstance(edit, ImageEdit) and edit.original_bbox is not None:
                    rect = _pdf_rect(page, edit.original_bbox)
                    # Slight outward pad so antialiased edges of the
                    # baked-in original image don't peek out.
                    pad = 1.5
                    rect = pymupdf.Rect(rect.x0 - pad, rect.y0 - pad,
                                        rect.x1 + pad, rect.y1 + pad)
                    page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1), width=0)
            for edit in page_edits:
                _draw(page, edit)
        pdf.save(str(out), garbage=4, deflate=True)
    return out


def _queue_redaction(page: pymupdf.Page, edit: EditText) -> None:
    # Always redact the source area (original_bbox); bbox may have moved.
    bbox = edit.original_bbox or edit.bbox
    rect = _pdf_rect(page, bbox)
    pad = 0.5
    page.add_redact_annot(
        pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad),
        fill=(1, 1, 1),
        cross_out=False,
    )


def _draw(page: pymupdf.Page, edit) -> None:
    if isinstance(edit, EditText):
        _draw_edit_text(page, edit)
    elif isinstance(edit, FreeText):
        _draw_freetext(page, edit)
    elif isinstance(edit, ImageEdit):
        _draw_image(page, edit)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _draw_edit_text(page: pymupdf.Page, edit: EditText) -> None:
    """Draw the replacement text. The whiteout was already done by
    ``apply_redactions``; this just inserts new glyphs in the same bbox,
    shrinking the size to fit if needed."""
    rect = _pdf_rect(page, edit.bbox)
    fontname = _resolve_font(edit.fontname, bold=edit.bold, italic=edit.italic)
    size = edit.fontsize
    while size > 6 and pymupdf.get_text_length(
        edit.new_text, fontsize=size, fontname=fontname,
    ) > rect.width:
        size -= 0.5
    baseline = pymupdf.Point(rect.x0, rect.y1 - size * 0.2)
    page.insert_text(
        baseline, edit.new_text,
        fontsize=size, fontname=fontname, color=_to_float(edit.color),
    )


def _draw_freetext(page: pymupdf.Page, edit: FreeText) -> None:
    """Draw the FreeText box's lines with explicit alignment + optional
    underline. Word-wraps to the bbox width so saved output matches the
    on-canvas editor (which uses ``setTextWidth``). Each line is
    positioned manually so the underline width can match the actual
    rendered text run."""
    rect = _pdf_rect(page, edit.bbox)
    fontname = _resolve_font(edit.fontname, bold=edit.bold, italic=edit.italic)
    color = _to_float(edit.color)
    line_h = edit.fontsize * 1.2
    underline_dy = edit.fontsize * 0.15
    underline_w = max(0.5, edit.fontsize * 0.06)
    lines = _wrap_lines(edit.text, rect.width, edit.fontsize, fontname)
    for i, line in enumerate(lines):
        baseline_y = rect.y0 + edit.fontsize + line_h * i
        text_w = pymupdf.get_text_length(line, fontsize=edit.fontsize, fontname=fontname)
        if edit.align == "center":
            cx = (rect.x0 + rect.x1) / 2
            x = cx - text_w / 2
        elif edit.align == "right":
            x = rect.x1 - text_w
        else:
            x = rect.x0
        page.insert_text(
            pymupdf.Point(x, baseline_y), line,
            fontsize=edit.fontsize, fontname=fontname, color=color,
        )
        if edit.underline and line:
            yu = baseline_y + underline_dy
            page.draw_line(
                pymupdf.Point(x, yu), pymupdf.Point(x + text_w, yu),
                color=color, width=underline_w,
            )


def _wrap_lines(text: str, max_width: float, fontsize: float, fontname: str) -> list[str]:
    """Word-wrap ``text`` to lines that fit within ``max_width``. Explicit
    newlines start new paragraphs; words within a paragraph wrap at
    spaces. A single word longer than ``max_width`` is kept on its own
    line and overflows — matching what Qt's ``setTextWidth`` does."""
    out: list[str] = []
    space_w = pymupdf.get_text_length(" ", fontsize=fontsize, fontname=fontname)
    for paragraph in text.split("\n"):
        if not paragraph:
            out.append("")
            continue
        current: list[str] = []
        current_w = 0.0
        for word in paragraph.split(" "):
            word_w = pymupdf.get_text_length(word, fontsize=fontsize, fontname=fontname)
            if not current:
                current = [word]
                current_w = word_w
            elif current_w + space_w + word_w <= max_width:
                current.append(word)
                current_w += space_w + word_w
            else:
                out.append(" ".join(current))
                current = [word]
                current_w = word_w
        out.append(" ".join(current))
    return out


def _draw_image(page: pymupdf.Page, edit: ImageEdit) -> None:
    """Place the bitmap stretched to fill the bbox so the saved output
    matches the on-canvas preview, which uses the same bbox without
    aspect-ratio preservation. ``image_path is None`` is a tombstone for
    a promoted source image the user deleted — the whiteout in
    ``save()`` already covered the original; nothing else to draw."""
    if edit.image_path is None:
        return
    rect = _pdf_rect(page, edit.bbox)
    try:
        page.insert_image(rect, filename=str(edit.image_path), keep_proportion=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pdf_rect(page: pymupdf.Page, bbox: tuple[float, float, float, float]) -> pymupdf.Rect:
    """Convert (x0, y0, x1, y1) PDF coords (bottom-left origin) into a
    ``pymupdf.Rect`` (top-left origin). Edits in :mod:`document` use PDF
    convention; PyMuPDF uses MuPDF convention."""
    page_h = page.rect.height
    x0, y0, x1, y1 = bbox
    return pymupdf.Rect(x0, page_h - y1, x1, page_h - y0)


def _to_float(color: tuple[int, int, int]) -> tuple[float, float, float]:
    return tuple(c / 255 for c in color)


def _resolve_font(name: str, bold: bool = False, italic: bool = False) -> str:
    """Pick a base-14 font name honoring explicit bold/italic flags and any
    flags hinted by the source font name."""
    clean = name.split("+")[-1] if "+" in name else name
    lower = clean.lower()
    if "courier" in lower or "mono" in lower:
        family = "Courier"
    elif "times" in lower or "serif" in lower:
        family = "Times"
    else:
        family = "Helvetica"
    if "bold" in lower or "black" in lower or "heavy" in lower:
        bold = True
    if "italic" in lower or "oblique" in lower:
        italic = True
    if family == "Times":
        return ("Times-BoldItalic" if bold and italic
                else "Times-Bold" if bold
                else "Times-Italic" if italic
                else "Times-Roman")
    if family == "Courier":
        return ("Courier-BoldOblique" if bold and italic
                else "Courier-Bold" if bold
                else "Courier-Oblique" if italic
                else "Courier")
    return ("Helvetica-BoldOblique" if bold and italic
            else "Helvetica-Bold" if bold
            else "Helvetica-Oblique" if italic
            else "Helvetica")


def export_pages(doc: Document, pages: list[int], out: Path) -> None:
    """Export specific pages (0-based indices) with all edits baked in."""
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".pdf")
    tmp_path = Path(tmp_name)
    try:
        import os
        os.close(tmp_fd)
        save(doc, tmp_path)
        pdf = pymupdf.open(str(tmp_path))
        pdf.select(pages)
        pdf.save(str(out), garbage=4, deflate=True)
        pdf.close()
    finally:
        tmp_path.unlink(missing_ok=True)
