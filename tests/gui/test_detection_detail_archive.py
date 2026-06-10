"""MainWindow wiring for out-of-buffer archive detail (3C, off-thread).

Asserts observable behaviour (rule 10): an in-buffer detection renders from
the live ring buffer (no archive load), an out-of-buffer detection dispatches
the OFF-THREAD loader and shows a loading state, a delivered 3C result renders
and rebuilds the unit context, an empty/failed result shows the honest
message, a newer selection supersedes an in-flight load (latest-wins), and a
3C unit change issues one deconvolution per present, gap-free component.

These exercise the wiring directly (loader.request monkeypatched / result
slots called) so no real worker thread or archive I/O is needed — the loader
itself is covered in ``tests/core/test_archive_detail_loader.py``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from obspy import UTCDateTime
from pytestqt.qtbot import QtBot

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.archive_detail_loader import ArchiveDetailResult, ComponentTrace
from echosmonitor.core.models import Detection
from echosmonitor.gui.main_window import MainWindow

_FS = 100.0


def _cfg(tmp_path: Path) -> tuple[RootConfig, Path]:
    dev = DeviceConfig(
        name="anmo",
        host="127.0.0.1",
        port=18000,
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
    )
    cfg = RootConfig(
        app=AppConfig(archive_root=str(tmp_path / "sds")),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[dev],
    )
    return cfg, tmp_path / "config.yaml"


def _det(t_on: str = "2026-06-01T00:00:30", t_off: str = "2026-06-01T00:00:33") -> Detection:
    return Detection(
        device="anmo",
        nslc="IU.ANMO.00.BHZ",
        kind="sta_lta",
        t_on=UTCDateTime(t_on),
        t_off=UTCDateTime(t_off),
        score=8.0,
        detected_at=UTCDateTime(t_on),
        meta={"sta_s": 1.0, "lta_s": 10.0, "on_thr": 3.5, "off_thr": 1.5},
    )


def _traces(t_start: float, seconds: float, gap_comp: str | None = None) -> list[ComponentTrace]:
    out = []
    n = int(seconds * _FS)
    for comp in ("Z", "N", "E"):
        x = t_start + np.arange(n, dtype=np.float64) / _FS
        y = np.random.default_rng(ord(comp)).standard_normal(n).astype(np.float64)
        if comp == gap_comp:
            y[: n // 4] = np.nan
        out.append(
            ComponentTrace(
                comp=comp, nslc=f"IU.ANMO.00.BH{comp}", x=x, y=y, fs=_FS, start_epoch=t_start
            )
        )
    return out


def test_out_of_buffer_selection_dispatches_off_thread_load(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        captured: list[tuple] = []

        def _fake_request(*args):  # type: ignore[no-untyped-def]
            captured.append(args)
            window._archive_load_token += 1
            return window._archive_load_token

        window._archive_loader.request = _fake_request  # type: ignore[assignment]

        # The engine has no live data → read_recent is empty → scrolled out.
        window._on_detection_selected(_det())

        assert captured, "out-of-buffer selection did not dispatch an archive load"
        device, _trigger_nslc, components, t_start, t_end, _root = captured[0]
        assert device == "anmo"
        assert set(components) == {"Z", "N", "E"}
        assert t_start < float(_det().t_on) < t_end
        # The pane shows the loading state, not the live plots / archive view.
        assert "loading" in window._detail_pane._message_text_for_test().lower()
        assert not window._detail_pane.is_showing_archive()
    finally:
        window.close()


def test_sta_lta_read_window_includes_lta_warmup_preroll(qtbot: QtBot, tmp_path: Path) -> None:
    """H3: an STA/LTA archive read pulls warm-up pre-roll ahead of the inspect
    window, while the on-screen view stays the inspect window.

    Without enough pre-roll the recomputed recursive STA/LTA is flat through
    the onset and peaks spuriously at the right edge. The read must start
    ``>= _ARCHIVE_RATIO_WARMUP_LTA_MULT * lta_s`` before the inspect pre-roll;
    the stored view window must remain ``[t_on - PRE, t_off + POST]``.
    """
    from echosmonitor.gui.main_window import (
        _ARCHIVE_INSPECT_POST_S,
        _ARCHIVE_INSPECT_PRE_S,
    )

    cfg, cfg_path = _cfg(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        captured: list[tuple] = []

        def _fake_request(*args):  # type: ignore[no-untyped-def]
            captured.append(args)
            window._archive_load_token += 1
            return window._archive_load_token

        window._archive_loader.request = _fake_request  # type: ignore[assignment]

        det = _det()  # kind sta_lta, meta lta_s=10.0
        lta_s = float(det.meta["lta_s"])  # type: ignore[arg-type]
        window._on_detection_selected(det)

        assert captured
        _dev, _nslc, _comps, t_start, t_end, _root = captured[0]
        t_on = float(det.t_on)
        t_off = float(det.t_off)
        # Behavioural requirement (independent of the exact multiplier): the
        # read must start at least one full LTA window ahead of the inspect
        # pre-roll, so the recursive LTA converges before the onset. A no-op
        # warm-up (read starting at t_on - PRE) fails this.
        assert t_start <= t_on - _ARCHIVE_INSPECT_PRE_S - lta_s + 1e-6
        # The end is the inspect post window (no extra padding needed there).
        assert abs(t_end - (t_off + _ARCHIVE_INSPECT_POST_S)) < 1e-6
        # The on-screen VIEW stays the inspect window (warm-up off-screen).
        assert window._archive_view_window is not None
        vs, ve = window._archive_view_window
        assert abs(vs - (t_on - _ARCHIVE_INSPECT_PRE_S)) < 1e-6
        assert abs(ve - (t_off + _ARCHIVE_INSPECT_POST_S)) < 1e-6
    finally:
        window.close()


def test_in_buffer_selection_uses_live_path(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        requested: list[tuple] = []
        window._archive_loader.request = lambda *a: requested.append(a) or 0  # type: ignore[assignment]

        # Pretend the ring buffer holds a window covering the onset.
        latest = UTCDateTime("2026-06-01T00:01:00")
        samples = np.random.default_rng(0).standard_normal(int(_FS * 60)).astype(np.float32)
        window._engine.read_recent = lambda *a, **k: (samples, _FS, latest)  # type: ignore[assignment]

        window._on_detection_selected(_det())

        assert not requested, "in-buffer selection must NOT dispatch an archive load"
        assert window._detail_pane._is_showing_plots_for_test()  # live single-trace page
        assert not window._detail_pane.is_showing_archive()
    finally:
        window.close()


def test_loaded_result_renders_3c_and_rebuilds_context(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        det = _det()
        window._archive_load_detection = det
        window._archive_load_token = 7
        t_start = float(det.t_on) - 10.0
        result = ArchiveDetailResult(
            token=7, trigger_comp="Z", traces=_traces(t_start, 40.0), elapsed_ms=1.0
        )

        window._on_archive_detail_loaded(result)

        assert window._detail_pane.is_showing_archive()
        assert window._archive_traces and set(window._archive_traces) == {"Z", "N", "E"}
        # The unit context was rebuilt from the trigger component.
        assert window._detail_ctx.get("nslc") == det.nslc
    finally:
        window.close()


def test_stale_loaded_result_dropped(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        det = _det()
        window._archive_load_detection = det
        window._archive_load_token = 9  # current
        stale = ArchiveDetailResult(
            token=3, trigger_comp="Z", traces=_traces(float(det.t_on), 10.0), elapsed_ms=1.0
        )

        window._on_archive_detail_loaded(stale)

        assert not window._detail_pane.is_showing_archive()  # stale token ignored
    finally:
        window.close()


def test_empty_result_shows_honest_message(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        window._archive_load_detection = _det()
        window._archive_load_token = 4

        window._on_archive_detail_empty(4)

        msg = window._detail_pane._message_text_for_test().lower()
        assert "no archived" in msg
        assert "archive replay" not in msg
    finally:
        window.close()


def test_new_selection_supersedes_in_flight_load(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        tokens = {"v": 0}

        def _fake_request(*args):  # type: ignore[no-untyped-def]
            tokens["v"] += 1
            window._archive_load_token = tokens["v"]
            return tokens["v"]

        window._archive_loader.request = _fake_request  # type: ignore[assignment]

        window._on_detection_selected(_det("2026-06-01T00:00:30", "2026-06-01T00:00:33"))
        first_token = window._archive_load_token
        window._on_detection_selected(_det("2026-06-01T00:05:30", "2026-06-01T00:05:33"))
        second_token = window._archive_load_token
        assert second_token > first_token

        # A result for the FIRST (now superseded) load is dropped.
        stale = ArchiveDetailResult(
            token=first_token,
            trigger_comp="Z",
            traces=_traces(float(_det().t_on), 10.0),
            elapsed_ms=1.0,
        )
        window._on_archive_detail_loaded(stale)
        assert not window._detail_pane.is_showing_archive()
    finally:
        window.close()


def test_3c_unit_change_dispatches_per_component(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        det = _det()
        window._archive_load_detection = det
        window._archive_load_token = 1
        # One component (N) has a gap → it is skipped (cannot deconvolve cleanly).
        result = ArchiveDetailResult(
            token=1,
            trigger_comp="Z",
            traces=_traces(float(det.t_on) - 10.0, 40.0, gap_comp="N"),
            elapsed_ms=1.0,
        )
        window._on_archive_detail_loaded(result)
        assert window._detail_pane.is_showing_archive()

        emitted: list[tuple] = []
        window._deconRequested.connect(lambda *a: emitted.append(a))

        window._on_unit_change_requested("VEL")

        # Z and E are gap-free → 2 requests; N (gapped) skipped.
        comps = {window._decon_components[a[0]] for a in emitted}
        assert comps == {"Z", "E"}
        assert all(a[3] == "VEL" for a in emitted)
    finally:
        window.close()
