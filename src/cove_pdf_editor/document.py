"""Document model + edit operations.

Everything the user does becomes an ``Edit`` entry in ``Document.edits``.
Hitting *Save* applies them in order: annotations land via pikepdf, text
edits + stamps + header/footer content land via reportlab overlays that
are merged onto the page with pypdf.

No edits are destructive until save — the source PDF on disk stays
untouched until the user picks an output path. This makes undo / redo a
simple list mutation and keeps the failure blast radius small.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Color = tuple[int, int, int]
Rect = tuple[float, float, float, float]   # PDF points, (x0, y0, x1, y1)


@dataclass
class EditText:
    page: int
    bbox: Rect
    old_text: str
    new_text: str
    fontname: str
    fontsize: float
    color: Color = (0, 0, 0)

    kind: Literal["edit_text"] = "edit_text"


@dataclass
class Markup:
    page: int
    bbox: Rect
    style: Literal["highlight", "strike", "underline"]
    color: Color = (255, 230, 0)

    kind: Literal["markup"] = "markup"


@dataclass
class Note:
    page: int
    x: float     # anchor point (PDF points)
    y: float
    text: str
    author: str = "Cove PDF Editor"

    kind: Literal["note"] = "note"


@dataclass
class FreeText:
    page: int
    bbox: Rect
    text: str
    fontsize: float = 11.0
    color: Color = (0, 0, 0)

    kind: Literal["freetext"] = "freetext"


@dataclass
class Shape:
    page: int
    bbox: Rect
    style: Literal["rect", "circle", "line", "arrow"]
    color: Color = (220, 40, 40)
    width: float = 1.5

    kind: Literal["shape"] = "shape"


@dataclass
class Ink:
    page: int
    points: list[tuple[float, float]]  # PDF points, in stroke order
    color: Color = (220, 40, 40)
    width: float = 1.5

    kind: Literal["ink"] = "ink"


@dataclass
class Stamp:
    page: int
    bbox: Rect
    image_path: Path

    kind: Literal["stamp"] = "stamp"


@dataclass
class FormFill:
    field_name: str
    value: str | bool

    kind: Literal["form_fill"] = "form_fill"


@dataclass
class HeaderFooter:
    text: str
    position: Literal["header-left", "header-center", "header-right",
                      "footer-left", "footer-center", "footer-right"]
    fontsize: float = 10.0
    color: Color = (100, 100, 100)
    pages: str = "all"  # "all", or "1,3,5-7"

    kind: Literal["header_footer"] = "header_footer"


@dataclass
class Watermark:
    text: str
    fontsize: float = 60.0
    color: Color = (200, 200, 200)
    opacity: float = 0.3
    rotation: float = 45.0
    pages: str = "all"

    kind: Literal["watermark"] = "watermark"


@dataclass
class Bookmark:
    title: str
    page: int
    parent: int | None = None   # index into Document.edits of parent Bookmark, or None for root

    kind: Literal["bookmark"] = "bookmark"


@dataclass
class Hyperlink:
    page: int
    bbox: Rect
    uri: str

    kind: Literal["hyperlink"] = "hyperlink"


Edit = (
    EditText | Markup | Note | FreeText | Shape | Ink | Stamp
    | FormFill | HeaderFooter | Watermark | Bookmark | Hyperlink
)


@dataclass
class Document:
    source: Path
    page_count: int
    edits: list[Edit] = field(default_factory=list)
    dirty: bool = False

    def add(self, edit: Edit) -> None:
        self.edits.append(edit)
        self.dirty = True

    def remove(self, edit: Edit) -> None:
        try:
            self.edits.remove(edit)
            self.dirty = True
        except ValueError:
            pass

    def edits_for_page(self, page: int) -> list[Edit]:
        out = []
        for e in self.edits:
            if getattr(e, "page", None) == page:
                out.append(e)
            elif e.kind in ("header_footer", "watermark") and _page_in_spec(page, e.pages):
                out.append(e)
        return out


def _page_in_spec(page: int, spec: str) -> bool:
    """Return True if ``page`` (0-based) is covered by the pages spec."""
    if spec.strip().lower() == "all":
        return True
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            try:
                if int(a) - 1 <= page <= int(b) - 1:
                    return True
            except ValueError:
                continue
        else:
            try:
                if page == int(chunk) - 1:
                    return True
            except ValueError:
                continue
    return False
