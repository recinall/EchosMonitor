"""Plot theming from ``ui.theme`` (M6 settings dialog).

The schema has carried ``ui.theme`` since M0; this module finally
consumes it. Scope is deliberately the PLOTS only: pyqtgraph's global
background/foreground (every TracePlot, PSD, spectrogram axis, HVSR
curve picks these up at construction). The Qt widget chrome keeps
following the system palette — restyling all widgets is a much bigger
(and fragile) surface for no field value.

Applied ONCE at bootstrap, before any plot widget is constructed —
pyqtgraph reads the config options at item creation, so a runtime
switch would leave every existing plot in the old colors. The settings
dialog therefore labels the theme (like the other settings) as
"applies at the next launch".
"""

from __future__ import annotations

import pyqtgraph as pg
import structlog

_log = structlog.get_logger(__name__)


def apply_theme(theme: str) -> None:
    """Set pyqtgraph's global colors for ``theme`` ("dark" | "light").

    "dark" is the historical default (pyqtgraph's own black background);
    anything unrecognized falls back to it — the schema's Literal type
    already rejects other values at load time, this is belt-and-braces.
    """
    if theme == "light":
        pg.setConfigOptions(background="w", foreground="k")
    else:
        pg.setConfigOptions(background="k", foreground="d")
    _log.info("theme_applied", theme=theme)


__all__ = ["apply_theme"]
