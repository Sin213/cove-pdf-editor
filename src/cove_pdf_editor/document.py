"""Document model + edit operations.

Each user action produces an ``Edit`` entry on ``Document.edits``.
Saving applies them to the source PDF; nothing is destructive until
the user picks an output path, which keeps undo/redo a simple list
mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
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


@dataclass
class BubbleEdit:
    """Numbered balloon callout (engineering / blueprint style).

    On the canvas: a small circle with a sequential number inside, plus
    an optional leader line whose tip points at ``leader_anchor`` (the
    feature being labeled). On save, the circle + number + leader are
    baked into the page as vector drawings — they are NOT PDF
    annotations, so other viewers cannot edit them.

    ``text`` is an in-session description shown on hover / click. It is
    not written to the saved PDF (the saved drawing is hardcoded).
    A future "key page" feature can collect descriptions into a table.
    """

    page: int
    bbox: Rect                               # small bounding square, ~24pt
    number: int                              # sequence number drawn inside
    leader_anchor: tuple[float, float] | None = None
    text: str = ""
    fontsize: float = 11.0
    fontname: str = "Helvetica"
    fill_color: Color = (220, 232, 246)      # light blueprint blue
    border_color: Color = (40, 90, 150)
    text_color: Color = (40, 90, 150)
    # Set after a Save flushes this balloon's circle/number/leader into
    # the source PDF's content stream. Keeps the dataclass alive (so
    # ``text`` survives for "Append Balloon Key Page") while preventing
    # a second save from drawing the graphic again on top of itself.
    baked: bool = False

    kind: Literal["bubble"] = "bubble"


@dataclass
class RedactionEdit:
    """Hardens a rectangle on save: text, images, and vector graphics
    intersecting the rect are removed from the page content stream and
    the rect is filled black. Not an annotation — recipients cannot
    recover the underlying content. Verify the saved PDF before
    publishing."""

    page: int
    bbox: Rect

    kind: Literal["redaction"] = "redaction"


Edit = EditText | FreeText | ImageEdit | BubbleEdit | RedactionEdit


@dataclass
class Document:
    source: Path
    page_count: int
    edits: list[Edit] = field(default_factory=list)
    dirty: bool = False

    def __post_init__(self) -> None:
        # Page-indexed lookup: rebuilt lazily in _rebuild_index().
        # Kept in sync by add() / remove(). Direct mutation of
        # ``edits`` (e.g. bulk-clear on save) must call _rebuild_index().
        self._page_index: dict[int, list[Edit]] = defaultdict(list)
        for edit in self.edits:
            self._page_index[edit.page].append(edit)

    def _rebuild_index(self) -> None:
        """Rebuild the page index from scratch.

        Call this whenever ``edits`` is replaced or bulk-mutated
        (e.g. after save clears the list).
        """
        self._page_index = defaultdict(list)
        for edit in self.edits:
            self._page_index[edit.page].append(edit)

    def add(self, edit: Edit) -> None:
        self.edits.append(edit)
        self._page_index[edit.page].append(edit)
        self.dirty = True

    def remove(self, edit: Edit) -> None:
        try:
            self.edits.remove(edit)
            self.dirty = True
        except ValueError:
            return
        page_list = self._page_index.get(edit.page)
        if page_list is not None:
            try:
                page_list.remove(edit)
            except ValueError:
                pass

    def edits_for_page(self, page: int) -> list[Edit]:
        return list(self._page_index.get(page, []))
