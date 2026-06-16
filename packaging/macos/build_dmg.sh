#!/usr/bin/env bash
# Wrap the PyInstaller .app bundle (dist/EchosMonitor.app, produced by the
# macOS BUNDLE step in echosmonitor.spec) into a compressed .dmg (M7-C2).
# Run AFTER scripts/build.sh.
#
# Usage:  packaging/macos/build_dmg.sh <version>
# Output: dist/EchosMonitor-<version>.dmg
#
# Uses the built-in hdiutil (no brew dependency). The disk image contains the
# .app plus an /Applications symlink so the user can drag-to-install. Unsigned
# for now (M7-D) — users right-click → Open or clear the quarantine attribute.
set -euo pipefail

VERSION="${1:?usage: build_dmg.sh <version>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

APP="dist/EchosMonitor.app"
[[ -d "$APP" ]] || { echo "missing $APP — run scripts/build.sh first" >&2; exit 1; }

STAGE="build/dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

OUT="dist/EchosMonitor-${VERSION}.dmg"
rm -f "$OUT"
echo ">> building $OUT"
hdiutil create \
  -volname "EchosMonitor ${VERSION}" \
  -srcfolder "$STAGE" \
  -fs HFS+ \
  -format UDZO \
  -ov \
  "$OUT"
echo ">> dmg OK -> $OUT"
