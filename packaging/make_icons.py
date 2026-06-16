"""Generate the per-platform app icons from the master PNG (M7-C2).

PyInstaller wants a ``.ico`` on Windows and a ``.icns`` on macOS; it ignores
PNG icons there (the runtime QApplication icon is still set from the PNG via
``gui/resources.app_icon()``). The master is the 2048x2048 RGBA
``src/echosmonitor/resources/EchosMonitor.png``; this regenerates the derived
icons into ``packaging/icons/`` so the logo has one source of truth.

Run after changing the logo, then commit the regenerated icons:

    uv run python packaging/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

_HERE = Path(__file__).resolve().parent
_MASTER = _HERE.parent / "src" / "echosmonitor" / "resources" / "EchosMonitor.png"
_OUT = _HERE / "icons"

# Windows .ico carries several sizes so the shell picks the crisp one per
# context (16 = tray, 256 = large icons). macOS .icns wants power-of-two sizes
# up to 1024; Pillow's ICNS writer derives the set from the largest provided.
_ICO_SIZES = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
_ICNS_SIZE = (1024, 1024)


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    master = Image.open(_MASTER).convert("RGBA")

    ico_path = _OUT / "EchosMonitor.ico"
    master.save(ico_path, format="ICO", sizes=_ICO_SIZES)
    print(f"wrote {ico_path} ({ico_path.stat().st_size} bytes)")

    icns_path = _OUT / "EchosMonitor.icns"
    # ICNS requires a square; Pillow synthesises the smaller members itself.
    master.resize(_ICNS_SIZE, Image.LANCZOS).save(icns_path, format="ICNS")
    print(f"wrote {icns_path} ({icns_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
