"""M2-C — session toolbar + new-session dialog + crash-recovery sweep.

The toolbar is the user's acquisition surface (rule 13): Monitor starts
every idle device, Record… opens the dialog and starts the session,
Stop returns everything to Idle with the session row closed. Driven
against a real engine + fake SeedLink server so the M2 acceptance path
(launch → Monitor → Record → Stop) is exercised end to end.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt

from echosmonitor.config.schema import (
    AppConfig,
    ArchiveConfig,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import AcquisitionState
from echosmonitor.core.streaming_engine import StreamingEngine
from echosmonitor.gui.dialogs.new_session_dialog import NewSessionDialog
from echosmonitor.gui.widgets.session_toolbar import SessionToolbar
from tests.core.fakes import FakeSeedLinkServer, FakeSeedLinkServerConfig
from tests.core.test_seedlink_worker import _LoopThread, loop_thread  # noqa: F401
from tests.core.test_streaming_engine_multi import (
    make_fake_server,  # noqa: F401  pytest fixture re-export
)

_SERVER_CFG = FakeSeedLinkServerConfig(
    network="IU",
    station="ANMO",
    location="00",
    channel="BHZ",
    sampling_rate=20.0,
    samples_per_record=20,
    packet_interval_s=0.1,
)


def _wait_until(predicate: Callable[[], bool], timeout_s: float, qtbot) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qtbot.wait(50)
        if predicate():
            return True
    return False


def _make_cfg(archive_root: Path, server: FakeSeedLinkServer) -> RootConfig:
    cfg = server.config
    return RootConfig(
        app=AppConfig(archive_root=archive_root),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name="dev",
                host=server.host,
                port=server.port,
                reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
                selectors=[
                    StreamSelectorConfig(
                        network=cfg.network,
                        station=cfg.station,
                        location=cfg.location,
                        channel=cfg.channel,
                    )
                ],
                archive=ArchiveConfig(enabled=True, fsync_interval_s=0.5, queue_max=256),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Toolbar: Monitor / Record / Stop happy path (M2 acceptance)
# ---------------------------------------------------------------------------


def test_toolbar_monitor_record_stop_cycle(qtbot, tmp_path: Path, make_fake_server) -> None:  # noqa: F811
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    engine = StreamingEngine(_make_cfg(archive_root, server))
    toolbar = SessionToolbar(engine)
    qtbot.addWidget(toolbar)
    try:
        # Launch state: everything idle, Stop disabled, label "Idle".
        assert toolbar._action_monitor.isEnabled()
        assert toolbar._action_record.isEnabled()
        assert not toolbar._action_stop.isEnabled()
        assert toolbar._status_label.text() == "Idle"

        # ▶ Monitor → device monitors, zero disk writes.
        toolbar._action_monitor.trigger()
        assert engine.acquisition_state("dev") is AcquisitionState.MONITORING
        assert _wait_until(
            lambda: engine.read_recent("dev", "IU.ANMO.00.BHZ", 5.0)[0].size > 0,
            timeout_s=10.0,
            qtbot=qtbot,
        )
        assert not archive_root.exists()
        toolbar._refresh()
        assert toolbar._status_label.text() == "Monitoring (1)"
        assert toolbar._action_stop.isEnabled()

        # ⏺ Record (session started programmatically — the dialog flow
        # is covered separately) → SDS tree under the project root.
        engine.start_session("Survey", ["dev"])
        qtbot.wait(50)  # queued sessionChanged → baseline snapshot
        toolbar._refresh()
        assert toolbar._status_label.text().startswith("⏺ Survey ·")
        assert not toolbar._action_record.isEnabled()
        assert _wait_until(
            lambda: any(p.is_file() for p in (archive_root / "Survey").rglob("*.D.*")),
            timeout_s=10.0,
            qtbot=qtbot,
        )

        # ⏹ Stop → everything idle, session row closed clean.
        toolbar._action_stop.trigger()
        assert engine.acquisition_state("dev") is AcquisitionState.IDLE
        assert engine.active_session() is None
        conn = sqlite3.connect(archive_root / "Survey" / "archive.db")
        try:
            row = conn.execute("SELECT ended_at, closed_dirty FROM sessions").fetchone()
        finally:
            conn.close()
        assert row[0] is not None
        assert row[1] == 0
        toolbar._refresh()
        assert toolbar._status_label.text() == "Idle"
        assert not toolbar._action_stop.isEnabled()
    finally:
        engine.stop()


def test_toolbar_record_flow_via_dialog(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
    monkeypatch,
) -> None:
    """The Record… click runs the dialog and starts the session with the
    dialog's name + device selection."""
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    engine = StreamingEngine(_make_cfg(archive_root, server))
    toolbar = SessionToolbar(engine)
    qtbot.addWidget(toolbar)
    try:

        def _fake_exec(self: NewSessionDialog) -> int:
            self._name_edit.setText("Dialog Proj")
            return int(NewSessionDialog.DialogCode.Accepted)

        monkeypatch.setattr(NewSessionDialog, "exec", _fake_exec)
        toolbar._action_record.trigger()
        session = engine.active_session()
        assert session is not None
        assert session.project_name == "Dialog Proj"
        assert session.devices == ("dev",)
        assert (archive_root / "Dialog_Proj" / "archive.db").is_file()
    finally:
        engine.stop()


def test_toolbar_collision_shows_warning_not_crash(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
    monkeypatch,
) -> None:
    """A colliding project name surfaces as a QMessageBox warning; no
    session starts and the toolbar stays usable."""
    from echosmonitor.storage.dao import ArchiveDao

    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    pre = archive_root / "Proj_A"
    pre.mkdir(parents=True)
    dao = ArchiveDao(pre / "archive.db")
    dao.end_session(dao.start_session("h", "v", "c", project_name="Proj A"))
    dao.close()

    engine = StreamingEngine(_make_cfg(archive_root, server))
    toolbar = SessionToolbar(engine)
    qtbot.addWidget(toolbar)
    warnings: list[str] = []
    try:

        def _fake_exec(self: NewSessionDialog) -> int:
            self._name_edit.setText("Proj_A")  # collides with "Proj A"
            return int(NewSessionDialog.DialogCode.Accepted)

        monkeypatch.setattr(NewSessionDialog, "exec", _fake_exec)
        monkeypatch.setattr(
            "echosmonitor.gui.widgets.session_toolbar.QMessageBox.warning",
            lambda _parent, _title, text: warnings.append(text),
        )
        toolbar._action_record.trigger()
        assert engine.active_session() is None
        assert len(warnings) == 1
        assert "Proj A" in warnings[0]
        assert toolbar._action_record.isEnabled()
    finally:
        engine.stop()


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


def test_dialog_results_and_ok_gating(qtbot) -> None:
    dialog = NewSessionDialog(["dev-a", "dev-b"])
    qtbot.addWidget(dialog)
    ok = dialog._buttons.button(dialog._buttons.StandardButton.Ok)
    assert ok is not None
    # Blank name → OK disabled, no preview.
    assert not ok.isEnabled()
    assert dialog._preview.text() == ""
    # Name typed → OK enabled, sanitized preview shown.
    dialog._name_edit.setText("My Survey!")
    assert ok.isEnabled()
    assert "My_Survey" in dialog._preview.text()
    assert dialog.project_name() == "My Survey!"
    # All devices pre-checked; unchecking one narrows the result.
    assert dialog.checked_devices() == ("dev-a", "dev-b")
    item = dialog._device_list.item(1)
    assert item is not None
    item.setCheckState(Qt.CheckState.Unchecked)
    assert dialog.checked_devices() == ("dev-a",)
    # No devices checked → OK disabled (a session must record something).
    item0 = dialog._device_list.item(0)
    assert item0 is not None
    item0.setCheckState(Qt.CheckState.Unchecked)
    assert not ok.isEnabled()
