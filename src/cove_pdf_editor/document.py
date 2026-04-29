"""Document model + edit operations.

Each user action produces an ``Edit`` entry on ``Document.edits``.
Saving applies them to the source PDF; nothing is destructive until
the user picks an output path, which keeps undo/redo a simple list
mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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
    bold: bool = False
    italic: bool = False
    # Bbox of the *source* span on the original PDF page. Stays pinned
    # so the redaction on save always covers the right area, even if
    # the user has moved/resized the replacement object.
    original_bbox: Rect | None = None

    kind: Literal["edit_text"] = "edit_text"

    def __post_init__(self) -> None:
        # Default original_bbox to bbox at creation so legacy callers
        # don't have to pass both. Movement updates bbox; original_bbox
        # stays put.
        if self.original_bbox is None:
            self.original_bbox = self.bbox


@dataclass
class FreeText:
    page: int
    bbox: Rect
    text: str
    fontsize: float = 12.0
    color: Color = (0, 0, 0)
    fontname: str = "Helvetica"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    align: Literal["left", "center", "right"] = "left"

    kind: Literal["freetext"] = "freetext"


@dataclass
class ImageEdit:
    page: int
    bbox: Rect
    # ``None`` is a tombstone: the image was promoted from the source PDF
    # and then deleted, so we still need to whiteout ``original_bbox`` on
    # save but draw nothing in its place.
    image_path: Path | None
    # Set when this edit was promoted from an existing PDF image. The
    # area is whiteouted in preview and on save so the original baked-in
    # pixels don't show through underneath the moved/resized object.
    original_bbox: Rect | None = None

    kind: Literal["image"] = "image"


Edit = EditText | FreeText | ImageEdit


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
        return [e for e in self.edits if e.page == page]
