"""SeedLink Dashboard — realtime seismic monitoring."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("seedlink-dashboard")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"

__all__ = ["__version__"]
