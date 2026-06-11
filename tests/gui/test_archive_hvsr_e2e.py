"""Archive → HVSR hand-off, end to end (M3-E acceptance).

A REAL HVSR measurement (``HvsrEngine.start_archive_measurement`` — no
monkeypatch) runs over a CLOSED session's archive with the engine fully
idle: browse the session, hand off to the HVSR tab, click "Run on
archive", recover the injected f0. The data exists ONLY under the
session root ``<base>/<project>/`` (rule 14) — the live engine roots
cannot reach it — so a recovered f0 proves the session-rooted read.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from obspy import Trace, UTCDateTime
from scipy import signal

from echosmonitor.core.models import StreamID
from echosmonitor.core.session import session_archive_root
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.sds import device_sds_root, sds_path

_FS = 100.0
_F0 = 6.0
_T0 = UTCDateTime("2026-05-10T12:00:00")
_DURATION_S = 600.0
_DEVICE = "echos-1"
_STA = "XX.STA.00.HH"
_GROUP = {
    "Z": "XX.STA.00.HHZ",
    "N": "XX.STA.00.HHN",
    "E": "XX.STA.00.HHE",
}


def _resonant(n: int, rng: np.random.Generator) -> np.ndarray:
    b, a = signal.iirpeak(_F0 / (_FS / 2.0), 25.0)
    w = rng.standard_normal(n)
    return w * 0.3 + signal.lfilter(b, a, w) * 4.0


def _seed_hvsr_session(base: Path, project: str) -> Path:
    """A closed recorded session with a known resonance: Z white, N/E
    resonant at ``_F0`` — continuous (gap-free) so every window is usable."""
    root = session_archive_root(base, project)
    dao = ArchiveDao(root / "archive.db")
    sid = dao.start_session("host", "v", "hash", project_name=project, devices=(_DEVICE,))
    dev_id = dao.upsert_device(_DEVICE, "h", 18000, {})
    n = int(_DURATION_S * _FS)
    rng = np.random.default_rng(5)
    comp_data = {
        "HHZ": rng.standard_normal(n),
        "HHN": _resonant(n, rng),
        "HHE": _resonant(n, rng),
    }
    for cha, data in comp_data.items():
        stream_id = dao.upsert_stream(dev_id, ("XX", "STA", "00", cha), _FS)
        sid_stream = StreamID("XX", "STA", "00", cha)
        tr = Trace(data=(data * 1000.0).astype(np.int32))
        tr.stats.network, tr.stats.station = "XX", "STA"
        tr.stats.location, tr.stats.channel = "00", cha
        tr.stats.sampling_rate = _FS
        tr.stats.starttime = _T0
        path = sds_path(device_sds_root(root, _DEVICE), _T0, sid_stream)
        path.parent.mkdir(parents=True, exist_ok=True)
        tr.write(str(path), format="MSEED")
        dao.record_file(stream_id, path, _T0, _T0 + _DURATION_S, path.stat().st_size)
    dao.end_session(sid)
    dao.close()
    return root


def test_closed_session_hvsr_end_to_end(qtbot, tmp_path) -> None:
    from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
    from echosmonitor.core.hvsr import HvsrResult
    from echosmonitor.gui.main_window import MainWindow

    base = tmp_path / "archive"
    _seed_hvsr_session(base, "field day")

    cfg = RootConfig(app=AppConfig(archive_root=base), ui=UiConfig(), devices=[])
    window = MainWindow(cfg, tmp_path / "config.yaml")
    qtbot.addWidget(window)
    try:
        assert window._engine.active_session() is None  # rule 13: idle
        tab = window._archive_tab
        qtbot.waitUntil(lambda: len(tab.session_rows_for_test()) >= 1, timeout=10_000)
        tab.select_session_for_test(0)
        qtbot.waitUntil(
            lambda: tab.station_strip_for_test(_DEVICE, _STA) is not None,
            timeout=10_000,
        )
        assert tab.select_station_for_test(_DEVICE, _STA)
        group = tab.selected_group()
        assert group is not None
        assert set(group.values()) == set(_GROUP.values())

        # The hand-off the Archive tab's HVSR button drives (its exact-emit
        # contract is pinned by test_handoff_button_emits_exact_selection).
        window._handoff_archive_to_hvsr(
            _DEVICE, group, float(_T0.timestamp), float((_T0 + _DURATION_S).timestamp)
        )

        hv = window._hvsr_widget
        assert window._central_tabs.currentWidget() is hv
        # The prefill must select the handed-off station even though the
        # engine is idle (no live buffers) — the station comes from the
        # browsed ARCHIVE, not the live engine.
        assert hv._device_combo.currentData() == _DEVICE
        sel = hv.selected_group()
        assert sel is not None
        assert set(sel.values()) == set(group.values())

        hv._wl_spin.setValue(30)  # ~20 windows in 600 s; faster than the 60 s default
        results: list[object] = []
        window._hvsr_engine.hvsrUpdated.connect(results.append)

        hv._archive_button.click()  # real handler → real start_archive_measurement

        assert hv._measurement_id, "the archive measurement did not start"
        qtbot.waitUntil(lambda: len(results) >= 1, timeout=60_000)
        res = results[0]
        assert isinstance(res, HvsrResult)
        assert res.provenance == "archive"
        assert abs(res.f0_hz - _F0) / _F0 < 0.15, f"recovered f0={res.f0_hz}"
        # Still no engine session: the read came from the closed session root.
        assert window._engine.active_session() is None
    finally:
        window.close()
