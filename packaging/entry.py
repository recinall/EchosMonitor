"""PyInstaller bootstrap entry point (M7-B).

A thin shim so the spec has a concrete script to analyze; it just defers to the
real CLI in :mod:`echosmonitor.__main__`. Keeping it separate from the package
means the frozen launcher and the ``echosmonitor`` console-script share one
implementation.
"""

from __future__ import annotations

from echosmonitor.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
