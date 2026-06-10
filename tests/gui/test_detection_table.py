"""Tests for the M8 detection table (model / proxy / widget).

Per CLAUDE.md rule 10 these assert observable behavior: the open
duration actually advances then freezes, filtering actually removes
rows from the *view* while leaving the source model intact, and a
double-click actually emits the focus signal with the right detection.
``now`` is injected so the ticking is deterministic.
"""

from __future__ import annotations

import pytest
from obspy import UTCDateTime
from PySide6.QtCore import QModelIndex, Qt

from echosmonitor.core.models import Detection
from echosmonitor.gui.widgets.detection_table import (
    DetectionColumn,
    DetectionTable,
)


class _Clock:
    """Mutable injected clock so duration ticks are deterministic."""

    def __init__(self, t: UTCDateTime) -> None:
        self.t = t

    def __call__(self) -> UTCDateTime:
        return self.t


def _det(
    t_on: str,
    t_off: str | None,
    score: float,
    *,
    device: str = "dev",
    nslc: str = "IU.ANMO.00.BHZ",
    det_id: int = 1,
    kind: str = "sta_lta",
) -> Detection:
    return Detection(
        device=device,
        nslc=nslc,
        kind=kind,
        t_on=UTCDateTime(t_on),
        t_off=UTCDateTime(t_off) if t_off is not None else None,
        score=score,
        detected_at=UTCDateTime(t_on),
        meta={"on_thr": 3.5},
        id=det_id,
    )


def _display(table: DetectionTable, source_row: int, col: DetectionColumn) -> str:
    model = table._model_for_test()
    return str(model.data(model.index(source_row, int(col)), Qt.ItemDataRole.DisplayRole))


def test_recorded_detection_inserts_row_with_fields(qtbot) -> None:
    table = DetectionTable()
    qtbot.addWidget(table)
    table.on_detection_recorded(_det("2026-06-01T00:00:00", "2026-06-01T00:00:03", 7.2))

    model = table._model_for_test()
    assert model.rowCount() == 1
    assert _display(table, 0, DetectionColumn.NSLC) == "IU.ANMO.00.BHZ"
    assert _display(table, 0, DetectionColumn.KIND) == "sta_lta"
    assert _display(table, 0, DetectionColumn.SCORE) == "7.20"
    assert _display(table, 0, DetectionColumn.DURATION) == "3.0s"


def test_open_duration_ticks_then_freezes(qtbot) -> None:
    clock = _Clock(UTCDateTime("2026-06-01T00:00:02"))
    table = DetectionTable(now_provider=clock)
    qtbot.addWidget(table)

    table.on_detection_recorded(_det("2026-06-01T00:00:00", None, 4.0, det_id=42))
    # Open: duration shows elapsed-since-onset at the injected "now".
    assert _display(table, 0, DetectionColumn.DURATION) == "open 2s"

    # Advance the clock and tick: the open duration must grow.
    clock.t = UTCDateTime("2026-06-01T00:00:09")
    table._model_for_test().tick()
    assert _display(table, 0, DetectionColumn.DURATION) == "open 9s"

    # Close it: duration freezes to the final span, no longer "open".
    table.on_detection_updated(_det("2026-06-01T00:00:00", "2026-06-01T00:00:05", 9.5, det_id=42))
    frozen = _display(table, 0, DetectionColumn.DURATION)
    assert frozen == "5.0s"
    # Further clock advances do NOT change a closed row.
    clock.t = UTCDateTime("2026-06-01T01:00:00")
    table._model_for_test().tick()
    assert _display(table, 0, DetectionColumn.DURATION) == "5.0s"


def test_sort_by_score_descending(qtbot) -> None:
    table = DetectionTable()
    qtbot.addWidget(table)
    for i, sc in enumerate((4.0, 9.0, 1.0)):
        table.on_detection_recorded(
            _det(f"2026-06-01T00:00:0{i}", f"2026-06-01T00:00:0{i + 1}", sc, det_id=i + 1)
        )
    proxy = table._proxy_for_test()
    proxy.sort(int(DetectionColumn.SCORE), Qt.SortOrder.DescendingOrder)
    scores = [
        float(proxy.index(r, int(DetectionColumn.SCORE)).data(Qt.ItemDataRole.DisplayRole))
        for r in range(proxy.rowCount())
    ]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(9.0)


def test_filters_are_view_side_only(qtbot) -> None:
    table = DetectionTable()
    qtbot.addWidget(table)
    table.on_detection_recorded(_det("2026-06-01T00:00:00", "2026-06-01T00:00:01", 2.0, det_id=1))
    table.on_detection_recorded(_det("2026-06-01T00:00:02", "2026-06-01T00:00:03", 9.0, det_id=2))
    model = table._model_for_test()
    proxy = table._proxy_for_test()
    assert model.rowCount() == 2
    assert proxy.rowCount() == 2

    # Min-score filter hides the low-score row in the VIEW only.
    proxy.set_min_score(5.0)
    assert proxy.rowCount() == 1
    assert model.rowCount() == 2  # source model untouched (hard rule)

    proxy.set_min_score(0.0)
    assert proxy.rowCount() == 2


def test_device_filter(qtbot) -> None:
    table = DetectionTable()
    qtbot.addWidget(table)
    table.on_detection_recorded(_det("2026-06-01T00:00:00", None, 4.0, device="a", det_id=1))
    table.on_detection_recorded(_det("2026-06-01T00:00:01", None, 4.0, device="b", det_id=2))
    proxy = table._proxy_for_test()
    proxy.set_device("a")
    assert proxy.rowCount() == 1
    src = proxy.mapToSource(proxy.index(0, 0))
    assert table._model_for_test().detection_for_source_row(src.row()).device == "a"


def test_time_window_filter_excludes_old(qtbot) -> None:
    clock = _Clock(UTCDateTime("2026-06-01T12:00:00"))
    table = DetectionTable(now_provider=clock)
    qtbot.addWidget(table)
    # One 10 minutes ago (in window), one 2 hours ago (outside 1h window).
    table.on_detection_recorded(_det("2026-06-01T11:50:00", None, 4.0, det_id=1))
    table.on_detection_recorded(_det("2026-06-01T10:00:00", None, 4.0, det_id=2))
    proxy = table._proxy_for_test()
    assert proxy.rowCount() == 2
    proxy.set_window(3600.0)  # last 1h
    assert proxy.rowCount() == 1


def test_no_kind_filter_mixed_kinds_all_visible(qtbot) -> None:
    """M0 regression (rule 12): the AI-pick kind filter is gone. Rows of
    ANY kind pass the proxy untouched, the proxy exposes no kind-filter
    API, and the toolbar offers no kind combo (no "AI picks" option
    anywhere)."""
    from PySide6.QtWidgets import QComboBox

    table = DetectionTable()
    qtbot.addWidget(table)
    table.on_detection_recorded(_det("2026-06-01T00:00:00", None, 4.0, det_id=1))
    table.on_detection_recorded(
        _det("2026-06-01T00:00:01", None, 4.0, det_id=2, kind="future_detector")
    )
    proxy = table._proxy_for_test()
    assert proxy.rowCount() == 2  # nothing filters on kind
    assert not hasattr(proxy, "set_sta_lta_only")

    combo_items = {
        combo.itemText(i)
        for combo in table.findChildren(QComboBox)
        for i in range(combo.count())
    }
    assert "AI picks" not in combo_items


def test_kind_cell_has_no_background_tint(qtbot) -> None:
    """M0 regression (rule 12): the phase-coloured Kind-cell tint left with
    the AI subsystem — BackgroundRole is unset for every kind, sta_lta and
    non-sta_lta alike."""
    table = DetectionTable()
    qtbot.addWidget(table)
    table.on_detection_recorded(_det("2026-06-01T00:00:00", None, 4.0, det_id=1))
    table.on_detection_recorded(
        _det("2026-06-01T00:00:01", None, 4.0, det_id=2, kind="future_detector")
    )
    model = table._model_for_test()
    for row in range(model.rowCount()):
        idx = model.index(row, int(DetectionColumn.KIND))
        assert model.data(idx, Qt.ItemDataRole.BackgroundRole) is None


def test_double_click_emits_focus_with_detection(qtbot) -> None:
    table = DetectionTable()
    qtbot.addWidget(table)
    det = _det("2026-06-01T00:00:00", "2026-06-01T00:00:02", 5.0, det_id=7)
    table.on_detection_recorded(det)
    proxy = table._proxy_for_test()
    idx: QModelIndex = proxy.index(0, 0)
    with qtbot.waitSignal(table.focusDetectionRequested, timeout=1000) as blocker:
        table._on_double_clicked(idx)
    emitted = blocker.args[0]
    assert isinstance(emitted, Detection)
    assert emitted.id == 7
    assert emitted.nslc == "IU.ANMO.00.BHZ"


def test_historical_rows_flagged_dimmed(qtbot) -> None:
    table = DetectionTable()
    qtbot.addWidget(table)
    table.load_historical([_det("2026-06-01T00:00:00", "2026-06-01T00:00:01", 4.0, det_id=1)])
    model = table._model_for_test()
    fg = model.data(model.index(0, 0), Qt.ItemDataRole.ForegroundRole)
    assert fg is not None  # dimmed foreground brush set for historical rows


def test_delta_from_previous_on_same_stream(qtbot) -> None:
    table = DetectionTable()
    qtbot.addWidget(table)
    table.on_detection_recorded(_det("2026-06-01T00:00:00", "2026-06-01T00:00:01", 4.0, det_id=1))
    table.on_detection_recorded(_det("2026-06-01T00:00:10", "2026-06-01T00:00:11", 4.0, det_id=2))
    # Second detection on the same stream is +10s after the first.
    assert _display(table, 1, DetectionColumn.DELTA) == "+10.0s"
    # First has no predecessor.
    assert _display(table, 0, DetectionColumn.DELTA) == ""


def test_historical_load_computes_delta(qtbot) -> None:
    """load_historical receives newest-first rows (as the DAO returns
    them) yet must still compute Δ-from-previous correctly."""
    table = DetectionTable()
    qtbot.addWidget(table)
    newest_first = [
        _det("2026-06-01T00:00:10", "2026-06-01T00:00:11", 4.0, det_id=2),
        _det("2026-06-01T00:00:00", "2026-06-01T00:00:01", 4.0, det_id=1),
    ]
    table.load_historical(newest_first)
    model = table._model_for_test()
    # Find the source row for the later detection (id=2) and check its Δ.
    deltas = {
        model.detection_for_source_row(r).id: model.data(
            model.index(r, int(DetectionColumn.DELTA)), Qt.ItemDataRole.DisplayRole
        )
        for r in range(model.rowCount())
    }
    assert deltas[2] == "+10.0s"
    assert deltas[1] == ""
