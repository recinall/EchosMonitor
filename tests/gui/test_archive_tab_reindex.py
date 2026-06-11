"""Archive tab — re-indexer surface (M3-D acceptance).

A copied archive (SDS tree, no DB) becomes browsable and loadable after
a re-index through the real worker; a stale DB's counts are corrected;
the ACTIVE session's DB is refused (the engine is writing it — rule 8);
target validation keeps re-indexes inside the discoverable base root.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from obspy import Trace, UTCDateTime

from echosmonitor.core.archive_browser_loader import ArchiveBrowserLoader
from echosmonitor.core.models import StreamID
from echosmonitor.gui.widgets.archive_tab import ArchiveTab
from echosmonitor.storage.sds import device_sds_root, sds_path

_FS = 100.0
_T0 = UTCDateTime("2026-05-10T12:00:00")
_DEVICE = "echos-1"
_STA = "XX.STA.00.HH"


def _write_copied_archive(base: Path, project_dir: str) -> Path:
    """An SDS tree as copied from another machine: files, NO archive.db."""
    root = base / project_dir
    for comp in ("Z", "N", "E"):
        sid = StreamID("XX", "STA", "00", f"HH{comp}")
        path = sds_path(device_sds_root(root, _DEVICE), _T0, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(abs(hash(comp)) % (2**32))
        Trace(
            data=(rng.standard_normal(int(_FS * 30)) * 1000.0).astype(np.int32),
            header={
                "network": "XX",
                "station": "STA",
                "location": "00",
                "channel": f"HH{comp}",
                "starttime": _T0,
                "sampling_rate": _FS,
            },
        ).write(str(path), format="MSEED")
    return root


@pytest.fixture
def browser():
    loader = ArchiveBrowserLoader()
    yield loader
    loader.shutdown()


# ---------------------------------------------------------------------------
# Target validation (tab-level)
# ---------------------------------------------------------------------------


def _patch_dir_dialog(monkeypatch, chosen: str) -> None:
    monkeypatch.setattr(
        "echosmonitor.gui.widgets.archive_tab.QFileDialog.getExistingDirectory",
        staticmethod(lambda *a, **k: chosen),
    )


def test_reindex_click_emits_for_project_dir(qtbot, tmp_path, browser, monkeypatch) -> None:
    base = tmp_path / "archive"
    root = _write_copied_archive(base, "copied")
    tab = ArchiveTab(browser, base)
    qtbot.addWidget(tab)
    _patch_dir_dialog(monkeypatch, str(root))
    captured: list[str] = []
    tab.reindexRequested.connect(captured.append)

    tab._reindex_button.click()

    assert captured == [str(root)]


def test_reindex_click_rejects_base_root_and_outsiders(
    qtbot, tmp_path, browser, monkeypatch
) -> None:
    base = tmp_path / "archive"
    base.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    tab = ArchiveTab(browser, base)
    qtbot.addWidget(tab)
    captured: list[str] = []
    tab.reindexRequested.connect(captured.append)

    _patch_dir_dialog(monkeypatch, str(base))
    tab._reindex_button.click()
    assert captured == []
    assert "INSIDE" in tab._browser_status.text()

    _patch_dir_dialog(monkeypatch, str(elsewhere))
    tab._reindex_button.click()
    assert captured == []
    assert "Not a project directory" in tab._browser_status.text()

    _patch_dir_dialog(monkeypatch, "")  # dialog cancelled
    tab._reindex_button.click()
    assert captured == []


# ---------------------------------------------------------------------------
# M3-D acceptance: copied archive (no DB) → re-index → browser lists AND
# loads it, end to end through the real worker, engine fully idle.
# ---------------------------------------------------------------------------


def test_copied_archive_reindex_then_browse_and_load(qtbot, tmp_path, monkeypatch) -> None:
    from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
    from echosmonitor.gui.main_window import MainWindow

    base = tmp_path / "archive"
    root = _write_copied_archive(base, "copied_field_day")
    assert not (root / "archive.db").exists()

    cfg = RootConfig(app=AppConfig(archive_root=base), ui=UiConfig(), devices=[])
    window = MainWindow(cfg, tmp_path / "config.yaml")
    qtbot.addWidget(window)
    try:
        assert window._engine.active_session() is None  # rule 13: idle
        tab = window._archive_tab
        # Nothing browsable before the re-index: no DB, no session rows.
        qtbot.waitUntil(
            lambda: "No sessions" in tab._browser_status.text(), timeout=10_000
        )
        assert tab.session_rows_for_test() == []

        _patch_dir_dialog(monkeypatch, str(root))
        tab._reindex_button.click()

        # The real worker rebuilds the DB; done → automatic re-discovery.
        qtbot.waitUntil(
            lambda: "Re-index complete" in tab._browser_status.text(), timeout=15_000
        )
        qtbot.waitUntil(lambda: len(tab.session_rows_for_test()) >= 1, timeout=10_000)
        (name, _started, status) = tab.session_rows_for_test()[0]
        assert name == "copied_field_day"  # dir name — the honest fallback
        assert "re-indexed" in status
        assert tab._reindex_button.isEnabled()  # busy state released

        # ...and LOADS: the synthesized row reaches real waveforms.
        tab.select_session_for_test(0)
        qtbot.waitUntil(
            lambda: tab.station_strip_for_test(_DEVICE, _STA) is not None,
            timeout=10_000,
        )
        assert tab.select_station_for_test(_DEVICE, _STA)
        assert tab.load_enabled_for_test()
        tab._load_button.click()

        def _z_rendered() -> bool:
            x, _y = tab.trace_curve_for_test("Z").getData()
            return x is not None and len(x) > 0

        qtbot.waitUntil(_z_rendered, timeout=15_000)
        assert "Loaded" in tab.status_text_for_test()
        assert window._engine.active_session() is None
    finally:
        window.close()


# ---------------------------------------------------------------------------
# Teardown: a re-index finishing across closeEvent must not resurrect the
# just-joined browser thread (qt-concurrency-auditor BLOCKER — the M3-A F1
# abort-at-exit class: closeEvent's bounded joins give an in-flight
# re-index time to finish; its queued `finished` dispatches AFTER
# closeEvent returns and used to call refresh_sessions unconditionally).
# ---------------------------------------------------------------------------


def test_reindex_done_after_close_does_not_resurrect_browser_thread(
    qtbot, tmp_path
) -> None:
    from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
    from echosmonitor.core.archive_reindex_worker import ArchiveReindexResult
    from echosmonitor.gui.main_window import MainWindow
    from echosmonitor.storage.reindex import ReindexReport

    base = tmp_path / "archive"
    base.mkdir()
    cfg = RootConfig(app=AppConfig(archive_root=base), ui=UiConfig(), devices=[])
    window = MainWindow(cfg, tmp_path / "config.yaml")
    qtbot.addWidget(window)

    window.close()
    assert not window._archive_browser._thread.isRunning()

    # The late queued `finished` for a re-index that outlived closeEvent.
    window._on_archive_reindex_done(
        ArchiveReindexResult(
            token=window._archive_reindex_token,
            report=ReindexReport(
                session_root=str(base / "proj"),
                devices=1,
                streams=3,
                files_indexed=3,
                files_skipped=0,
                files_pruned=0,
                synthesized_session=True,
                cancelled=False,
                elapsed_s=0.1,
            ),
        )
    )
    qtbot.wait(150)
    assert not window._archive_browser._thread.isRunning()


# ---------------------------------------------------------------------------
# TOCTOU inverse guard (code-reviewer major): a recording session must not
# START into a root whose re-index is in flight — that would put the
# engine's DAO and the re-index DAO on one archive.db concurrently (rule 8).
# ---------------------------------------------------------------------------


def test_session_start_vetoed_into_root_being_reindexed(
    qtbot, tmp_path, monkeypatch
) -> None:
    import time as time_mod

    from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
    from echosmonitor.gui.main_window import MainWindow
    from echosmonitor.storage import reindex as reindex_mod

    base = tmp_path / "archive"
    root = _write_copied_archive(base, "copied_field_day")

    real_truth = reindex_mod._read_file_truth
    started = {"n": 0}

    def _slow_truth(candidate):  # type: ignore[no-untyped-def]
        started["n"] += 1
        time_mod.sleep(0.4)
        return real_truth(candidate)

    monkeypatch.setattr(reindex_mod, "_read_file_truth", _slow_truth)

    cfg = RootConfig(app=AppConfig(archive_root=base), ui=UiConfig(), devices=[])
    window = MainWindow(cfg, tmp_path / "config.yaml")
    qtbot.addWidget(window)
    try:
        window._on_archive_reindex_requested(str(root))
        qtbot.waitUntil(lambda: started["n"] >= 1, timeout=10_000)

        # Mid-re-index: that project is vetoed, any other project is not.
        veto = window._session_start_guard("copied_field_day")
        assert veto is not None and "re-indexed" in veto
        assert window._session_start_guard("some other project") is None

        qtbot.waitUntil(
            lambda: "Re-index complete" in window._archive_tab._browser_status.text(),
            timeout=15_000,
        )
        assert window._session_start_guard("copied_field_day") is None
    finally:
        window.close()


def test_toolbar_record_respects_start_guard(qtbot, tmp_path, monkeypatch) -> None:
    """The toolbar consults the installed guard BEFORE engine.start_session
    and surfaces the veto as a warning — no session starts."""
    from echosmonitor.config.schema import (
        AppConfig,
        DeviceConfig,
        RootConfig,
        StreamSelectorConfig,
        UiConfig,
    )
    from echosmonitor.core.streaming_engine import StreamingEngine
    from echosmonitor.gui.dialogs.new_session_dialog import NewSessionDialog
    from echosmonitor.gui.widgets.session_toolbar import SessionToolbar

    engine = StreamingEngine(
        RootConfig(
            app=AppConfig(archive_root=tmp_path / "archive"),
            ui=UiConfig(),
            devices=[
                DeviceConfig(
                    name="dev",
                    host="127.0.0.1",
                    port=18000,
                    selectors=[StreamSelectorConfig(network="XX", station="STA")],
                )
            ],
        )
    )
    toolbar = SessionToolbar(engine)
    qtbot.addWidget(toolbar)
    toolbar.set_session_start_guard(
        lambda project: f"'{project}' is being re-indexed" if project == "busy" else None
    )
    warnings: list[str] = []
    try:

        def _fake_exec(self: NewSessionDialog) -> int:
            self._name_edit.setText("busy")
            return int(NewSessionDialog.DialogCode.Accepted)

        monkeypatch.setattr(NewSessionDialog, "exec", _fake_exec)
        monkeypatch.setattr(
            "echosmonitor.gui.widgets.session_toolbar.QMessageBox.warning",
            lambda _parent, _title, text: warnings.append(text),
        )
        toolbar._action_record.trigger()
        assert engine.active_session() is None
        assert warnings == ["'busy' is being re-indexed"]
    finally:
        engine.stop()


# ---------------------------------------------------------------------------
# Active-session guard (rule 8: the engine is writing that DB)
# ---------------------------------------------------------------------------


def test_reindex_refuses_active_session_db(qtbot, tmp_path) -> None:
    from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
    from echosmonitor.gui.main_window import MainWindow

    base = tmp_path / "archive"
    cfg = RootConfig(app=AppConfig(archive_root=base), ui=UiConfig(), devices=[])
    window = MainWindow(cfg, tmp_path / "config.yaml")
    qtbot.addWidget(window)
    try:
        session = window._engine.start_session("live proj")
        session_root = Path(session.db_root)
        assert (session_root / "archive.db").is_file()

        window._on_archive_reindex_requested(str(session_root))

        assert window._archive_reindex_token == 0  # never reached the worker
        assert "ACTIVE" in window._archive_tab._browser_status.text()
        # Another (closed/copied) root is still allowed while a session runs.
        other = _write_copied_archive(base, "other_proj")
        window._on_archive_reindex_requested(str(other))
        assert window._archive_reindex_token == 1
        qtbot.waitUntil(
            lambda: "Re-index complete" in window._archive_tab._browser_status.text(),
            timeout=15_000,
        )
    finally:
        window._engine.end_session()
        window.close()
