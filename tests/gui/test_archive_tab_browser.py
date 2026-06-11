"""Archive tab — session browser (M3-A acceptance).

Sessions are the archive unit (rule 14): the browser lists sessions across
the project dirs' ``archive.db``s plus the base monitoring index, flags
crash-dirty sessions visibly, filters by name and date, shows a per-session
device/station tree with coverage strips, defaults the load interval to a
recent slice within the session's REAL coverage, and reads a CLOSED
session's waveforms with no active session in the engine — the data lives
under ``<base>/<project>/`` where the live readers cannot reach it (the
M2-B NOTE in ROADMAP).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest
from obspy import Trace, UTCDateTime

from echosmonitor.core.archive_browser_loader import ArchiveBrowserLoader
from echosmonitor.core.models import StreamID
from echosmonitor.core.session import session_archive_root
from echosmonitor.gui.widgets.archive_tab import ArchiveTab
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.sds import device_sds_root, sds_path

_T0 = UTCDateTime("2026-05-10T12:00:00")
_FS = 100.0
_DEVICE = "echos-1"
_STA = "XX.STA.00.HH"


def _write_trace(
    session_root: Path, comp: str, t0: UTCDateTime, npts: int, *, suffix: str = ""
) -> Path:
    sid = StreamID("XX", "STA", "00", f"HH{comp}")
    path = sds_path(device_sds_root(session_root, _DEVICE), t0, sid)
    if suffix:
        # A second segment as its own indexed file (distinct ``files`` row →
        # the coverage gap is modelled; the index-backed reader finds it).
        path = path.with_name(path.name + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(abs(hash(comp)) % (2**32))
    tr = Trace(
        data=(rng.standard_normal(npts) * 1000.0).astype(np.int32),
        header={
            "network": "XX",
            "station": "STA",
            "location": "00",
            "channel": f"HH{comp}",
            "starttime": t0,
            "sampling_rate": _FS,
        },
    )
    tr.write(str(path), format="MSEED")
    return path


def _seed_session(
    base: Path,
    project: str,
    *,
    started: UTCDateTime,
    ended: UTCDateTime | None,
    dirty: bool = False,
    waveforms: bool = True,
    streams_only: bool = False,
) -> Path:
    """A recorded session: DB rows + (optionally) real SDS files with a gap.

    Coverage: [_T0, _T0+30] and [_T0+60, _T0+90] — two segments, one gap.
    ``streams_only`` indexes the streams but records no files (the
    "session recorded nothing for this stream" empty state).
    """
    root = session_archive_root(base, project)
    dao = ArchiveDao(root / "archive.db")
    sid = dao.start_session("host", "v", "hash", project_name=project, devices=(_DEVICE,))
    if waveforms or streams_only:
        dev_id = dao.upsert_device(_DEVICE, "h", 18000, {})
        for comp in ("Z", "N", "E"):
            stream_id = dao.upsert_stream(dev_id, ("XX", "STA", "00", f"HH{comp}"), _FS)
            if streams_only:
                continue
            path_a = _write_trace(root, comp, _T0, int(_FS * 30))
            dao.record_file(stream_id, path_a, _T0, _T0 + 30, path_a.stat().st_size)
            path_b = _write_trace(root, comp, _T0 + 60, int(_FS * 30), suffix=".seg2")
            dao.record_file(stream_id, path_b, _T0 + 60, _T0 + 90, path_b.stat().st_size)
    if ended is not None:
        dao.end_session(sid, dirty=dirty)
    dao.close()
    conn = sqlite3.connect(root / "archive.db")
    conn.execute(
        "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
        (str(started), str(ended) if ended is not None else None, sid),
    )
    conn.commit()
    conn.close()
    return root


@pytest.fixture
def browser():
    loader = ArchiveBrowserLoader()
    yield loader
    loader.shutdown()


def _make_tab(qtbot, base: Path, browser: ArchiveBrowserLoader) -> ArchiveTab:
    tab = ArchiveTab(browser, base)
    qtbot.addWidget(tab)
    return tab


def _wait_rows(qtbot, tab: ArchiveTab, n: int) -> None:
    qtbot.waitUntil(lambda: len(tab.session_rows_for_test()) >= n, timeout=10_000)


def _wait_station(qtbot, tab: ArchiveTab) -> None:
    qtbot.waitUntil(
        lambda: tab.station_strip_for_test(_DEVICE, _STA) is not None, timeout=10_000
    )


# ---------------------------------------------------------------------------
# Listing across DBs + flags
# ---------------------------------------------------------------------------


def test_lists_sessions_across_project_dbs_and_base_index(qtbot, tmp_path, browser) -> None:
    base = tmp_path / "archive"
    _seed_session(base, "Alpha Site", started=_T0 - 10, ended=_T0 + 120)
    _seed_session(base, "Beta", started=_T0 + 4000, ended=_T0 + 5000, waveforms=False)
    base_dao = ArchiveDao(base / "archive.db")  # sessionless monitoring index
    sid = base_dao.start_session("host", "v", "hash")
    base_dao.end_session(sid)
    base_dao.close()

    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 3)

    names = [r[0] for r in tab.session_rows_for_test()]
    assert set(names) == {"Alpha Site", "Beta", "(monitoring)"}


def test_dirty_session_is_visibly_flagged(qtbot, tmp_path, browser) -> None:
    base = tmp_path / "archive"
    _seed_session(base, "clean", started=_T0 - 10, ended=_T0 + 120, waveforms=False)
    _seed_session(
        base, "crashy", started=_T0 + 1000, ended=_T0 + 2000, dirty=True, waveforms=False
    )

    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 2)

    status = {name: st for name, _started, st in tab.session_rows_for_test()}
    assert "dirty" in status["crashy"]
    assert "dirty" not in status["clean"]


def test_open_session_is_marked_open(qtbot, tmp_path, browser) -> None:
    base = tmp_path / "archive"
    _seed_session(base, "running", started=_T0 - 10, ended=None, waveforms=False)
    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 1)
    (_name, _started, status) = tab.session_rows_for_test()[0]
    assert "open" in status


# ---------------------------------------------------------------------------
# Name / date filter
# ---------------------------------------------------------------------------


def test_name_search_filters_list(qtbot, tmp_path, browser) -> None:
    base = tmp_path / "archive"
    _seed_session(base, "Alpha Site", started=_T0 - 10, ended=_T0 + 120, waveforms=False)
    _seed_session(base, "Beta", started=_T0 + 1000, ended=_T0 + 2000, waveforms=False)
    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 2)

    tab._search_edit.setText("alpha")
    assert [r[0] for r in tab.session_rows_for_test()] == ["Alpha Site"]
    tab._search_edit.setText("")
    assert len(tab.session_rows_for_test()) == 2


def test_date_filter_excludes_out_of_range_sessions(qtbot, tmp_path, browser) -> None:
    from PySide6.QtCore import QDate

    base = tmp_path / "archive"
    _seed_session(
        base,
        "old",
        started=UTCDateTime("2026-04-01T08:00:00"),
        ended=UTCDateTime("2026-04-01T09:00:00"),
        waveforms=False,
    )
    _seed_session(
        base,
        "recent",
        started=UTCDateTime("2026-05-10T08:00:00"),
        ended=UTCDateTime("2026-05-10T09:00:00"),
        waveforms=False,
    )
    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 2)

    tab._date_from.setDate(QDate(2026, 5, 1))
    tab._date_to.setDate(QDate(2026, 5, 31))
    tab._date_check.setChecked(True)
    assert [r[0] for r in tab.session_rows_for_test()] == ["recent"]
    tab._date_check.setChecked(False)
    assert len(tab.session_rows_for_test()) == 2


# ---------------------------------------------------------------------------
# Per-session tree, coverage, default interval
# ---------------------------------------------------------------------------


def test_selecting_session_builds_tree_with_gap_coverage(qtbot, tmp_path, browser) -> None:
    base = tmp_path / "archive"
    _seed_session(base, "proj", started=_T0 - 10, ended=_T0 + 120)
    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 1)

    tab.select_session_for_test(0)
    _wait_station(qtbot, tab)

    strip = tab.station_strip_for_test(_DEVICE, _STA)
    assert strip is not None
    w_start, w_end, intervals = strip.coverage_for_test()
    # The strip's frame is the session span; two covered segments → one gap.
    assert w_start == float((_T0 - 10).timestamp)
    assert w_end == float((_T0 + 120).timestamp)
    assert len(intervals) == 2


def test_station_select_defaults_interval_within_session_coverage(
    qtbot, tmp_path, browser
) -> None:
    base = tmp_path / "archive"
    _seed_session(base, "proj", started=_T0 - 10, ended=_T0 + 120)
    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 1)
    tab.select_session_for_test(0)
    _wait_station(qtbot, tab)
    assert tab.select_station_for_test(_DEVICE, _STA)

    assert "Archived (this session)" in tab.extent_text_for_test()
    t_start, t_end = tab.interval_for_test()
    # Within the real coverage [_T0, _T0+90] — never a placeholder epoch.
    assert float(_T0.timestamp) <= t_start < t_end <= float((_T0 + 90).timestamp)
    assert UTCDateTime(t_start).year == 2026
    assert tab.load_enabled_for_test()
    # The main strip models the gap once the interval spans it.
    from PySide6.QtCore import QDateTime, QTimeZone

    tab._start_edit.setDateTime(
        QDateTime.fromSecsSinceEpoch(int(_T0.timestamp), QTimeZone.utc())
    )
    tab._end_edit.setDateTime(
        QDateTime.fromSecsSinceEpoch(int((_T0 + 90).timestamp), QTimeZone.utc())
    )
    _w0, _w1, intervals = tab._coverage.coverage_for_test()
    assert len(intervals) == 2


def test_station_with_no_session_files_points_at_recording(qtbot, tmp_path, browser) -> None:
    """Streams indexed but zero file rows: the empty state names the device
    and the action that creates archives (a Recording session) — the
    M2-A empty-state contract, re-scoped per-session."""
    base = tmp_path / "archive"
    _seed_session(
        base, "proj", started=_T0 - 10, ended=_T0 + 120, waveforms=False, streams_only=True
    )
    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 1)
    tab.select_session_for_test(0)
    _wait_station(qtbot, tab)
    assert tab.select_station_for_test(_DEVICE, _STA)

    msg = tab.extent_text_for_test()
    assert "No archived waveforms" in msg
    assert _DEVICE in msg
    assert "recording session" in msg.lower()
    assert not tab.load_enabled_for_test()


def test_late_detail_for_vanished_session_does_not_resurrect_tree(
    qtbot, tmp_path, browser
) -> None:
    """Ghost-tree regression (qt-concurrency-auditor F2): once the selected
    session vanished (filter/refresh), a late detailLoaded for it must be
    dropped — a resurrected tree would route Load to the wrong root."""
    from echosmonitor.core.archive_browser_loader import SessionDetailResult

    base = tmp_path / "archive"
    _seed_session(base, "proj", started=_T0 - 10, ended=_T0 + 120)
    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 1)
    tab.select_session_for_test(0)
    _wait_station(qtbot, tab)
    stale = tab._detail
    stale_token = tab._detail_token
    assert stale is not None

    tab._search_edit.setText("zzz")  # the selected session vanishes
    assert tab.station_strip_for_test(_DEVICE, _STA) is None

    tab._on_detail_loaded(
        SessionDetailResult(
            token=stale_token,
            entry=stale.entry,
            span=stale.span,
            stations=stale.stations,
            elapsed_ms=1.0,
        )
    )
    assert tab.station_strip_for_test(_DEVICE, _STA) is None
    assert tab.selected_group() is None


def test_session_without_stations_shows_honest_state(qtbot, tmp_path, browser) -> None:
    base = tmp_path / "archive"
    _seed_session(base, "empty", started=_T0 - 10, ended=_T0 + 120, waveforms=False)
    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 1)
    tab.select_session_for_test(0)
    qtbot.waitUntil(
        lambda: "No 3-component stations" in tab.browser_status_for_test(), timeout=10_000
    )
    assert not tab.load_enabled_for_test()


def test_load_emits_exact_selection(qtbot, tmp_path, browser) -> None:
    base = tmp_path / "archive"
    _seed_session(base, "proj", started=_T0 - 10, ended=_T0 + 120)
    tab = _make_tab(qtbot, base, browser)
    _wait_rows(qtbot, tab, 1)
    tab.select_session_for_test(0)
    _wait_station(qtbot, tab)
    assert tab.select_station_for_test(_DEVICE, _STA)

    captured: list[tuple[object, object, float, float]] = []
    tab.loadRequested.connect(lambda d, g, s, e: captured.append((d, g, s, e)))
    sel_start, sel_end = tab.interval_for_test()
    tab._on_load_clicked()

    assert len(captured) == 1
    device, emitted_group, t_start, t_end = captured[0]
    assert device == _DEVICE
    assert emitted_group == tab.selected_group()
    assert set(emitted_group.values()) == {f"{_STA}Z", f"{_STA}N", f"{_STA}E"}
    assert t_start == sel_start
    assert t_end == sel_end
    # The session context the main window resolves the request against.
    entry = tab.selected_session_entry()
    assert entry is not None
    assert entry.session_root == str(session_archive_root(base, "proj"))


# ---------------------------------------------------------------------------
# M3-A acceptance: a CLOSED session's waveforms load end-to-end with NO
# active session in the engine (the M2-B NOTE — live readers can't reach
# <base>/<project>/ between sessions; the browser must).
# ---------------------------------------------------------------------------


def test_closed_session_waveforms_load_without_active_session(qtbot, tmp_path) -> None:
    from echosmonitor.config.schema import (
        AppConfig,
        DeviceConfig,
        RootConfig,
        StreamSelectorConfig,
        UiConfig,
    )
    from echosmonitor.gui.main_window import MainWindow

    base = tmp_path / "archive"
    _seed_session(base, "field day", started=_T0 - 10, ended=_T0 + 120)

    cfg = RootConfig(
        app=AppConfig(archive_root=base),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name=_DEVICE,
                host="127.0.0.1",
                port=18000,
                selectors=[StreamSelectorConfig(network="XX", station="STA")],
            )
        ],
    )
    window = MainWindow(cfg, tmp_path / "config.yaml")
    qtbot.addWidget(window)
    try:
        assert window._engine.active_session() is None  # rule 13: nothing runs
        tab = window._archive_tab
        _wait_rows(qtbot, tab, 1)
        tab.select_session_for_test(0)
        _wait_station(qtbot, tab)
        assert tab.select_station_for_test(_DEVICE, _STA)
        assert tab.load_enabled_for_test()

        tab._load_button.click()

        def _z_rendered() -> bool:
            x, _y = tab.trace_curve_for_test("Z").getData()
            return x is not None and len(x) > 0

        qtbot.waitUntil(_z_rendered, timeout=15_000)
        assert "Loaded" in tab.status_text_for_test()
        # Still no engine session — the read came from the browsed project
        # root, not the live context.
        assert window._engine.active_session() is None
    finally:
        window.close()


def test_hvsr_handoff_carries_session_root(qtbot, tmp_path) -> None:
    """The M3-E seam: 'Run on archive' after a hand-off reads the browsed
    session's root, not the live engine root."""
    from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
    from echosmonitor.core.hvsr import HvsrSettings
    from echosmonitor.gui.main_window import MainWindow

    base = tmp_path / "archive"
    root = _seed_session(base, "field day", started=_T0 - 10, ended=_T0 + 120)

    cfg = RootConfig(app=AppConfig(archive_root=base), ui=UiConfig(), devices=[])
    window = MainWindow(cfg, tmp_path / "config.yaml")
    qtbot.addWidget(window)
    try:
        tab = window._archive_tab
        _wait_rows(qtbot, tab, 1)
        tab.select_session_for_test(0)
        _wait_station(qtbot, tab)
        group = tab.selected_group()
        assert group is not None

        window._handoff_archive_to_hvsr(
            _DEVICE, group, float(_T0.timestamp), float((_T0 + 90).timestamp)
        )
        assert window._hvsr_archive_ctx == (
            _DEVICE,
            str(root),
            float(_T0.timestamp),
            float((_T0 + 90).timestamp),
        )

        captured: list[Path] = []
        window._hvsr_engine.start_archive_measurement = (  # type: ignore[method-assign]
            lambda device, grp, t0, t1, settings, reader: (
                captured.append(reader._root),
                "m-1",
            )[1]
        )
        window._run_hvsr_archive(
            _DEVICE, group, str(_T0), str(_T0 + 90), HvsrSettings()
        )
        assert captured == [Path(root)]

        # Same device, DIFFERENT interval = a manual re-target of the HVSR
        # widget (reachable without a hand-off): it must fall back to the
        # live engine root, never silently read the stale session (review
        # finding — the ctx is keyed on device AND interval).
        window._run_hvsr_archive(
            _DEVICE, group, str(_T0 + 3600), str(_T0 + 3690), HvsrSettings()
        )
        assert captured[-1] == window._engine.archive_root(_DEVICE)
        assert captured[-1] != Path(root)
    finally:
        window.close()


def test_close_does_not_resurrect_browser_thread(qtbot, tmp_path) -> None:
    """Teardown regression (qt-concurrency-auditor F1): a sessionChanged
    emitted at/after closeEvent (engine.stop closes the active session)
    must not lazily RESTART the just-joined browser thread — nothing would
    ever join it again and Qt aborts at exit."""
    from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
    from echosmonitor.gui.main_window import MainWindow

    base = tmp_path / "archive"
    _seed_session(base, "proj", started=_T0 - 10, ended=_T0 + 120, waveforms=False)
    cfg = RootConfig(app=AppConfig(archive_root=base), ui=UiConfig(), devices=[])
    window = MainWindow(cfg, tmp_path / "config.yaml")
    qtbot.addWidget(window)
    _wait_rows(qtbot, window._archive_tab, 1)

    window.close()
    assert not window._archive_browser._thread.isRunning()
    # The bridge is severed in closeEvent: a late queued sessionChanged
    # (the engine.stop() path) can no longer reach refresh_sessions.
    window._engine.sessionChanged.emit(None)
    qtbot.wait(150)
    assert not window._archive_browser._thread.isRunning()
