"""Custom exception hierarchy for the streaming subsystem."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from echosmonitor.core.models import EchosErrorKind, FailureKind


class EchosMonitorError(Exception):
    """Base for all errors raised by this application."""


class SeedLinkError(EchosMonitorError):
    """Generic SeedLink-layer failure."""


class SeedLinkConnectionError(SeedLinkError):
    """Network-level connection failure (DNS, TCP, socket reset)."""


class SeedLinkProtocolError(SeedLinkError):
    """Server replied in a way that violates the SeedLink protocol."""


class ConfigError(EchosMonitorError):
    """Malformed or inconsistent configuration."""


class HvsrError(EchosMonitorError):
    """HVSR (H/V spectral ratio) analysis failure.

    Raised by :mod:`core.hvsr` when accumulated windows are inconsistent
    (mismatched component length or sample rate), when too few windows
    exist to compute a curve, or when the underlying ``hvsrpy`` workflow
    fails. A distinct type lets the GUI surface a "cannot compute HVSR"
    state without parsing message text.
    """


class ResponseError(EchosMonitorError):
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


# ----------------------------------------------------------------------
# Echos REST API (M1 stage A) — core/echos_api.py.
# ----------------------------------------------------------------------


class EchosApiError(EchosMonitorError):
    """Base class for Echos REST client failures.

    Carries a closed-set ``kind`` (``core.models.EchosErrorKind``) so the
    device dialog and the status poller branch deterministically without
    inspecting message text (skill: echos-rest-api). Messages never
    contain credentials (rule 15).

    Attributes:
        kind: One of ``"auth_failed" | "locked_out" | "unreachable" |
            "timeout" | "protocol"``.
    """

    __slots__ = ("kind",)

    def __init__(self, kind: EchosErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: EchosErrorKind = kind


# Names below intentionally omit the ``Error`` suffix, matching the Info*
# family above; N818 suppressed locally for the same reason.
class EchosAuthFailed(EchosApiError):  # noqa: N818
    """The device rejected the admin credentials (HTTP 401), or a write
    was attempted with no password configured. Always ``kind == "auth_failed"``.
    """

    def __init__(self, message: str) -> None:
        super().__init__("auth_failed", message)


class EchosLockedOut(EchosApiError):  # noqa: N818
    """The device's auth lockout is active (HTTP 429 + ``Retry-After``).

    Also raised by the client-side guard that fast-fails authenticated
    requests before the known lockout window has expired, so the app
    never hammers a locked device (rule 15).

    Attributes:
        retry_after_s: Seconds remaining until the device accepts
            authenticated requests again (from the ``Retry-After`` header,
            or the remaining client-side window on a guard fast-fail).
    """

    __slots__ = ("retry_after_s",)

    def __init__(self, retry_after_s: float, message: str) -> None:
        super().__init__("locked_out", message)
        self.retry_after_s: float = retry_after_s


class EchosUnreachable(EchosApiError):  # noqa: N818
    """Network-level failure (DNS, refused, reset) before any HTTP response.

    Always ``kind == "unreachable"``.
    """

    def __init__(self, message: str) -> None:
        super().__init__("unreachable", message)


class EchosTimeout(EchosApiError):  # noqa: N818
    """A connect/read deadline elapsed (rule 7 bound), including the bounded
    wait for the seedlink hot-reload restart poll. Always ``kind == "timeout"``.
    """

    def __init__(self, message: str) -> None:
        super().__init__("timeout", message)


class EchosApiProtocolError(EchosApiError):
    """The device answered, but not in the expected shape.

    Unexpected status code, non-JSON body, or a payload that fails pydantic
    validation. Always ``kind == "protocol"``.
    """

    def __init__(self, message: str) -> None:
        super().__init__("protocol", message)
