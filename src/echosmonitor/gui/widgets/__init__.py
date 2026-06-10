"""GUI widgets package.

Imports here also configure pyqtgraph globally so the application picks
deterministic defaults regardless of import order. OpenGL is left off by
default — it requires a working GLX driver and is not needed at the
target throughput (16 channels x 100 Hz).
"""

from __future__ import annotations

import pyqtgraph as pg

pg.setConfigOptions(antialias=False, useOpenGL=False)
