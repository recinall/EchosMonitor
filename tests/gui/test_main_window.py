"""Smoke tests for :class:`MainWindow`."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from obspy import UTCDateTime
from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtWidgets import QDockWidget, QLabel, QTabWidget, QToolBar
from pytestqt.qtbot import QtBot

from echosmonitor.config.loader import load_config
from echosmonitor.config.schema import (
    AppConfig,
    BandpassStage,
    DetrendStage,
    DeviceConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import AcquisitionState, Detection
from echosmonitor.gui import main_window as main_window_mod
from echosmonitor.gui.main_window import MainWindow


def _root_cfg(devices: list[DeviceConfig]) -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=devices,
    )


def _device(name: str, dsp_chain: list[object]) -> DeviceConfig:
    return DeviceConfig(
        name=name,
        host="127.0.0.1",
        port=18000,
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
        dsp_chain=dsp_chain,  # type: ignore[arg-type]
    )


def _no_chain_label(window: MainWindow) -> QLabel:
    label = window.findChild(QLabel, "StatusBarNoChainNote")
    assert label is not None, "status-bar no-chain label missing"
    return label


def test_main_window_smoke(qtbot: QtBot) -> None:
    cfg, _ = load_config(None)
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)

    assert window.windowTitle() == "EchosMonitor"
    docks = window.findChildren(QDockWidget)
    assert len(docks) == 4
    expected = {"Devices", "Stations", "Spectrogram", "Log"}
    assert {dock.windowTitle() for dock in docks} == expected

    # The analysis views are central QTabWidget tabs.
    central = window.centralWidget()
    assert isinstance(central, QTabWidget)
    tab_texts = [central.tabText(i) for i in range(central.count())]
    assert tab_texts == ["Detections", "Live", "PSD", "HVSR", "Archive"]

    # Closing must not raise even when QSettings is empty.
    window.close()


def _detection(t_on: str, t_off: str | None = None) -> Detection:
    return Detection(
        device="dev",
        nslc="IU.ANMO.00.BHZ",
        kind="sta_lta",
        t_on=UTCDateTime(t_on),
        t_off=UTCDateTime(t_off) if t_off is not None else None,
        score=8.0,
        detected_at=UTCDateTime(t_on),
        meta={"sta_s": 1.0, "lta_s": 10.0, "on_thr": 3.5, "off_thr": 1.5},
    )


def test_detections_master_detail_in_central_splitter(qtbot: QtBot) -> None:
    """The Detections tab is a master-detail splitter: the table and the
    detail pane are both children of ``_detections_splitter``. Selecting a
    detection raises the Detections tab; a non-selection clears the pane."""
    cfg, _ = load_config(None)
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    try:
        splitter = window._detections_splitter
        # Both halves live inside the splitter (reparented as-is).
        assert window._detection_table.parent() is splitter
        assert window._detail_pane.parent() is splitter

        # A real (scrolled-out) detection still drives the central tab to
        # the Detections splitter.
        window._central_tabs.setCurrentWidget(window._live_tabs)
        window._on_detection_selected(_detection("2026-06-01T00:00:30"))
        assert window._central_tabs.currentWidget() is splitter

        # A non-None → None selection clears the detail pane without raising.
        window._on_detection_selected(None)
        assert not window._detail_pane._is_showing_plots_for_test()
    finally:
        window.close()


def test_central_minimum_width_stays_bounded(qtbot: QtBot) -> None:
    """The central QTabWidget's minimum width must stay < 400 px even after
    rendering a pathologically long detection title — and the QTabWidget's
    MAX-over-pages aggregation must not let another tab inflate it (the
    same trap the old QStackedWidget posed)."""
    cfg, _ = load_config(None)
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    try:
        long_det = Detection(
            device="a-very-long-device-name-for-the-title-bar",
            nslc="XX.LONGSTATION.00.HHZ",
            kind="sta_lta",
            t_on=UTCDateTime("2026-06-01T00:00:30"),
            t_off=UTCDateTime("2026-06-01T00:09:33"),
            score=8.0,
            detected_at=UTCDateTime("2026-06-01T00:00:30"),
            meta={"sta_s": 1.0, "lta_s": 10.0},
        )
        latest = UTCDateTime("2026-06-01T00:10:00")
        samples = np.random.default_rng(1).standard_normal(6000).astype(np.float32)
        window._detail_pane.show_detection(long_det, samples, 100.0, latest)
        qtbot.wait(10)

        assert window._central_tabs.minimumSizeHint().width() < 400
        # Switching to PSD and back must not let any page inflate the
        # aggregate minimum.
        window._central_tabs.setCurrentWidget(window._psd_widget)
        qtbot.wait(10)
        window._central_tabs.setCurrentWidget(window._detections_splitter)
        qtbot.wait(10)
        assert window._central_tabs.minimumSizeHint().width() < 400
    finally:
        window.close()


def _seed_legacy_qsettings() -> None:
    """Write one key into the pre-rename SeedLinkDashboard QSettings store.

    The autouse ``_redirect_qsettings`` fixture routes both org/app pairs
    into the per-test tmp dir, so this never touches the user's machine.
    """
    legacy = QSettings(main_window_mod._LEGACY_ORG_NAME, main_window_mod._LEGACY_APP_NAME)
    legacy.setValue("windowState", QByteArray(b"stale-pre-rename-blob"))
    legacy.sync()


def test_qsettings_reset_after_rename_logged(
    qtbot: QtBot, capture_structlog: list[dict[str, object]]
) -> None:
    """M0 regression (decision log): legacy SeedLinkDashboard QSettings are
    NOT migrated. When the renamed app has no settings of its own but the
    legacy store has keys, ``_restore_state`` logs
    ``qsettings_reset_after_rename`` once, with both org names in context,
    and the window still comes up with the default layout."""
    _seed_legacy_qsettings()
    window = MainWindow(_root_cfg([]), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    try:
        events = [
            r for r in capture_structlog if r.get("event") == "qsettings_reset_after_rename"
        ]
        assert len(events) == 1
        assert events[0]["legacy_org"] == main_window_mod._LEGACY_ORG_NAME
        assert events[0]["org"] == main_window_mod._ORG_NAME
        # Default layout is intact: the legacy blob was never restored.
        assert len(window.findChildren(QDockWidget)) == 4
    finally:
        window.close()


def test_qsettings_reset_log_absent_without_legacy_store(
    qtbot: QtBot, capture_structlog: list[dict[str, object]]
) -> None:
    """A genuinely fresh install (no legacy store either) must NOT emit the
    rename log — it would be pure noise."""
    window = MainWindow(_root_cfg([]), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    try:
        assert not any(
            r.get("event") == "qsettings_reset_after_rename" for r in capture_structlog
        )
    finally:
        window.close()


def test_qsettings_reset_log_is_one_time(
    qtbot: QtBot, capture_structlog: list[dict[str, object]]
) -> None:
    """The rename log fires only while the new store is empty: after the
    first window persists its state on close, a second launch must stay
    quiet even though the legacy store still has keys."""
    _seed_legacy_qsettings()
    first = MainWindow(_root_cfg([]), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(first)
    first.close()  # closeEvent persists geometry/windowState to the new store
    assert QSettings(main_window_mod._ORG_NAME, main_window_mod._APP_NAME).allKeys()

    second = MainWindow(_root_cfg([]), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(second)
    try:
        events = [
            r for r in capture_structlog if r.get("event") == "qsettings_reset_after_rename"
        ]
        assert len(events) == 1  # the first launch only
    finally:
        second.close()


def test_launch_with_devices_does_not_start_engine(qtbot: QtBot) -> None:
    """Rule 13 at the window level: constructing MainWindow with
    configured devices must NOT start the engine — no workers, no
    threads, every device IDLE. This pins the M2-A removal of the
    autostart site in ``MainWindow.__init__``; the engine-level pins
    live in ``tests/core/test_engine_session_lifecycle.py``."""
    cfg = _root_cfg(devices=[_device("dev-a", []), _device("dev-b", [])])
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    try:
        qtbot.wait(300)
        assert window._engine._started is False
        assert window._engine._workers == {}
        assert window._engine.acquisition_state("dev-a") is AcquisitionState.IDLE
        assert window._engine.acquisition_state("dev-b") is AcquisitionState.IDLE
    finally:
        window.close()


def test_status_bar_shows_note_when_device_has_no_dsp_chain(qtbot: QtBot) -> None:
    """Devices configured without a `dsp_chain` get a dim italic note in
    the status bar listing the count, with the device names in the tooltip.
    """
    cfg = _root_cfg(
        devices=[
            _device("with-chain", [BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)]),
            _device("no-chain-1", []),
            _device("no-chain-2", []),
        ]
    )
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    try:
        label = _no_chain_label(window)
        assert "2 device(s) without DSP chain" in label.text()
        tooltip = label.toolTip()
        assert "no-chain-1" in tooltip
        assert "no-chain-2" in tooltip
        assert "with-chain" not in tooltip
    finally:
        window.close()


def test_status_bar_no_note_when_all_devices_have_chains(qtbot: QtBot) -> None:
    """The note disappears (empty text) when every configured device has
    at least one DSP stage."""
    cfg = _root_cfg(
        devices=[
            _device("a", [DetrendStage(type="detrend", kind="constant")]),
            _device("b", [BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)]),
        ]
    )
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    try:
        label = _no_chain_label(window)
        assert label.text() == ""
        assert label.toolTip() == ""
    finally:
        window.close()


def test_close_event_is_reentrant(qtbot: QtBot) -> None:
    """Regression (M1-D): closeEvent runs TWICE in practice — an explicit
    ``window.close()`` plus the pytest-qt teardown close. The Echos
    poller shutdown uses a BlockingQueuedConnection barrier that would
    block forever if invoked into the already-finished worker thread on
    the second pass; the isRunning() guard makes the second close a
    no-op instead of a deadlock (which froze the whole suite at ~45%).
    """
    cfg, _ = load_config(None)
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    window.close()
    assert not window._echos_thread.isRunning()
    window.close()  # must return, not hang


def test_launch_has_session_toolbar_and_sweeps_crashed_sessions(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """M2-C: MainWindow exposes the session toolbar and the launch
    crash-recovery sweep closes-as-dirty any session a crash left open."""
    from echosmonitor.storage.dao import ArchiveDao

    archive_root = tmp_path / "archive"
    crashed = archive_root / "proj"
    crashed.mkdir(parents=True)
    dao = ArchiveDao(crashed / "archive.db")
    dao.start_session("h", "v", "c", project_name="proj")  # never ended
    dao.close()

    cfg = _root_cfg(devices=[_device("dev-a", [])])
    cfg = cfg.model_copy(update={"app": cfg.app.model_copy(update={"archive_root": archive_root})})
    window = MainWindow(cfg, tmp_path / "cfg.yaml")
    qtbot.addWidget(window)
    try:
        toolbar = window.findChild(QToolBar, "SessionToolbar")
        assert toolbar is not None
        label = window.findChild(QLabel, "SessionStatusLabel")
        assert label is not None and label.text() == "Idle"

        import sqlite3

        conn = sqlite3.connect(crashed / "archive.db")
        try:
            row = conn.execute("SELECT ended_at, closed_dirty FROM sessions").fetchone()
        finally:
            conn.close()
        assert row[0] is not None
        assert row[1] == 1
    finally:
        window.close()


def test_default_config_resolves_archive_root_inside_test_tmp(tmp_path: Path) -> None:
    """Suite-isolation pin (code-reviewer blocker on the M2-C diff): with
    a default config (archive_root=None) the platformdirs fallback must
    resolve INSIDE the per-test tmp dir — otherwise every MainWindow
    test sweeps (and schema-migrates) the user's real archive at launch."""
    from echosmonitor.core.session import resolve_base_archive_root

    root = resolve_base_archive_root(_root_cfg([]))
    assert str(root).startswith(str(tmp_path)), root
