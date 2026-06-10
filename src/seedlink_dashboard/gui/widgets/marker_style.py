"""Shared detection-marker colours (CLAUDE.md rule 10).

Phase colour is ONE concept that lives in FOUR widgets: the trace plot,
the spectrogram view, and their two fan-out wrappers (LiveTabs and
SpectrogramDock). To stop those twins from drifting, the colour map is
defined exactly once here and ``phase`` is threaded through the whole
marker chain. STA/LTA detections (no phase) keep the historic amber;
AI picks colour by phase (P blue / S red).
"""

from __future__ import annotations

STA_LTA_COLOR = "#e0a030"  # amber (existing STA/LTA marker colour)
P_COLOR = "#3a7bd5"  # blue
S_COLOR = "#d53a3a"  # red
DETECTION_COLOR = "#3a9d4a"  # green (EQTransformer detection segment, M10)
# M10 Stage C — the two learning agents' sentinels, each a distinct hue.
EVENT_COLOR = "#b84ad5"  # purple (heuristic classifier "event")
UNKNOWN_COLOR = "#8a8a8a"  # grey (heuristic classifier "unknown")
ANOMALY_COLOR = "#d56a1f"  # orange (autoencoder anomaly segment)


def marker_color(phase: str | None) -> str:
    """Return the marker colour hex for a detection ``phase``.

    Args:
        phase: ``"P"`` / ``"S"`` for AI picks, ``"detection"`` for an
            EQTransformer span-style detection, ``"event"`` / ``"unknown"``
            for the heuristic classifier, ``"anomaly"`` for the autoencoder
            anomaly segment, ``None`` for STA/LTA.

    Returns:
        Hex colour string. STA/LTA (``None`` or any unrecognised value)
        maps to amber so the existing behaviour is preserved.
    """
    if phase == "P":
        return P_COLOR
    if phase == "S":
        return S_COLOR
    if phase == "detection":
        return DETECTION_COLOR
    if phase == "event":
        return EVENT_COLOR
    if phase == "unknown":
        return UNKNOWN_COLOR
    if phase == "anomaly":
        return ANOMALY_COLOR
    return STA_LTA_COLOR
