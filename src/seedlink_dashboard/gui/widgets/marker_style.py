"""Shared detection-marker colour (CLAUDE.md rule 10).

The detection-marker colour is ONE concept that lives in FOUR widgets: the
trace plot, the spectrogram view, and their two fan-out wrappers (LiveTabs
and SpectrogramDock). To stop those twins from drifting, the colour is
defined exactly once here. STA/LTA detections keep the historic amber.
"""

from __future__ import annotations

STA_LTA_COLOR = "#e0a030"  # amber (STA/LTA marker colour)
