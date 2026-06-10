"""Custom exception hierarchy for the streaming subsystem."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from seedlink_dashboard.core.models import FailureKind


class SeedLinkDashboardError(Exception):
    """Base for all errors raised by this application."""


class SeedLinkError(SeedLinkDashboardError):
    """Generic SeedLink-layer failure."""


class SeedLinkConnectionError(SeedLinkError):
    """Network-level connection failure (DNS, TCP, socket reset)."""


class SeedLinkProtocolError(SeedLinkError):
    """Server replied in a way that violates the SeedLink protocol."""


class ConfigError(SeedLinkDashboardError):
    """Malformed or inconsistent configuration."""


class HvsrError(SeedLinkDashboardError):
    """HVSR (H/V spectral ratio) analysis failure.

    Raised by :mod:`core.hvsr` when accumulated windows are inconsistent
    (mismatched component length or sample rate), when too few windows
    exist to compute a curve, or when the underlying ``hvsrpy`` workflow
    fails. A distinct type lets the GUI surface a "cannot compute HVSR"
    state without parsing message text.
    """


class ResponseError(SeedLinkDashboardError):
    """Instrument-response deconvolution failure.

    Raised by :mod:`core.response` when inventory metadata cannot be
    loaded, when a trace has no matching response for its NSLC/time, or
    when a window is unfit for deconvolution (e.g. masked gaps). Carrying
    a distinct type lets the GUI surface a "cannot show physical units"
    state without inspecting message text.
    """


# ----------------------------------------------------------------------
# INFO subsystem (M4 stage A) — out-of-band server queries.
# ----------------------------------------------------------------------


class InfoError(SeedLinkError):
    """Base class for INFO-fetch failures.

    Carries a closed-set ``kind`` describing the network-level cause so
    callers (and the GUI's diagnostics panel) can branch deterministically
    without inspecting message text.

    Attributes:
        kind: One of ``"timeout" | "refused" | "dns" | "unknown"``.
    """

    __slots__ = ("kind",)

    def __init__(self, kind: FailureKind, message: str) -> None:
        super().__init__(message)
        self.kind: FailureKind = kind


# Names below intentionally omit the ``Error`` suffix: callers refer to
# them as ``InfoTimeout`` / ``InfoCanceled`` in API surface and tests
# (see core/info.py docstrings), and renaming would force a rippling
# rename across the M4 stage A spec. Suppress the N818 lint locally
# rather than project-wide.
class InfoTimeout(InfoError):  # noqa: N818
    """The wall-clock deadline elapsed before the server finished responding.

    Always carries ``kind == "timeout"`` so the caller may treat it as a
    specialisation of the closed FailureKind set.
    """

    def __init__(self, message: str) -> None:
        super().__init__("timeout", message)


class InfoCanceled(SeedLinkError):  # noqa: N818
    """The fetch was canceled via its ``CancellationToken`` before completing.

    Distinct from ``InfoTimeout`` so callers can suppress logging and UI
    feedback for explicit user-initiated cancels (the typical case is the
    Stations dock closing while a fetch is in flight).
    """


class InfoProtocolError(SeedLinkProtocolError):
    """The server's INFO response could not be parsed as valid XML.

    Surfaces malformed XML, unexpected document roots, or missing elements
    that prevent constructing the typed dataclasses returned to the caller.
    """
