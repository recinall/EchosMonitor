"""Shared pane-header styling for Live / Spectrogram / PSD panes (M7 C2).

Before M7 Stage C each pane (``TracePlot`` title, ``SpectrogramView``
title, ``PsdWidget`` plot label) styled and formatted its header
independently — different font sizes, different ``NSLC — fs`` wordings,
inconsistent margins. This module is the single source of truth so the
three panes read as one visual family:

* :data:`PANE_TITLE_STYLE` — the stylesheet applied to every pane title
  ``QLabel`` (consistent font size + weight, dim secondary colour).
* :data:`PANE_HEADER_MARGINS` — the ``(l, t, r, b)`` content margins for
  a pane's header row layout.
* :func:`format_pane_title` — the canonical ``NSLC — <fs> Hz`` (or, for
  the stacked raw/filtered trace, ``NSLC — <fs_raw> / <fs_proc> Hz``)
  string.

Pure presentation: no Qt widgets are constructed here, no state is held.
"""

from __future__ import annotations

# Pane title object name shared by every pane title QLabel. Kept stable so
# tests and a future global stylesheet can target all pane titles at once.
PANE_TITLE_OBJECT_NAME = "PaneHeaderTitle"

# Title font size in points. One value across Live / Spectrogram / PSD so
# the three panes' headers line up visually.
_PANE_TITLE_FONT_PT = 11

# Stylesheet applied to every pane title QLabel. Bold + a slightly larger
# point size makes the stream name the clear focal point of each pane;
# the colour stays neutral so it does not fight the plot content.
PANE_TITLE_STYLE = (
    f"QLabel#{PANE_TITLE_OBJECT_NAME} {{"
    f" font-size: {_PANE_TITLE_FONT_PT}pt; font-weight: bold; color: #d8dde3; }}"
)

# Content margins (left, top, right, bottom) for a pane header row. Tighter
# than Qt's default so the header does not steal vertical pixels from the
# plot below it.
PANE_HEADER_MARGINS: tuple[int, int, int, int] = (6, 3, 6, 3)


def format_pane_title(label: str, fs: float, fs_processed: float | None = None) -> str:
    """Return the canonical ``NSLC — <fs> Hz`` pane title.

    Args:
        label: Stream label, typically the NSLC string.
        fs: Primary (raw) sample rate in Hz.
        fs_processed: Optional post-DSP sample rate. When supplied (the
            stacked raw/filtered trace) the title becomes
            ``NSLC — <fs> / <fs_processed> Hz``.

    Returns:
        The formatted header string.
    """
    if fs_processed is not None:
        return f"{label} — {fs:g} / {fs_processed:g} Hz"
    return f"{label} — {fs:g} Hz"
