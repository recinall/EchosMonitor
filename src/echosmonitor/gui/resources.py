"""GUI resource access (M7-A).

Loads packaged binary resources (the application icon) via
``importlib.resources`` so they resolve identically from a source checkout, an
installed wheel, and a PyInstaller one-dir bundle (where the data file is
collected next to the package). Reading the bytes — rather than handing Qt a
filesystem path — keeps it correct even when the package is imported from a
zip, and never leaves a path-lifetime trap.
"""

from __future__ import annotations

from importlib.resources import files

from PySide6.QtGui import QIcon, QPixmap

_ICON_RESOURCE = "EchosMonitor.png"


def app_icon() -> QIcon:
    """Return the application icon, or an empty :class:`QIcon` if unavailable.

    Never raises: a missing or undecodable icon must not stop the app from
    launching (it is pure branding, not a functional dependency).
    """
    try:
        data = (files("echosmonitor") / "resources" / _ICON_RESOURCE).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return QIcon()
    pixmap = QPixmap()
    if not pixmap.loadFromData(data):
        return QIcon()
    return QIcon(pixmap)
