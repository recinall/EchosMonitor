#!/usr/bin/env bash
# Build a one-dir PyInstaller bundle of EchosMonitor (M7-B). Linux / macOS.
#
# Output: dist/echosmonitor/echosmonitor  (a self-contained launcher).
# Requires the dev dependency group (pyinstaller); `uv sync` installs it.
#
# Usage:
#   ./scripts/build.sh            # clean build
#   ./scripts/build.sh --no-clean # keep previous build/ cache
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLEAN=1
for arg in "$@"; do
  case "$arg" in
    --no-clean) CLEAN=0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$CLEAN" == "1" ]]; then
  echo ">> cleaning build/ and dist/echosmonitor"
  rm -rf build dist/echosmonitor
fi

echo ">> ensuring build deps are installed (uv sync)"
uv sync

echo ">> running PyInstaller (packaging/echosmonitor.spec)"
uv run pyinstaller packaging/echosmonitor.spec --noconfirm --workpath build --distpath dist

BIN="dist/echosmonitor/echosmonitor"
echo ">> smoke: $BIN --version (headless)"
QT_QPA_PLATFORM=offscreen "$BIN" --version

# --check is the real gate: it constructs config + the main window in the
# freeze and exits by code (no stdout dependency), so it works identically on
# the GUI-subsystem builds (Windows) where --version has nowhere to print.
echo ">> smoke: $BIN --check (headless start/quit)"
QT_QPA_PLATFORM=offscreen "$BIN" --check >/dev/null

echo ">> build OK -> dist/echosmonitor/"
