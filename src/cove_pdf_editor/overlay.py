"""Bake a Document's edits into a saved PDF.

PyMuPDF opens the source PDF, draws each pending edit directly onto the
matching page (whiteout + replacement glyphs for ``EditText``; positioned
text for ``FreeText``; placed bitmap for ``ImageEdit``; rounded-rect
callouts for ``BubbleEdit``), and writes the result. One library, one
pass, no overlay+merge dance.

Edits are baked into the page content stream. Other typical PDF
artifacts — sticky notes, ink, form-field updates, bookmarks,
hyperlinks, watermarks, headers/footers — are not produced.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

from .document import (
    BubbleEdit,
    Document,
    EditText,
    FreeText,
    ImageEdit,
    RedactionEdit,
)


def save(doc: Document, out: Path) -> Path:
    """Render all pending edits into a new PDF at ``out``.

    Per page: queue a PDF redaction for each ``EditText`` (so the original
    glyphs are removed from the content stream, not just visually
    covered), apply the redactions, then draw replacement text + any
    ``FreeText`` / ``ImageEdit`` on top.

    Writes always land via a sibling temp file in the destination's
    directory followed by :func:`os.replace`, so a kill mid-write never
    leaves a partial file at ``out``. When ``out`` resolves to the same
    inode as ``doc.source``, the source handle is closed before the
    rename, which side-steps pymupdf's "save to original must be
    incremental" rejection and Windows file-locking errors.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{out.name}.", suffix=".part", dir=str(out.parent),
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        if not doc.edits:
            # No work to do; stream-copy so multi-GB sources don't OOM.
            with open(doc.source, "rb") as src, open(tmp_path, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
        else:
            with pymupdf.open(str(doc.source)) as pdf:
                for page_idx in range(doc.page_count):
                    page = pdf[page_idx]
                    page_edits = doc.edits_for_page(page_idx)
                    # Pass 1 — hard redactions. Black-fill rects whose
                    # content (text + images + graphics) must be removed
                    # from the page stream entirely. Run before EditText
                    # whiteouts so the two passes don't interfere.
                    hard = False
                    for edit in page_edits:
                        if isinstance(edit, RedactionEdit):
                            rect = _pdf_rect(page, edit.bbox)
                            page.add_redact_annot(rect, fill=(0, 0, 0))
                            hard = True
                    if hard:
                        page.apply_redactions(images=2, graphics=1)
                    # Pass 2 — EditText whiteouts. Images and graphics
                    # are intentionally left alone (``images=0``,
                    # ``graphics=0``) so a text replacement that
                    # happens to overlap a logo doesn't delete the
                    # logo.
                    redacted = False
                    for edit in page_edits:
                        if isinstance(edit, EditText):
                            _queue_redaction(page, edit)
                            redacted = True
                    if redacted:
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
                pdf.save(str(tmp_path), garbage=4, deflate=True)
        # ``mkstemp`` opens the temp file with mode 0600. Without this
        # adjustment, ``os.replace`` would silently make the destination
        # owner-only — surprising for shared/readable PDFs and for new
        # files that the user expects to inherit umask defaults.
        _align_dest_mode(tmp_path, out)
        # Atomic publish — pdf is closed (or the source file is closed)
        # before we replace, so Windows can swing the rename and the
        # destination is never partially written.
        os.replace(str(tmp_path), str(out))
    except (OSError, RuntimeError, ValueError) as exc:
        log.error("Save to %s failed, removing temp file: %s", out, exc)
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return out


def _align_dest_mode(tmp_path: Path, out: Path) -> None:
    """Match the temp file's permissions to what the destination should
    end up with: preserve an existing destination's mode (so overwriting
    a 0644 PDF doesn't quietly drop world-read), or apply the current
    umask to a fresh file (so a new save behaves like ``open(..., 'w')``
    rather than the 0600 mkstemp default)."""
    try:
        if out.exists():
            os.chmod(tmp_path, os.stat(out).st_mode & 0o7777)
            return
        # No destination yet: derive the umask-respecting mode the way
        # the standard library does for a freshly created regular file.
        prev = os.umask(0)
        os.umask(prev)
        os.chmod(tmp_path, 0o666 & ~prev)
    except OSError:
        # chmod is best-effort (e.g. on Windows it only toggles read-
        # only); don't fail the whole save over a permission tweak.
        pass


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
    elif isinstance(edit, BubbleEdit):
        _draw_bubble(page, edit)


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


def _draw_bubble(page: pymupdf.Page, edit: BubbleEdit) -> None:
    """Bake a numbered balloon callout as vector drawings.

    Output: a filled circle, the number drawn inside, and (if the edit
    has one) a leader line from the circle edge to ``leader_anchor``
    with a small arrowhead at the anchor. These are real PDF content
    streams — the result is not a PDF annotation, so it cannot be
    edited or removed in other PDF viewers.

    Note: ``edit.text`` (the description) is NOT written to the PDF.
    Keep descriptions in-session; a future "key page" feature can
    collect them into a table.

    Skipped when the edit is already baked into the source PDF. The
    dataclass is kept around after save so the description survives
    for the Balloon Key page; redrawing here would stamp a second
    circle on top of the first on every subsequent save.
    """
    if getattr(edit, "baked", False):
        return
    rect = _pdf_rect(page, edit.bbox)
    cx = (rect.x0 + rect.x1) / 2
    cy = (rect.y0 + rect.y1) / 2
    radius = min(rect.width, rect.height) / 2
    fill = _to_float(edit.fill_color)
    border = _to_float(edit.border_color)
    text_color = _to_float(edit.text_color)

    page_h = page.rect.height
    if edit.leader_anchor is not None:
        ax_pt, ay_pt = edit.leader_anchor
        anchor = pymupdf.Point(ax_pt, page_h - ay_pt)
        dx = anchor.x - cx
        dy = anchor.y - cy
        dist = (dx * dx + dy * dy) ** 0.5
        if dist > radius + 1:
            ux, uy = dx / dist, dy / dist
            edge = pymupdf.Point(cx + ux * radius, cy + uy * radius)
            page.draw_line(edge, anchor, color=border, width=0.8)
            # Arrowhead — small filled triangle at the anchor.
            head_len = 6.0
            spread = 2.5
            base = pymupdf.Point(anchor.x - ux * head_len,
                                 anchor.y - uy * head_len)
            px, py = -uy, ux
            p1 = pymupdf.Point(base.x + px * spread, base.y + py * spread)
            p2 = pymupdf.Point(base.x - px * spread, base.y - py * spread)
            page.draw_polyline(
                [anchor, p1, p2, anchor],
                color=border, fill=border, width=0.5,
            )

    page.draw_circle(
        pymupdf.Point(cx, cy), radius,
        color=border, fill=fill, width=1.0,
    )

    fontname = _resolve_font(edit.fontname, bold=True)
    text = str(edit.number)
    text_w = pymupdf.get_text_length(text, fontsize=edit.fontsize, fontname=fontname)
    # Eyeballed vertical centering for base-14 fonts: cap height is
    # roughly 70% of fontsize, so dropping the baseline ~25% below the
    # circle center reads as centered.
    page.insert_text(
        pymupdf.Point(cx - text_w / 2, cy + edit.fontsize * 0.32),
        text,
        fontsize=edit.fontsize, fontname=fontname, color=text_color,
    )


def append_balloon_key_pages(
    pdf_doc: pymupdf.Document,
    bubbles: list[BubbleEdit],
) -> int:
    """Append one or more Letter-size pages summarizing balloon
    callouts. Each row lists the balloon number, the source page it's
    on, and the in-session description. Rows are sorted by source page
    then number. Returns the number of pages appended (0 if there are
    no bubbles)."""
    if not bubbles:
        return 0
    rows = sorted(bubbles, key=lambda e: (e.page, e.number))

    PW, PH = 612.0, 792.0
    M = 50.0
    col_num_x = M + 6
    col_page_x = M + 50
    col_desc_x = M + 100
    desc_w = PW - M - col_desc_x
    title_size = 20.0
    header_size = 10.0
    body_size = 10.0
    line_h = body_size * 1.35

    pages_added = 0

    def _new_page() -> tuple[pymupdf.Page, float]:
        nonlocal pages_added
        page = pdf_doc.new_page(width=PW, height=PH)
        pages_added += 1
        # Title (only on the first page; subsequent pages just get a
        # continuation header so the table flows).
        if pages_added == 1:
            page.insert_text(
                pymupdf.Point(M, M + title_size),
                "Balloon Key",
                fontsize=title_size, fontname="Helvetica-Bold",
                color=(0.15, 0.35, 0.6),
            )
            ty = M + title_size + 18
        else:
            page.insert_text(
                pymupdf.Point(M, M + 14),
                "Balloon Key (continued)",
                fontsize=12, fontname="Helvetica-Bold",
                color=(0.4, 0.45, 0.55),
            )
            ty = M + 30
        # Header row: filled band + column labels.
        page.draw_rect(
            pymupdf.Rect(M, ty, PW - M, ty + 18),
            fill=(0.92, 0.94, 0.98), color=(0.4, 0.5, 0.7), width=0.5,
        )
        page.insert_text(
            pymupdf.Point(col_num_x, ty + 13),
            "#", fontsize=header_size, fontname="Helvetica-Bold",
        )
        page.insert_text(
            pymupdf.Point(col_page_x, ty + 13),
            "Page", fontsize=header_size, fontname="Helvetica-Bold",
        )
        page.insert_text(
            pymupdf.Point(col_desc_x, ty + 13),
            "Description", fontsize=header_size, fontname="Helvetica-Bold",
        )
        return page, ty + 22

    page, y = _new_page()
    bottom_limit = PH - M

    for bubble in rows:
        text = (bubble.text or "(no description)").strip() or "(no description)"
        wrapped = _wrap_lines(text, desc_w, body_size, "Helvetica")
        if not wrapped:
            wrapped = [""]
        row_h = max(line_h, len(wrapped) * line_h) + 6
        if y + row_h > bottom_limit:
            page, y = _new_page()
        page.insert_text(
            pymupdf.Point(col_num_x, y + body_size),
            str(bubble.number),
            fontsize=body_size, fontname="Helvetica-Bold",
        )
        page.insert_text(
            pymupdf.Point(col_page_x, y + body_size),
            str(bubble.page + 1),
            fontsize=body_size, fontname="Helvetica",
        )
        for i, line in enumerate(wrapped):
            page.insert_text(
                pymupdf.Point(col_desc_x, y + body_size + i * line_h),
                line,
                fontsize=body_size, fontname="Helvetica",
            )
        y += row_h
        page.draw_line(
            pymupdf.Point(M, y),
            pymupdf.Point(PW - M, y),
            color=(0.85, 0.88, 0.92), width=0.3,
        )
        y += 2

    return pages_added


def _draw_image(page: pymupdf.Page, edit: ImageEdit) -> None:
    """Place the bitmap stretched to fill the bbox so the saved output
    matches the on-canvas preview, which uses the same bbox without
    aspect-ratio preservation. ``image_path is None`` is a tombstone for
    a promoted source image the user deleted — the whiteout in
    ``save()`` already covered the original; nothing else to draw.

    The bytes are fed via ``stream=`` rather than ``filename=`` so a PNG
    soft-mask (alpha) survives the embed. ``filename=`` can lose the
    soft-mask depending on the path PyMuPDF takes internally, which
    flattens transparent areas to opaque black on the saved page."""
    if edit.image_path is None:
        return
    rect = _pdf_rect(page, edit.bbox)
    try:
        data = Path(edit.image_path).read_bytes()
    except OSError:
        return
    try:
        page.insert_image(rect, stream=data, keep_proportion=False)
    except (RuntimeError, ValueError) as exc:
        log.warning("Could not insert image %s into page: %s", edit.image_path, exc)


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
