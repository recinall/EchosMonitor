"""BUG 1 regression: the Devices tree must keep the Stream (NSLC) column
readable, not starve it behind the wide Stats column.

The child rows were always *created* (``_stream_count_for_test`` already
asserted that), but column 0 used to be ``Stretch`` while State/Diagnostics/
Stats were ``ResizeToContents`` — so a wide Stats string collapsed column 0
to ~38px and the NSLC clipped to nothing, making the rows look absent. These
tests assert the observable invariant (rule 10): the Stream column is wide
enough to actually show the NSLC text at a realistic dock width.
"""

from __future__ import annotations

from PySide6.QtGui import QFontMetrics

from echosmonitor.core.models import ConnState, DeviceStatus
from echosmonitor.gui.widgets.device_panel import DevicePanel

_NSLCS = ["XX.ECHOS.00.HHZ", "XX.ECHOS.00.HHN", "XX.ECHOS.00.HHE"]


def _wide_status(name: str) -> DeviceStatus:
    """A status whose Stats column renders a long string (the starver)."""
    return DeviceStatus(
        name=name,
        state=ConnState.CONNECTED,
        packets_received=123456,
        bytes_received=98_765_432,
        archive_enabled=True,
        archive_bytes_written=5_000_000,
        archive_files_open=3,
    )


def test_stream_column_shows_nslc_at_realistic_width(qtbot) -> None:
    panel = DevicePanel()
    qtbot.addWidget(panel)
    panel.resize(320, 400)  # the default side-dock width
    panel.show()
    qtbot.waitExposed(panel)

    panel.set_status_provider(lambda: {"ECHOS": _wide_status("ECHOS")})
    panel.on_device_state("ECHOS", int(ConnState.CONNECTED))
    for nslc in _NSLCS:
        panel.on_new_stream("ECHOS", nslc)
    qtbot.wait(10)

    # The rows exist (the create path) ...
    assert panel._stream_count_for_test("ECHOS") == 3

    # ... and the Stream column is wide enough to actually SHOW the NSLC,
    # not the ~38px sliver the old Stretch-vs-ResizeToContents layout gave.
    tree = panel._tree
    header = tree.header()
    col0 = header.sectionSize(0)
    needed = QFontMetrics(tree.font()).horizontalAdvance(max(_NSLCS, key=len))
    assert col0 >= needed, f"Stream column {col0}px too narrow for NSLC (~{needed}px)"


def test_stream_column_survives_a_squeezed_dock(qtbot) -> None:
    """Even at the dock's 220px minimum the NSLC stays visible (the
    Stats column yields instead of starving the Stream column)."""
    panel = DevicePanel()
    qtbot.addWidget(panel)
    panel.resize(220, 400)
    panel.show()
    qtbot.waitExposed(panel)

    panel.set_status_provider(lambda: {"ECHOS": _wide_status("ECHOS")})
    panel.on_device_state("ECHOS", int(ConnState.CONNECTED))
    for nslc in _NSLCS:
        panel.on_new_stream("ECHOS", nslc)
    qtbot.wait(10)

    header = panel._tree.header()
    needed = QFontMetrics(panel._tree.font()).horizontalAdvance(max(_NSLCS, key=len))
    assert header.sectionSize(0) >= needed
