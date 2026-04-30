#!/usr/bin/env bash
# Build .AppImage and .deb for Cove PDF Editor.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="cove-pdf-editor"
DISPLAY_NAME="Cove PDF Editor"
APP_SLUG="Cove-PDF-Editor"
VERSION="${VERSION:-2.0.0}"
ARCH="x86_64"
DEB_ARCH="amd64"
RELEASE_DIR="$ROOT/release"
DIST_DIR="$ROOT/dist"
APPDIR="$ROOT/build/AppDir"
DEB_BUILD="$ROOT/build/deb"
ICON_SRC="$ROOT/src/cove_pdf_editor/assets/cove_icon.png"

LOCAL_BIN="${HOME}/.local/bin"
APPIMAGETOOL="${LOCAL_BIN}/appimagetool"

BUILDENV="$ROOT/.buildenv"

mkdir -p "$RELEASE_DIR" "$LOCAL_BIN"
rm -rf "$DIST_DIR" "$ROOT/build"
mkdir -p "$ROOT/build"

echo "==> Creating build venv"
rm -rf "$BUILDENV"
python -m venv "$BUILDENV"
"$BUILDENV/bin/pip" install --quiet --upgrade pip
"$BUILDENV/bin/pip" install --quiet PySide6 pymupdf pypdfium2 Pillow pyinstaller

echo "==> Running PyInstaller"
"$BUILDENV/bin/python" -m PyInstaller --noconfirm --clean packaging/cove-pdf-editor.spec

BUNDLE="$DIST_DIR/$APP_NAME"
[ -d "$BUNDLE" ] || { echo "PyInstaller bundle not found at $BUNDLE"; exit 1; }

echo "==> Assembling AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/$APP_NAME" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"

cp -r "$BUNDLE"/. "$APPDIR/usr/lib/$APP_NAME/"
cp "$ICON_SRC" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_NAME.png"
cp "$ICON_SRC" "$APPDIR/$APP_NAME.png"
cp "$ICON_SRC" "$APPDIR/.DirIcon" 2>/dev/null || true

cat > "$APPDIR/$APP_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$DISPLAY_NAME
GenericName=Offline PDF Editor
Comment=Edit text, annotate, sign, and fill PDFs — fully offline
Exec=$APP_NAME
Icon=$APP_NAME
Terminal=false
Categories=Office;Utility;
Keywords=pdf;edit;annotate;highlight;sign;form;
StartupNotify=true
EOF
cp "$APPDIR/$APP_NAME.desktop" "$APPDIR/usr/share/applications/$APP_NAME.desktop"

cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="$HERE/usr/bin:$PATH"
export LD_LIBRARY_PATH="$HERE/usr/lib/cove-pdf-editor:${LD_LIBRARY_PATH:-}"
exec "$HERE/usr/lib/cove-pdf-editor/cove-pdf-editor" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/usr/bin/$APP_NAME" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")/../lib/cove-pdf-editor"
exec "$HERE/cove-pdf-editor" "$@"
EOF
chmod +x "$APPDIR/usr/bin/$APP_NAME"

if [ ! -x "$APPIMAGETOOL" ]; then
    if command -v appimagetool >/dev/null 2>&1; then
        APPIMAGETOOL="$(command -v appimagetool)"
    else
        echo "==> Downloading appimagetool"
        curl -fL --retry 3 -o "$APPIMAGETOOL" \
            "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
        chmod +x "$APPIMAGETOOL"
    fi
fi

echo "==> Building AppImage"
APPIMAGE_OUT="$RELEASE_DIR/${APP_SLUG}-${VERSION}-${ARCH}.AppImage"
ARCH=$ARCH "$APPIMAGETOOL" --no-appstream "$APPDIR" "$APPIMAGE_OUT"
chmod +x "$APPIMAGE_OUT"
echo "    -> $APPIMAGE_OUT"

echo "==> Assembling .deb tree"
PKG_ROOT="$DEB_BUILD/${APP_NAME}_${VERSION}_${DEB_ARCH}"
rm -rf "$DEB_BUILD"
mkdir -p "$PKG_ROOT/DEBIAN" \
         "$PKG_ROOT/usr/bin" \
         "$PKG_ROOT/usr/lib/$APP_NAME" \
         "$PKG_ROOT/usr/share/applications" \
         "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps" \
         "$PKG_ROOT/usr/share/doc/$APP_NAME"

cp -r "$BUNDLE"/. "$PKG_ROOT/usr/lib/$APP_NAME/"
cp "$ICON_SRC" "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps/$APP_NAME.png"

cat > "$PKG_ROOT/usr/bin/$APP_NAME" <<'EOF'
#!/usr/bin/env bash
exec /usr/lib/cove-pdf-editor/cove-pdf-editor "$@"
EOF
chmod +x "$PKG_ROOT/usr/bin/$APP_NAME"

cat > "$PKG_ROOT/usr/share/applications/$APP_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$DISPLAY_NAME
GenericName=Offline PDF Editor
Comment=Edit text, annotate, sign, and fill PDFs — fully offline
Exec=$APP_NAME
Icon=$APP_NAME
Terminal=false
Categories=Office;Utility;
Keywords=pdf;edit;annotate;highlight;sign;form;
StartupNotify=true
EOF

cp "$ROOT/LICENSE" "$PKG_ROOT/usr/share/doc/$APP_NAME/copyright"

INSTALLED_SIZE=$(du -sk "$PKG_ROOT/usr" | awk '{print $1}')

cat > "$PKG_ROOT/DEBIAN/control" <<EOF
Package: $APP_NAME
Version: $VERSION
Architecture: $DEB_ARCH
Maintainer: Cove <noreply@cove.local>
Installed-Size: $INSTALLED_SIZE
Section: utils
Priority: optional
Description: Offline PDF editor — edit text, annotate, sign, fill forms
 Cove PDF Editor is a focused desktop app for the everyday PDF edits most
 people use Foxit or Acrobat for: editing existing text (overlay technique),
 highlighting, free-text boxes, drawing, sticky notes, image stamps and
 signatures, AcroForm fill, headers/footers/watermarks, bookmarks, and
 hyperlinks. No cloud, no account — everything runs locally.
EOF

echo "==> Building .deb archive"
DEB_OUT="$RELEASE_DIR/${APP_SLUG}-${VERSION}-${DEB_ARCH}.deb"
WORK="$DEB_BUILD/work"
rm -rf "$WORK"
mkdir -p "$WORK"

(cd "$PKG_ROOT" && tar --xz --owner=0 --group=0 -cf "$WORK/control.tar.xz" -C DEBIAN .)
(cd "$PKG_ROOT" && tar --xz --owner=0 --group=0 -cf "$WORK/data.tar.xz" \
    --transform 's,^\./,,' \
    --exclude=./DEBIAN \
    .)
echo -n "2.0" > "$WORK/debian-binary"
echo "" >> "$WORK/debian-binary"

(cd "$WORK" && ar -rc "$DEB_OUT" debian-binary control.tar.xz data.tar.xz)

echo "    -> $DEB_OUT"

rm -rf "$BUILDENV" "$DIST_DIR" "$ROOT/build"

echo ""
echo "Release artifacts:"
ls -lh "$RELEASE_DIR"
