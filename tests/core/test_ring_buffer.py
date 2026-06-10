"""Unit tests for `core.ring_buffer.RingBuffer`."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from obspy import UTCDateTime

from echosmonitor.core.ring_buffer import RingBuffer


def test_init_validation() -> None:
    with pytest.raises(ValueError):
        RingBuffer(0, 100.0)
    with pytest.raises(ValueError):
        RingBuffer(10, 0.0)


def test_push_below_capacity_no_drops() -> None:
    rb = RingBuffer(10, 100.0)
    dropped = rb.push(np.arange(7, dtype=np.float32))
    assert dropped == 0
    arr, total = rb.read_all()
    assert total == 7
    np.testing.assert_array_equal(arr, np.arange(7, dtype=np.float32))


def test_push_oversize_drops_oldest_input() -> None:
    rb = RingBuffer(5, 100.0)
    # 8 samples into a 5-sample ring → 3 input samples dropped, last 5 retained
    dropped = rb.push(np.arange(8, dtype=np.float32))
    assert dropped == 3
    arr, total = rb.read_all()
    assert total == 8
    np.testing.assert_array_equal(arr, np.arange(3, 8, dtype=np.float32))


def test_repeated_push_evicts_oldest() -> None:
    rb = RingBuffer(4, 100.0)
    rb.push(np.array([1, 2, 3], dtype=np.float32))
    dropped = rb.push(np.array([4, 5, 6], dtype=np.float32))
    # 6 samples pushed total, capacity 4: 2 evicted on the second push
    assert dropped == 2
    arr, total = rb.read_all()
    assert total == 6
    np.testing.assert_array_equal(arr, np.array([3, 4, 5, 6], dtype=np.float32))


def test_read_last_truncates_when_underfilled() -> None:
    rb = RingBuffer(10, 100.0)
    rb.push(np.array([1, 2, 3], dtype=np.float32))
    out = rb.read_last(5)
    np.testing.assert_array_equal(out, np.array([1, 2, 3], dtype=np.float32))


def test_read_last_after_wrap() -> None:
    rb = RingBuffer(5, 100.0)
    rb.push(np.arange(8, dtype=np.float32))  # ring now: [3,4,5,6,7], write_idx=3
    out = rb.read_last(3)
    np.testing.assert_array_equal(out, np.array([5, 6, 7], dtype=np.float32))
    out_full = rb.read_last(10)  # clamped to capacity
    np.testing.assert_array_equal(out_full, np.array([3, 4, 5, 6, 7], dtype=np.float32))


def test_dtype_coerced_to_float32() -> None:
    rb = RingBuffer(8, 100.0)
    rb.push(np.array([1, 2, 3], dtype=np.int32))
    out, _ = rb.read_all()
    assert out.dtype == np.float32


def test_latest_t_advances() -> None:
    rb = RingBuffer(8, 100.0)
    assert rb.latest_t is None
    t0 = UTCDateTime("2026-05-08T00:00:00.000Z")
    rb.push(np.zeros(3, dtype=np.float32), end_time=t0)
    assert rb.latest_t == t0
    t1 = t0 + 1.0
    rb.push(np.zeros(3, dtype=np.float32), end_time=t1)
    assert rb.latest_t == t1


def test_concurrent_push_read_does_not_corrupt() -> None:
    rb = RingBuffer(1024, 100.0)
    stop = threading.Event()
    errors: list[Exception] = []

    def writer() -> None:
        i = 0
        try:
            while not stop.is_set():
                rb.push(np.full(50, fill_value=float(i % 1000), dtype=np.float32))
                i += 1
        except Exception as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                arr, _ = rb.read_all()
                # Must always come back as float32, length <= capacity
                assert arr.dtype == np.float32
                assert arr.shape[0] <= 1024
                rb.read_last(128)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer)]
    for _ in range(3):
        threads.append(threading.Thread(target=reader))
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=2.0)
    assert not errors, f"concurrency errors: {errors}"
