"""PyInstaller runtime hook — fix obspy's version-file lookup in a freeze (M7-B).

``obspy.core.util.version`` derives ``OBSPY_ROOT`` (and thus the path to its
``RELEASE-VERSION`` data file) from::

    inspect.getfile(inspect.currentframe())

Under PyInstaller a frozen module's filename is RELATIVE to the bundle root, so
``os.path.abspath`` of it resolves against the *current working directory*
instead of the bundle. ``OBSPY_ROOT`` then points at ``<cwd>/obspy``; obspy
fails to read its bundled ``RELEASE-VERSION``, runs ``git describe`` (matching
when the cwd happens to be a git repo), and finally tries to *write*
``<cwd>/obspy/RELEASE-VERSION`` — which does not exist — crashing the app at
``import obspy`` with FileNotFoundError.

This hook runs before any application import. It makes ``inspect.getfile``
return the bundle-rooted path for such relative frozen filenames, so
``OBSPY_ROOT`` lands inside the bundle (where the spec collects RELEASE-VERSION)
and obspy reads its version cleanly and never tries to write.

Note the source ``.py``/``.pyc`` modules live *inside* the PYZ archive, not on
disk — only data files are extracted under ``_MEIPASS``. So the guard cannot
test the module file itself; it rewrites only when the relative path's
top-level package directory actually exists on disk under ``_MEIPASS`` (e.g.
``_MEIPASS/obspy`` exists because RELEASE-VERSION was collected there). That
keeps the patch a strict, conservative improvement for any package using this
same introspection idiom.
"""

import inspect
import os
import sys

_MEIPASS = getattr(sys, "_MEIPASS", None)

if _MEIPASS:
    _orig_getfile = inspect.getfile

    def _getfile(obj: object) -> str:
        path = _orig_getfile(obj)
        if not os.path.isabs(path):
            top = path.replace("\\", "/").split("/", 1)[0]
            if top and os.path.isdir(os.path.join(_MEIPASS, top)):
                return os.path.join(_MEIPASS, path)
        return path

    inspect.getfile = _getfile  # type: ignore[assignment]
