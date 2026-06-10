"""A log-frequency :class:`pyqtgraph.AxisItem` that decimates tick labels.

pyqtgraph's default log axis labels too many minor ticks for the available
width — at a tab's real widths the 5-10 Hz and 50-100 Hz minor-tick labels
collide and run together. This axis keeps the gridlines but labels ONLY the
decades (…, 0.1, 1, 10, 100, …) plus a sparse readable subset of
intermediate ticks (the 2· and 5· mantissas); every other minor tick gets an
empty label, so labels never collide.

Shared by every log-frequency plot — the HVSR H/V and PSD plots and the
standalone PSD tab — so the decimation is defined once.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg

# Mantissas (within a decade) that get a text label; all others are blanked.
_LABELLED_MANTISSAS = (1, 2, 5)


def _format_frequency(value: float) -> str:
    """Compact label for a frequency tick (e.g. ``0.2``, ``1``, ``10``, ``50``)."""
    if value <= 0:
        return ""
    # ``%g`` renders 0.1/0.2/0.5/1/2/5/10/20/50/100 without trailing zeros.
    return f"{value:g}"


class DecimatedLogAxisItem(pg.AxisItem):  # type: ignore[misc]  # pg is untyped
    """Log axis labelling only decades + the 2x/5x mantissa ticks."""

    def logTickStrings(  # noqa: N802 — overrides pyqtgraph's mixedCase method
        self, values: list[float], scale: float, spacing: float
    ) -> list[str]:
        """Format log-space tick values, blanking all but decade/2x/5x ticks.

        ``values`` are ``log10(frequency)``. A tick is labelled only when its
        mantissa (``f / 10**floor(log10 f)``) rounds to 1, 2 or 5.
        """
        out: list[str] = []
        for v in values:
            f = 10.0**v
            if f <= 0 or not np.isfinite(f):
                out.append("")
                continue
            decade = 10.0 ** np.floor(np.log10(f) + 1e-9)
            mantissa = round(f / decade)
            out.append(_format_frequency(f) if mantissa in _LABELLED_MANTISSAS else "")
        return out
