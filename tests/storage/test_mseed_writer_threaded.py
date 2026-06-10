"""Threaded behaviour of ``MseedWriter``.

These tests exercise the parts that depend on a Qt event loop on the
writer's own thread: the QTimer-driven periodic fsync and the
BlockingQueuedConnection close pattern. File-level encoding/I/O
behaviour is covered by ``test_mseed_writer.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from obspy import Trace, UTCDateTime
from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, Signal

from echosmonitor.config.schema import ArchiveConfig
from echosmonitor.core.models import StreamID
from echosmonitor.storage.mseed_writer import MseedWriter
from echosmonitor.storage.sds import device_sds_root, sds_path

_DEVICE = "dev1"

# Slightly longer than the writer's fsync interval below, plus headroom
# for signal-machinery latency on slow CI.
_TIMER_WAIT_MS = 3_000


def _make_trace() -> Trace:
    return Trace(
        data=(np.arange(512, dtype=np.int32) % 1000),
        header={
            "network": "IU",
            "station": "ANMO",
            "location": "00",
            "channel": "BHZ",
            "starttime": UTCDateTime("2026-05-09T12:00:00"),
            "sampling_rate": 100.0,
        },
    )


class _Sender(QObject):
    """Helper: emits ``request`` from the engine/test thread; queued to writer."""

    request = Signal(str, object)  # nslc, trace


@pytest.fixture
def writer_on_thread(qapp: Any, tmp_path: Path) -> Iterator[tuple[MseedWriter, QThread, _Sender]]:
    """Build a MseedWriter on its own QThread, started and ready."""
    cfg = ArchiveConfig(
        enabled=True,
        encoding="STEIM2",
        record_length=512,
        fsync_interval_s=0.5,  # tight to keep tests quick
    )
    writer = MseedWriter(_DEVICE, tmp_path, cfg)
    thread = QThread()
    writer.moveToThread(thread)
    thread.started.connect(writer.start)
    sender = _Sender()
    sender.request.connect(writer.write_trace, Qt.ConnectionType.QueuedConnection)
    thread.start()
    try:
        yield writer, thread, sender
    finally:
        # Block for close so the thread shuts down cleanly even on
        # test failure.
        QMetaObject.invokeMethod(
            writer,
            "close_all",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        thread.quit()
        assert thread.wait(_TIMER_WAIT_MS), "writer thread did not exit"


def test_queued_write_trace_then_timer_fsync_emits_flushed_file(
    writer_on_thread: tuple[MseedWriter, QThread, _Sender],
    qtbot: Any,
    tmp_path: Path,
) -> None:
    """write_trace via queued signal, wait for the timer-driven flush."""
    writer, _, sender = writer_on_thread

    with qtbot.waitSignal(writer.flushedFile, timeout=_TIMER_WAIT_MS) as blocker:
        sender.request.emit("IU.ANMO.00.BHZ", _make_trace())
    device, nslc, path, t_start, t_end, bytes_added, file_size = blocker.args
    assert device == _DEVICE
    assert nslc == "IU.ANMO.00.BHZ"
    assert path == sds_path(
        device_sds_root(tmp_path, _DEVICE),
        UTCDateTime("2026-05-09T12:00:00"),
        StreamID("IU", "ANMO", "00", "BHZ"),
    )
    assert bytes_added > 0
    # file_size sampled via os.fstat after fsync — must equal the
    # actual on-disk file size at that moment.
    assert file_size == path.stat().st_size
    assert t_end >= t_start


def test_close_all_via_blocking_queued_connection_blocks_until_done(
    writer_on_thread: tuple[MseedWriter, QThread, _Sender],
    qtbot: Any,
    tmp_path: Path,
) -> None:
    """``BlockingQueuedConnection`` close returns only after the slot finished."""
    writer, _, sender = writer_on_thread

    with qtbot.waitSignal(writer.writeOk, timeout=_TIMER_WAIT_MS):
        sender.request.emit("IU.ANMO.00.BHZ", _make_trace())

    # Blocking close: the call below returns synchronously — the
    # storage thread has finished close_all and there are no open
    # file handles afterwards.
    QMetaObject.invokeMethod(
        writer,
        "close_all",
        Qt.ConnectionType.BlockingQueuedConnection,
    )
    assert writer._open_files == {}  # type: ignore[attr-defined]
