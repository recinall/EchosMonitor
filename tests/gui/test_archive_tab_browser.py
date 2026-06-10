"""Archive tab — browser (Stage A).

The browser shows the real recorded extent, defaults the load interval to a
recent slice **within** that extent (never an epoch/1999 placeholder), shows an
honest empty state when nothing is archived, and emits the exact
``(device, group, t_start, t_end)`` the user selected.
"""

from __future__ import annotations

from pathlib import Path

from obspy import UTCDateTime
from PySide6.QtCore import QDateTime, QObject, QTimeZone, Signal

from echosmonitor.core.models import device_stream_key
from echosmonitor.gui.widgets.archive_tab import ArchiveTab
from echosmonitor.storage.dao import ArchiveDao

_T0 = UTCDateTime("2026-05-10T12:00:00")
_FS = 100.0
_STA = "XX.STA.00.HH"


class _FakeEngine(QObject):
    newStreamSeen = Signal(str, str)  # noqa: N815
    devicesChanged = Signal()  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self._buffers: dict[str, object] = {}

    def add_stream(self, device: str, nslc: str) -> None:
        self._buffers[device_stream_key(device, nslc)] = object()
        self.devicesChanged.emit()
        self.newStreamSeen.emit(device, nslc)


def _add_3c(engine: _FakeEngine, device: str = "dev") -> dict[str, str]:
    group = {"Z": f"{_STA}Z", "N": f"{_STA}N", "E": f"{_STA}E"}
    for nslc in group.values():
        engine.add_stream(device, nslc)
    return group


def _seed_archive(tmp_path: Path, device: str, group: dict[str, str]) -> ArchiveDao:
    dao = ArchiveDao(tmp_path / "archive.db", batch_window_s=0.1)
    dev = dao.upsert_device(device, "h", 18000, {})
    for nslc in group.values():
        net, sta, loc, cha = nslc.split(".")
        sid_row = dao.upsert_stream(dev, (net, sta, loc, cha), _FS)
        # One hour of coverage with a gap in the middle.
        dao.record_file(sid_row, Path(f"/sds/{nslc}-a.mseed"), _T0, _T0 + 1800, 1024)
        dao.record_file(sid_row, Path(f"/sds/{nslc}-b.mseed"), _T0 + 2400, _T0 + 3600, 1024)
    return dao


def test_browser_shows_real_extent_and_default_within_extent(qtbot, tmp_path: Path) -> None:
    engine = _FakeEngine()
    group = _add_3c(engine)
    dao = _seed_archive(tmp_path, "dev", group)
    tab = ArchiveTab(engine, dao)  # type: ignore[arg-type]
    qtbot.addWidget(tab)

    # The extent label reflects the real recorded span, not a placeholder.
    text = tab.extent_text_for_test()
    assert "2026-05-10" in text
    assert "No archived data" not in text

    # The default interval falls strictly within [t_min, t_max] — never 1999.
    t_start, t_end = tab.interval_for_test()
    assert float(_T0.timestamp) <= t_start < t_end <= float((_T0 + 3600).timestamp)
    assert UTCDateTime(t_start).year == 2026
    assert tab.load_enabled_for_test()


def test_coverage_strip_models_gap(qtbot, tmp_path: Path) -> None:
    engine = _FakeEngine()
    group = _add_3c(engine)
    dao = _seed_archive(tmp_path, "dev", group)
    tab = ArchiveTab(engine, dao)  # type: ignore[arg-type]
    qtbot.addWidget(tab)

    # Force the strip to cover the full recorded span so the gap is visible.
    tab._start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(_T0.timestamp), QTimeZone.utc()))
    tab._end_edit.setDateTime(
        QDateTime.fromSecsSinceEpoch(int((_T0 + 3600).timestamp), QTimeZone.utc())
    )
    tab._update_coverage()
    _w_start, _w_end, intervals = tab._coverage.coverage_for_test()
    # Two covered intervals → one gap between them (2400..2400 region uncovered).
    assert len(intervals) == 2


def test_empty_archive_honest_state(qtbot, tmp_path: Path) -> None:
    engine = _FakeEngine()
    _add_3c(engine)  # streams exist live, but nothing archived
    dao = ArchiveDao(tmp_path / "empty.db", batch_window_s=0.1)
    dao.upsert_device("dev", "h", 18000, {})
    tab = ArchiveTab(engine, dao)  # type: ignore[arg-type]
    qtbot.addWidget(tab)

    assert "No archived data" in tab.extent_text_for_test()
    assert not tab.load_enabled_for_test()


def test_empty_state_names_archiving_disabled(qtbot, tmp_path: Path) -> None:
    """When the device has archiving disabled (the field-bug cause), the empty
    state says so — not a generic 'no data' — so the user knows WHY the archive
    is empty even though archive.db exists (full of detection metadata)."""
    from PySide6.QtCore import QObject, Signal

    from echosmonitor.config.schema import (
        ArchiveConfig,
        DeviceConfig,
        StreamSelectorConfig,
    )

    group = {"Z": f"{_STA}Z", "N": f"{_STA}N", "E": f"{_STA}E"}

    class _EngineWithConfig(QObject):
        newStreamSeen = Signal(str, str)  # noqa: N815
        devicesChanged = Signal()  # noqa: N815

        def __init__(self) -> None:
            super().__init__()
            self._buffers: dict[str, object] = {}
            for nslc in group.values():
                self._buffers[device_stream_key("dev", nslc)] = object()

        def devices(self):
            return (
                DeviceConfig(
                    name="dev",
                    host="h",
                    port=18000,
                    selectors=[StreamSelectorConfig(network="XX", station="STA")],
                    archive=ArchiveConfig(enabled=False),
                ),
            )

    engine = _EngineWithConfig()
    dao = ArchiveDao(tmp_path / "empty.db", batch_window_s=0.1)
    dao.upsert_device("dev", "h", 18000, {})
    tab = ArchiveTab(engine, dao)  # type: ignore[arg-type]
    qtbot.addWidget(tab)

    msg = tab.extent_text_for_test()
    assert "archiving is disabled" in msg.lower()
    assert "dev" in msg
    assert not tab.load_enabled_for_test()


def test_load_emits_exact_selection(qtbot, tmp_path: Path) -> None:
    engine = _FakeEngine()
    group = _add_3c(engine)
    dao = _seed_archive(tmp_path, "dev", group)
    tab = ArchiveTab(engine, dao)  # type: ignore[arg-type]
    qtbot.addWidget(tab)

    captured: list[tuple[object, object, float, float]] = []
    tab.loadRequested.connect(lambda d, g, s, e: captured.append((d, g, s, e)))

    sel_start, sel_end = tab.interval_for_test()
    tab._on_load_clicked()

    assert len(captured) == 1
    device, emitted_group, t_start, t_end = captured[0]
    assert device == "dev"
    # The emitted group is exactly the widget's selected Z/N/E mapping.
    assert emitted_group == tab.selected_group()
    assert set(emitted_group.values()) == set(group.values())
    assert t_start == sel_start
    assert t_end == sel_end
