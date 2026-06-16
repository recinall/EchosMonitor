#!/usr/bin/env bash
# Wrap the PyInstaller one-dir bundle (dist/echosmonitor/) into an AppImage
# (M7-C2). Run AFTER scripts/build.sh has produced dist/echosmonitor/.
#
# Usage:  packaging/linux/build_appimage.sh <version>
# Output: dist/EchosMonitor-<version>-x86_64.AppImage
#
# appimagetool is fetched on demand and run with --appimage-extract-and-run so
# no FUSE is required (GitHub runners have no /dev/fuse).
set -euo pipefail

VERSION="${1:?usage: build_appimage.sh <version>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

BUNDLE="dist/echosmonitor"
[[ -d "$BUNDLE" ]] || { echo "missing $BUNDLE — run scripts/build.sh first" >&2; exit 1; }

APPDIR="build/EchosMonitor.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# The whole one-dir bundle goes under usr/bin (exe + _internal next to it).
cp -a "$BUNDLE"/. "$APPDIR/usr/bin/"

# Icon: 256x256 PNG at the AppDir root (appimagetool reads it) + the hicolor
# theme path the .desktop Icon= key resolves against.
ICON_SRC="src/echosmonitor/resources/EchosMonitor.png"
python3 - "$ICON_SRC" "$APPDIR/echosmonitor.png" <<'PY'
import sys
from PIL import Image
Image.open(sys.argv[1]).convert("RGBA").resize((256, 256), Image.LANCZOS).save(sys.argv[2])
PY
cp "$APPDIR/echosmonitor.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/echosmonitor.png"

cat > "$APPDIR/echosmonitor.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=EchosMonitor
Comment=Echos seismic device monitoring, recording and analysis
Exec=echosmonitor
Icon=echosmonitor
Categories=Science;Education;
Terminal=false
EOF

cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "${HERE}/usr/bin/echosmonitor" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# Fetch appimagetool once (cached in build/).
TOOL="build/appimagetool-x86_64.AppImage"
if [[ ! -x "$TOOL" ]]; then
  echo ">> downloading appimagetool"
  curl -fsSL -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$TOOL"
fi

OUT="dist/EchosMonitor-${VERSION}-x86_64.AppImage"
echo ">> building $OUT"
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
echo ">> AppImage OK -> $OUT"
