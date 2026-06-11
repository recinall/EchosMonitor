"""Archive re-index worker (M3-D) — off-thread tree walk + DB writes.

Skill ``qt-worker-threading`` §7 obligations for a new worker
(start→stop→start, stop-during-busy) plus the serial-queue contract it
shares with the M3-C export worker: a second request must NOT cancel an
in-flight one (each is an explicit user action on a directory).
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from obspy import Trace, UTCDateTime
from PySide6.QtCore import QObject, Slot

from echosmonitor.core.archive_reindex_worker import (
    ArchiveReindexLoader,
    ArchiveReindexProgressEvent,
    ArchiveReindexResult,
)
from echosmonitor.core.models import StreamID
from echosmonitor.storage import reindex as reindex_mod
from echosmonitor.storage.sds import device_sds_root, sds_path

_FS = 100.0
_T0 = UTCDateTime("2026-05-10T12:00:00")
_DEVICE = "echos-1"


def _write_component(root: Path, comp: str) -> Path:
    sid = StreamID("XX", "STA", "00", f"HH{comp}")
    path = sds_path(device_sds_root(root, _DEVICE), _T0, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    Trace(
        data=np.zeros(int(_FS * 30), dtype=np.int32),
        header={
            "network": "XX",
            "station": "STA",
            "location": "00",
            "channel": f"HH{comp}",
            "starttime": _T0,
            "sampling_rate": _FS,
        },
    ).write(str(path), format="MSEED")
    return path


def _seed_tree(root: Path) -> None:
    for c in ("Z", "N", "E"):
        _write_component(root, c)


class _Collector(QObject):
    def __init__(self, loader: ArchiveReindexLoader) -> None:
        super().__init__()
        self.done: list[ArchiveReindexResult] = []
        self.failed: list[tuple[int, str]] = []
        self.progress: list[ArchiveReindexProgressEvent] = []
        loader.finished.connect(self._on_done)
        loader.failed.connect(lambda tok, msg: self.failed.append((tok, msg)))
        loader.progressed.connect(self._on_progress)

    @Slot(object)
    def _on_done(self, payload: object) -> None:
        if isinstance(payload, ArchiveReindexResult):
            self.done.append(payload)

    @Slot(object)
    def _on_progress(self, payload: object) -> None:
        if isinstance(payload, ArchiveReindexProgressEvent):
            self.progress.append(payload)


def test_reindex_builds_db_and_reports(qtbot, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _seed_tree(root)
    loader = ArchiveReindexLoader()
    col = _Collector(loader)
    try:
        tok = loader.request(str(root), host="lab", version="1.0")
        qtbot.waitUntil(lambda: bool(col.done) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        result = col.done[0]
        assert result.token == tok
        assert result.report.files_indexed == 3
        assert result.report.synthesized_session
        assert (root / "archive.db").is_file()
        # The final progress beat always arrives (throttle exempts it).
        assert col.progress and col.progress[-1].files_done == 3
    finally:
        loader.shutdown()


def test_requests_are_a_serial_queue_not_latest_wins(qtbot, tmp_path: Path) -> None:
    root_a = tmp_path / "proj_a"
    root_b = tmp_path / "proj_b"
    _seed_tree(root_a)
    _seed_tree(root_b)
    loader = ArchiveReindexLoader()
    col = _Collector(loader)
    try:
        tok_a = loader.request(str(root_a), host="h", version="v")
        tok_b = loader.request(str(root_b), host="h", version="v")
        qtbot.waitUntil(lambda: len(col.done) >= 2 or bool(col.failed), timeout=15_000)
        assert not col.failed, col.failed
        assert {r.token for r in col.done} == {tok_a, tok_b}
        assert (root_a / "archive.db").is_file()
        assert (root_b / "archive.db").is_file()
    finally:
        loader.shutdown()


def test_failure_emits_failed_and_thread_survives(qtbot, tmp_path: Path) -> None:
    good = tmp_path / "proj"
    _seed_tree(good)
    loader = ArchiveReindexLoader()
    col = _Collector(loader)
    try:
        loader.request(str(tmp_path / "no_such_dir"), host="h", version="v")
        qtbot.waitUntil(lambda: bool(col.failed), timeout=10_000)
        # The worker thread survives — the next request completes.
        loader.request(str(good), host="h", version="v")
        qtbot.waitUntil(lambda: bool(col.done), timeout=10_000)
        assert (good / "archive.db").is_file()
    finally:
        loader.shutdown()


def test_shutdown_during_busy_reindex_is_bounded_then_serves_again(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "proj"
    _seed_tree(root)

    real_truth = reindex_mod._read_file_truth
    started = {"n": 0}

    def _slow_truth(candidate):  # type: ignore[no-untyped-def]
        started["n"] += 1
        time.sleep(0.6)  # each per-file read straddles the shutdown
        return real_truth(candidate)

    monkeypatch.setattr(reindex_mod, "_read_file_truth", _slow_truth)

    loader = ArchiveReindexLoader()
    col = _Collector(loader)
    loader.request(str(root), host="h", version="v")
    qtbot.waitUntil(lambda: started["n"] >= 1, timeout=10_000)

    t0 = time.monotonic()
    loader.shutdown()
    assert time.monotonic() - t0 < 4.5  # the join bound (rule 7)
    qtbot.wait(100)
    # Cancelled mid-run: nothing announced (the partial index on disk is
    # safe — files win over it and a re-run converges).
    assert not col.done

    # start → stop → start (skill §7): the loader serves again, and the
    # re-run converges on the full index.
    monkeypatch.undo()
    try:
        loader.request(str(root), host="h", version="v")
        qtbot.waitUntil(lambda: bool(col.done), timeout=10_000)
        assert col.done[0].report.files_indexed == 3
        assert col.done[0].report.synthesized_session
    finally:
        loader.shutdown()
