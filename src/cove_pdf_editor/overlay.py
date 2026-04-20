"""Turn a :class:`Document` into a saved PDF.

Two layers of work:

1. **Annotations** go in via pikepdf, which preserves the original
   object structure so readers (Acrobat, Foxit, Firefox) see them as
   real PDF annotations and can toggle visibility.
2. **Text edits, stamps, headers/footers, watermarks, hyperlinks** go in
   via reportlab overlay pages merged with pypdf. These visually modify
   the page content.

Saving in "flatten" mode writes the annotations as page content too by
asking pikepdf to flatten annotations before returning the bytes.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Literal

import pikepdf
import pypdf
from reportlab.lib.colors import Color as RLColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from .document import (
    Bookmark,
    Document,
    EditText,
    FormFill,
    FreeText,
    HeaderFooter,
    Hyperlink,
    Ink,
    Markup,
    Note,
    Shape,
    Stamp,
    Watermark,
)
from .render import page_info

# Reportlab's standard 14 fonts — if the captured font name matches one
# of these we use it directly, otherwise we fall back to Helvetica.
_RL_STANDARD = {
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Symbol", "ZapfDingbats",
}


def save(doc: Document, out: Path, mode: Literal["preserve", "flatten"] = "preserve") -> Path:
    """Render all pending edits into a new PDF at ``out``."""
    # 1. Apply content-layer edits (text edits, free-text, shapes, ink,
    # stamps, headers/footers, watermarks, hyperlinks) by building an
    # overlay PDF with reportlab and stamping it onto the source.
    overlay_bytes = _build_content_overlay(doc)
    if overlay_bytes is not None:
        merged_bytes = _stamp_overlay(doc.source, overlay_bytes)
    else:
        merged_bytes = doc.source.read_bytes()

    # 2. Apply annotation-layer edits (markup, notes, form fills,
    # bookmarks). This works on top of the content-modified bytes.
    final_bytes = _apply_annotations(merged_bytes, doc, flatten=(mode == "flatten"))

    out.write_bytes(final_bytes)
    return out


# ---------------------------------------------------------------------------
# 1. Content overlay via reportlab
# ---------------------------------------------------------------------------

def _build_content_overlay(doc: Document) -> bytes | None:
    """Build one overlay PDF page per page of the source, containing all
    text-edit whiteouts + redraws, stamps, header/footer text, etc.
    Returns None if no overlay content is required (so the caller can
    skip the merge step entirely)."""
    content_kinds = {
        "edit_text", "freetext", "shape", "ink", "stamp",
        "header_footer", "watermark", "hyperlink",
    }
    has_content = any(e.kind in content_kinds for e in doc.edits)
    if not has_content:
        return None

    buf = io.BytesIO()
    # Use a synthetic default canvas size; override per-page below using
    # showPage + setPageSize.
    c = canvas.Canvas(buf, pagesize=letter)
    for page_idx in range(doc.page_count):
        info = page_info(doc.source, page_idx)
        c.setPageSize((info.width, info.height))
        for edit in doc.edits_for_page(page_idx):
            _draw_content_edit(c, edit, info.width, info.height)
        c.showPage()
    c.save()
    return buf.getvalue()


def _draw_content_edit(c: canvas.Canvas, edit, page_w: float, page_h: float) -> None:
    if isinstance(edit, EditText):
        _draw_edit_text(c, edit)
    elif isinstance(edit, FreeText):
        _draw_freetext(c, edit)
    elif isinstance(edit, Shape):
        _draw_shape(c, edit)
    elif isinstance(edit, Ink):
        _draw_ink(c, edit)
    elif isinstance(edit, Stamp):
        _draw_stamp(c, edit)
    elif isinstance(edit, HeaderFooter):
        _draw_header_footer(c, edit, page_w, page_h)
    elif isinstance(edit, Watermark):
        _draw_watermark(c, edit, page_w, page_h)
    elif isinstance(edit, Hyperlink):
        _draw_link_area(c, edit)


def _draw_edit_text(c: canvas.Canvas, edit: EditText) -> None:
    x0, y0, x1, y1 = edit.bbox
    pad = 0.5
    c.setFillColorRGB(1, 1, 1)
    c.setStrokeColorRGB(1, 1, 1)
    c.rect(x0 - pad, y0 - pad, (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad, fill=1, stroke=0)
    font = _resolve_font(edit.fontname)
    c.setFont(font, edit.fontsize)
    _set_fill_rgb(c, edit.color)
    c.drawString(x0, y0 + edit.fontsize * 0.2, edit.new_text)


def _draw_freetext(c: canvas.Canvas, edit: FreeText) -> None:
    x0, y0, x1, y1 = edit.bbox
    c.setFont("Helvetica", edit.fontsize)
    _set_fill_rgb(c, edit.color)
    t = c.beginText(x0, y1 - edit.fontsize)
    for line in edit.text.split("\n"):
        t.textLine(line)
    c.drawText(t)


def _draw_shape(c: canvas.Canvas, edit: Shape) -> None:
    x0, y0, x1, y1 = edit.bbox
    _set_stroke_rgb(c, edit.color)
    c.setLineWidth(edit.width)
    if edit.style == "rect":
        c.rect(x0, y0, x1 - x0, y1 - y0, stroke=1, fill=0)
    elif edit.style == "circle":
        c.ellipse(x0, y0, x1, y1, stroke=1, fill=0)
    elif edit.style == "line":
        c.line(x0, y0, x1, y1)
    elif edit.style == "arrow":
        c.line(x0, y0, x1, y1)
        # Arrowhead: two short strokes at the destination.
        import math
        dx, dy = x1 - x0, y1 - y0
        length = max(1.0, math.hypot(dx, dy))
        ux, uy = dx / length, dy / length
        head = 8.0
        hx1 = x1 - head * ux + head * 0.5 * -uy
        hy1 = y1 - head * uy + head * 0.5 * ux
        hx2 = x1 - head * ux - head * 0.5 * -uy
        hy2 = y1 - head * uy - head * 0.5 * ux
        c.line(x1, y1, hx1, hy1)
        c.line(x1, y1, hx2, hy2)


def _draw_ink(c: canvas.Canvas, edit: Ink) -> None:
    if len(edit.points) < 2:
        return
    _set_stroke_rgb(c, edit.color)
    c.setLineWidth(edit.width)
    c.setLineCap(1)  # round
    c.setLineJoin(1)
    p = c.beginPath()
    p.moveTo(*edit.points[0])
    for x, y in edit.points[1:]:
        p.lineTo(x, y)
    c.drawPath(p, stroke=1, fill=0)


def _draw_stamp(c: canvas.Canvas, edit: Stamp) -> None:
    x0, y0, x1, y1 = edit.bbox
    try:
        from reportlab.lib.utils import ImageReader
        img = ImageReader(str(edit.image_path))
        c.drawImage(img, x0, y0, width=x1 - x0, height=y1 - y0,
                    mask='auto', preserveAspectRatio=True)
    except Exception:
        pass


def _draw_header_footer(c: canvas.Canvas, edit: HeaderFooter, page_w: float, page_h: float) -> None:
    c.setFont("Helvetica", edit.fontsize)
    _set_fill_rgb(c, edit.color)
    margin = 36.0  # half inch
    if edit.position.startswith("header"):
        y = page_h - margin
    else:
        y = margin
    if edit.position.endswith("left"):
        x = margin
        c.drawString(x, y, edit.text)
    elif edit.position.endswith("center"):
        c.drawCentredString(page_w / 2, y, edit.text)
    else:
        c.drawRightString(page_w - margin, y, edit.text)


def _draw_watermark(c: canvas.Canvas, edit: Watermark, page_w: float, page_h: float) -> None:
    c.saveState()
    c.translate(page_w / 2, page_h / 2)
    c.rotate(edit.rotation)
    c.setFont("Helvetica-Bold", edit.fontsize)
    r, g, b = [v / 255 for v in edit.color]
    c.setFillColorRGB(r, g, b, alpha=edit.opacity)
    c.drawCentredString(0, 0, edit.text)
    c.restoreState()


def _draw_link_area(c: canvas.Canvas, edit: Hyperlink) -> None:
    # reportlab supports URI link annotations directly.
    x0, y0, x1, y1 = edit.bbox
    c.linkURL(edit.uri, (x0, y0, x1, y1), relative=0)


def _resolve_font(name: str) -> str:
    clean = name.split("+")[-1] if "+" in name else name
    if clean in _RL_STANDARD:
        return clean
    # Heuristic remaps for common non-standard subsets.
    lower = clean.lower()
    if "bold" in lower and "italic" in lower:
        return "Helvetica-BoldOblique"
    if "bold" in lower:
        return "Helvetica-Bold"
    if "italic" in lower or "oblique" in lower:
        return "Helvetica-Oblique"
    if "mono" in lower or "courier" in lower:
        return "Courier"
    if "times" in lower or "serif" in lower:
        return "Times-Roman"
    return "Helvetica"


def _set_fill_rgb(c: canvas.Canvas, color) -> None:
    r, g, b = [v / 255 for v in color]
    c.setFillColorRGB(r, g, b)


def _set_stroke_rgb(c: canvas.Canvas, color) -> None:
    r, g, b = [v / 255 for v in color]
    c.setStrokeColorRGB(r, g, b)


def _stamp_overlay(source: Path, overlay_bytes: bytes) -> bytes:
    """Merge the overlay PDF onto the source and return fresh bytes."""
    reader_src = pypdf.PdfReader(str(source))
    reader_ovl = pypdf.PdfReader(io.BytesIO(overlay_bytes))
    writer = pypdf.PdfWriter()
    n = min(len(reader_src.pages), len(reader_ovl.pages))
    for i in range(n):
        orig = reader_src.pages[i]
        orig.merge_page(reader_ovl.pages[i])
        writer.add_page(orig)
    # Copy any trailing pages past the overlay count.
    for i in range(n, len(reader_src.pages)):
        writer.add_page(reader_src.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# 2. Annotation layer via pikepdf
# ---------------------------------------------------------------------------

def _apply_annotations(pdf_bytes: bytes, doc: Document, *, flatten: bool) -> bytes:
    with pikepdf.open(io.BytesIO(pdf_bytes), allow_overwriting_input=False) as pdf:
        for edit in doc.edits:
            if isinstance(edit, Markup):
                _add_markup_annot(pdf, edit)
            elif isinstance(edit, Note):
                _add_note_annot(pdf, edit)
            elif isinstance(edit, FormFill):
                _set_form_field(pdf, edit)
            elif isinstance(edit, Bookmark):
                _add_bookmark(pdf, edit)
        # Bookmarks are written to /Outlines via pikepdf's OutlineItem API
        # below during save (not here).
        if flatten:
            # Flatten annotations into page content.
            try:
                pdf.flatten_annotations()
            except Exception:
                pass  # pikepdf raises on some malformed PDFs; not fatal
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


def _add_markup_annot(pdf: pikepdf.Pdf, edit: Markup) -> None:
    page = pdf.pages[edit.page]
    x0, y0, x1, y1 = edit.bbox
    quad = [x0, y1, x1, y1, x0, y0, x1, y0]
    subtype = {
        "highlight": "/Highlight",
        "strike": "/StrikeOut",
        "underline": "/Underline",
    }[edit.style]
    color = [v / 255 for v in edit.color]
    annot = pikepdf.Dictionary(
        Type=pikepdf.Name("/Annot"),
        Subtype=pikepdf.Name(subtype),
        Rect=[x0, y0, x1, y1],
        QuadPoints=quad,
        C=color,
        F=4,
        T="Cove PDF Editor",
    )
    _append_annot(page, annot)


def _add_note_annot(pdf: pikepdf.Pdf, edit: Note) -> None:
    page = pdf.pages[edit.page]
    box = [edit.x - 8, edit.y - 8, edit.x + 8, edit.y + 8]
    annot = pikepdf.Dictionary(
        Type=pikepdf.Name("/Annot"),
        Subtype=pikepdf.Name("/Text"),
        Rect=box,
        Contents=edit.text,
        Open=False,
        Name=pikepdf.Name("/Comment"),
        T=edit.author,
        F=4,
    )
    _append_annot(page, annot)


def _append_annot(page, annot_dict: pikepdf.Dictionary) -> None:
    annots = page.get("/Annots")
    if annots is None:
        page["/Annots"] = pikepdf.Array([annot_dict])
    else:
        annots.append(annot_dict)


def _set_form_field(pdf: pikepdf.Pdf, edit: FormFill) -> None:
    # Use pypdf on the already-saved bytes path instead? Simpler: walk
    # /AcroForm /Fields and match by /T.
    try:
        root = pdf.Root
        form = root.get("/AcroForm")
        if form is None:
            return
        fields = form.get("/Fields")
        if fields is None:
            return
        target = None
        for f in _iter_form_fields(fields):
            if str(f.get("/T", "")).strip("()") == edit.field_name:
                target = f
                break
        if target is None:
            return
        if isinstance(edit.value, bool):
            target["/V"] = pikepdf.Name("/Yes") if edit.value else pikepdf.Name("/Off")
            target["/AS"] = target["/V"]
        else:
            target["/V"] = pikepdf.String(str(edit.value))
        # Tell viewers to regenerate appearances.
        form["/NeedAppearances"] = True
    except Exception:
        pass


def _iter_form_fields(arr):
    for f in arr:
        yield f
        kids = f.get("/Kids")
        if kids is not None:
            yield from _iter_form_fields(kids)


def _add_bookmark(pdf: pikepdf.Pdf, edit: Bookmark) -> None:
    # Use pikepdf's outline API.
    with pdf.open_outline() as outline:
        item = pikepdf.OutlineItem(edit.title, edit.page)
        outline.root.append(item)


# ---------------------------------------------------------------------------
# Form field enumeration (used by the UI to build the form panel)
# ---------------------------------------------------------------------------

def list_form_fields(source: Path) -> list[dict]:
    out = []
    try:
        with pikepdf.open(source) as pdf:
            form = pdf.Root.get("/AcroForm")
            if form is None:
                return out
            fields = form.get("/Fields") or []
            for f in _iter_form_fields(fields):
                name = str(f.get("/T", "")).strip("()")
                ft = str(f.get("/FT", "")).strip("/")
                value = str(f.get("/V", "")).strip("()")
                if name:
                    out.append({"name": name, "type": ft, "value": value})
    except Exception:
        pass
    return out
