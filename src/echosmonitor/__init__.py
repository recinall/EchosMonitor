"""EchosMonitor — realtime seismic monitoring."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _resolve_version() -> str:
    """Resolve the package version with a frozen-app-safe fallback chain (M7-A).

    1. ``importlib.metadata`` — works for installed / editable (``uv sync``)
       checkouts and for PyInstaller bundles that collect the dist-info.
    2. the hatch-vcs generated ``_version.py`` — written at build time, so a
       frozen app that did not collect metadata still has a real version.
    3. ``"0.0.0+dev"`` — a source checkout that was never built or installed.
    """
    try:
        return version("echosmonitor")
    except PackageNotFoundError:
        pass
    try:
        from echosmonitor._version import __version__ as _vcs_version
    except ImportError:
        return "0.0.0+dev"
    return _vcs_version


__version__ = _resolve_version()

__all__ = ["__version__"]
