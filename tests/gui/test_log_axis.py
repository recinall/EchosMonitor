"""DecimatedLogAxisItem (FIX 1) — log-axis tick labels are decimated.

Asserts the observable (rule 10): across a representative band the rendered
tick-label set is sparse (decades + the 2x/5x mantissas), NOT a label on
every minor tick — which is what made the 5-10 / 50-100 Hz labels collide.
"""

from __future__ import annotations

import numpy as np

from echosmonitor.gui.widgets.log_axis import DecimatedLogAxisItem

# 1..9 mantissa minor ticks across 0.1-100 Hz (the dense set pyqtgraph emits).
_TICKS = [m * d for d in (0.1, 1.0, 10.0) for m in range(1, 10)] + [100.0]


def _labels(axis: DecimatedLogAxisItem) -> list[str]:
    return axis.logTickStrings([np.log10(f) for f in _TICKS], 1.0, 1.0)


def test_labels_are_decimated(qtbot) -> None:
    axis = DecimatedLogAxisItem(orientation="bottom")
    labels = _labels(axis)
    labelled = [lbl for lbl in labels if lbl]
    blanked = [lbl for lbl in labels if not lbl]
    # Far fewer labels than ticks (every minor tick must NOT be labelled).
    assert len(labelled) < len(labels) / 2
    assert len(blanked) > 0


def test_decades_and_2_5_are_labelled(qtbot) -> None:
    axis = DecimatedLogAxisItem(orientation="bottom")
    labelled = {lbl for lbl in _labels(axis) if lbl}
    # Decades + the 2x / 5x mantissas get labels; 3,4,6,7,8,9 do not.
    assert {"0.1", "1", "10", "100", "2", "5", "20", "50"} <= labelled
    assert "3" not in labelled
    assert "70" not in labelled


def test_nonpositive_and_nonfinite_are_blank(qtbot) -> None:
    axis = DecimatedLogAxisItem(orientation="bottom")
    out = axis.logTickStrings([float("-inf"), float("nan"), np.log10(2.0)], 1.0, 1.0)
    assert out[0] == "" and out[1] == ""
    assert out[2] == "2"
