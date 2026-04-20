# Cove PDF Editor

An offline PDF editor for **Linux** and **Windows**. The everyday edits
most people pay Foxit or Acrobat for — in a focused desktop app that
never touches the cloud.

![icon](cove_icon.png)

## Download (v1.0.0)

| Platform | File |
| -------- | ---- |
| Windows (installer) | `cove-pdf-editor-1.0.0-Setup.exe` |
| Windows (portable) | `cove-pdf-editor-1.0.0-Portable.exe` |
| Linux (AppImage) | `Cove-PDF-Editor-1.0.0-x86_64.AppImage` |
| Linux (Debian / Ubuntu) | `cove-pdf-editor_1.0.0_amd64.deb` |

Grab the artifacts from the [Releases page](https://github.com/Sin213/cove-pdf-editor/releases).

## The flagship: Edit Text

Click any text on the page. An inline editor appears with the existing
text pre-filled and the detected font + size remembered. Hit Enter and
the original text is replaced in-place, using the same font so it looks
native. This is the "quick invoice fix" use case — not full multi-line
paragraph reflow, but it nails the common cases.

Works best on accounting-software / office PDFs with clean, searchable
text. Scanned PDFs need OCR first (use a separate tool like `cove-ocr`),
and some design-tool exports convert text to vector outlines which can't
be edited as text.

## Everything else

**Annotations:** highlight, strikethrough, underline; sticky notes; free
text boxes; rectangles, circles, lines, arrows; freehand ink drawing.

**Stamps & signatures:** drop any PNG/JPG as a stamp; hand-drawn
signatures via a small canvas dialog, saved as a reusable stamp.

**Forms:** fill in AcroForm fields (the common kind). No XFA — that's
largely deprecated.

**Page extras:** headers, footers, page numbers, watermarks (text with
rotation/opacity), bookmarks, hyperlinks.

**Save options:**
- **Preserve** — annotations stay as layered PDF objects, which readers
  can toggle independently. Editable later.
- **Flatten** — everything bakes into the page content. The edits travel
  with the file no matter which reader opens it.

## Requirements

- `ffmpeg` not needed. No ML models. No internet at runtime.
- Python 3.10+ to run from source.

## Running from source

```bash
pip install -e .
cove-pdf-editor
```

Or without installing:

```bash
PYTHONPATH=src python -m cove_pdf_editor
```

## Building release artifacts

**Linux:**
```bash
VERSION=1.0.0 ./scripts/build-release.sh
```

**Windows:**
```powershell
.\build.ps1 -Version 1.0.0
```
(Requires Python 3.12+ and [Inno Setup 6](https://jrsoftware.org/isdl.php).)

**GitHub Actions:** tagging `vX.Y.Z` builds all four artifacts and
attaches them to a release.

## Known limits

- No true in-place paragraph text editing (the single thing Foxit owns).
  Our Edit Text tool uses an overlay technique — works great for
  short replacements at the original font/size, doesn't reflow.
- No OCR for scanned PDFs (separate app).
- Font fallback: if the captured font name isn't in reportlab's standard
  set, we pick the closest visual match. Usually imperceptible; for
  exotic fonts you may see a slight style difference.
- "Edit text" works on a whole line at a time by default, not a single
  word. Select the whole line's worth of replacement text.

## License

MIT — see [LICENSE](LICENSE).
