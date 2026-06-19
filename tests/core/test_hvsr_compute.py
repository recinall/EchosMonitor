"""The off-process HVSR compute boundary (``core/hvsr_compute.py``).

These tests drive the REAL spawn subprocess (not the in-process default the
rest of the suite uses) — that is the point: they pin the serialization
round-trip, prompt cancellation (the child is actually killed), the error
channel, automatic respawn after a child death, and bounded close. They run
``hvsrpy`` in the child, so each cold client pays one numba JIT; kept small
on purpose (2 s windows, 64 centre frequencies).
"""

from __future__ import annotations

import multiprocessing
import sys

import numpy as np
import pytest
import structlog
from obspy import UTCDateTime

from echosmonitor.core import hvsr_compute as hc
from echosmonitor.core.exceptions import HvsrError
from echosmonitor.core.hvsr import HvsrAccumulator, HvsrSettings
from echosmonitor.core.hvsr_compute import (
    InProcessHvsrComputeClient,
    SubprocessHvsrComputeClient,
    make_default_compute_client,
)

_FS = 100.0
_SETTINGS = HvsrSettings(
    window_length_s=2.0, freqmin_hz=1.0, freqmax_hz=20.0, resample_n=64
)


def _accumulator(*, seed: int = 0, n_windows: int = 4) -> HvsrAccumulator:
    acc = HvsrAccumulator(
        _SETTINGS,
        same_response=True,
        same_response_detail="test",
        device="dev",
        station_key="XX.DEV",
        provenance="archive",
    )
    rng = np.random.default_rng(seed)
    n = int(_SETTINGS.window_length_s * _FS)
    t0 = UTCDateTime(0)
    for i in range(n_windows):
        acc.add_window(
            rng.standard_normal(n),
            rng.standard_normal(n),
            rng.standard_normal(n),
            t0 + i * _SETTINGS.window_length_s,
            _FS,
        )
    return acc


def _never_stop() -> bool:
    return False


def test_default_factory_is_subprocess_client() -> None:
    """Production default is the subprocess client (the suite overrides it)."""
    client = make_default_compute_client()
    try:
        assert isinstance(client, SubprocessHvsrComputeClient)
    finally:
        client.close()


def test_subprocess_round_trip_matches_in_process() -> None:
    """A subprocess compute equals the same compute run in-process (bit-for-bit).

    Same accumulator, same deterministic data → identical curves and f0; the
    only difference is the process the numba ran in. A second compute on the
    warm client must also succeed (the child is reused, not respawned).
    """
    in_proc = InProcessHvsrComputeClient().compute(_accumulator(), should_stop=_never_stop)
    assert in_proc is not None

    client = SubprocessHvsrComputeClient()
    try:
        sub = client.compute(_accumulator(), should_stop=_never_stop)
        assert sub is not None
        np.testing.assert_allclose(sub.frequency, in_proc.frequency)
        np.testing.assert_allclose(sub.mean_curve, in_proc.mean_curve)
        assert sub.f0_hz == pytest.approx(in_proc.f0_hz)
        # Warm reuse: the second compute lands on the SAME child process.
        pid = client._proc.pid
        again = client.compute(_accumulator(seed=1), should_stop=_never_stop)
        assert again is not None
        assert client._proc is not None and client._proc.pid == pid
    finally:
        client.close()


def test_cancel_kills_child_and_returns_none() -> None:
    """``should_stop`` true → compute returns None and the child is killed."""
    client = SubprocessHvsrComputeClient()
    try:
        # Spawn the child with one real compute, then cancel the next one.
        assert client.compute(_accumulator(), should_stop=_never_stop) is not None
        proc = client._proc
        assert proc is not None and proc.is_alive()

        result = client.compute(_accumulator(), should_stop=lambda: True)
        assert result is None
        # The cancelled child was terminated and forgotten; the next compute
        # transparently respawns a fresh one.
        proc.join(2.0)
        assert not proc.is_alive()
        assert client._proc is None
        assert client.compute(_accumulator(), should_stop=_never_stop) is not None
    finally:
        client.close()


def test_compute_error_raises_and_child_survives() -> None:
    """A compute failure in the child surfaces as HvsrError; the child lives on.

    An empty accumulator makes ``compute()`` raise inside the child — that is
    an ``("err", ...)`` response, NOT a crash, so the same warm child serves
    the next (valid) compute.
    """
    empty = HvsrAccumulator(
        _SETTINGS,
        same_response=True,
        same_response_detail="test",
        device="dev",
        station_key="XX.DEV",
        provenance="archive",
    )
    client = SubprocessHvsrComputeClient()
    try:
        with pytest.raises(HvsrError):
            client.compute(empty, should_stop=_never_stop)
        proc = client._proc
        assert proc is not None and proc.is_alive()  # error did not kill it
        assert client.compute(_accumulator(), should_stop=_never_stop) is not None
        assert client._proc is proc
    finally:
        client.close()


def test_respawns_after_child_death() -> None:
    """If the child dies between computes, the next compute spawns a new one."""
    client = SubprocessHvsrComputeClient()
    try:
        assert client.compute(_accumulator(), should_stop=_never_stop) is not None
        dead = client._proc
        assert dead is not None
        old_pid = dead.pid
        dead.kill()
        dead.join(2.0)
        assert not dead.is_alive()

        result = client.compute(_accumulator(), should_stop=_never_stop)
        assert result is not None
        assert client._proc is not None
        assert client._proc.pid != old_pid
    finally:
        client.close()


def test_close_is_bounded_idempotent_and_terminal() -> None:
    """``close`` kills the child, is idempotent, and refuses further computes."""
    client = SubprocessHvsrComputeClient()
    assert client.compute(_accumulator(), should_stop=_never_stop) is not None
    proc = client._proc
    assert proc is not None

    client.close()
    proc.join(2.0)
    assert not proc.is_alive()
    client.close()  # idempotent — no raise, nothing alive

    with pytest.raises(HvsrError):
        client.compute(_accumulator(), should_stop=_never_stop)


def test_in_process_client_ignores_should_stop() -> None:
    """The in-process client computes synchronously and never returns None."""
    client = InProcessHvsrComputeClient()
    result = client.compute(_accumulator(), should_stop=lambda: True)
    assert result is not None  # should_stop is unobserved in-process
    client.close()  # no-op, no raise


# --- degraded in-process fallback (belt-and-suspenders) ----------------------
#
# If the spawn child's ENVIRONMENT is unusable the child fails every compute the
# PARENT can still complete in-process. The client must keep HVSR working by
# falling back, and latch so it stops re-spawning the doomed child. These tests
# force that branch with a fake child conn (no real broken child needed) so they
# stay fast and deterministic on every platform.


class _FakeConn:
    """A pipe end that always answers one canned (tag, payload)."""

    def __init__(self, response: object) -> None:
        self._response = response
        self.sent: list[object] = []

    def send(self, obj: object) -> None:
        self.sent.append(obj)

    def poll(self, timeout: float | None = None) -> bool:
        return True

    def recv(self) -> object:
        return self._response

    def close(self) -> None:
        pass


class _FakeProc:
    pid = 4321

    def is_alive(self) -> bool:
        return True

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        return None


def _wire_broken_child(client: SubprocessHvsrComputeClient, response: object) -> _FakeConn:
    """Make ``_ensure_child`` hand back a fake child that returns ``response``."""
    fake_conn = _FakeConn(response)

    def _ensure() -> _FakeConn:
        client._proc = _FakeProc()  # type: ignore[assignment]
        client._conn = fake_conn  # type: ignore[assignment]
        return fake_conn

    client._ensure_child = _ensure  # type: ignore[method-assign]
    return fake_conn


def test_falls_back_in_process_when_child_environment_broken() -> None:
    """Child fails a compute the parent completes in-process → degrade + latch."""
    client = SubprocessHvsrComputeClient()
    try:
        _wire_broken_child(
            client, (hc._RESP_ERR, ("cannot create weak reference to 'NoneType' object", "tb"))
        )
        assert client.subprocess_broken is False

        result = client.compute(_accumulator(), should_stop=_never_stop)
        assert result is not None  # HVSR still works via the in-process fallback
        assert client.subprocess_broken is True  # latched
        assert client._proc is None  # doomed child dropped

        # A second compute takes the latched fast path: no child is spawned.
        again = client.compute(_accumulator(seed=1), should_stop=_never_stop)
        assert again is not None
        assert client._proc is None
    finally:
        client.close()


def test_child_error_on_bad_input_propagates_and_does_not_latch() -> None:
    """A real INPUT error (in-process raises the SAME way) must NOT latch."""
    empty = HvsrAccumulator(
        _SETTINGS,
        same_response=True,
        same_response_detail="test",
        device="dev",
        station_key="XX.DEV",
        provenance="archive",
    )
    client = SubprocessHvsrComputeClient()
    try:
        _wire_broken_child(client, (hc._RESP_ERR, ("no windows accumulated", "tb")))
        with pytest.raises(HvsrError):
            client.compute(empty, should_stop=_never_stop)
        # In-process reproduced the failure → genuine input error, not a broken
        # environment: the fallback must stay off.
        assert client.subprocess_broken is False
    finally:
        client.close()


def test_child_main_survives_none_std_streams() -> None:
    """The child entry must not crash when sys.stdout/sys.stderr are None.

    THE v0.1.3 Windows root cause: a windowed (console=False) PyInstaller child
    has ``sys.stdout is None``; structlog's default ``PrintLogger`` then dies on
    ``weakref(None)`` at the FIRST compute log line — surfacing as
    ``HvsrError: cannot create weak reference to 'NoneType' object`` and breaking
    ALL HVSR on Windows. Drive ``_compute_server_main`` directly with the streams
    forced to None: with the fix it must return an OK result, not crash.
    """
    saved_out, saved_err = sys.stdout, sys.stderr
    saved_cfg = structlog.get_config()  # the child calls structlog.configure()
    parent, child = multiprocessing.Pipe()
    try:
        sys.stdout = None  # type: ignore[assignment]
        sys.stderr = None  # type: ignore[assignment]
        parent.send((hc._REQ_COMPUTE, _accumulator()))
        parent.send((hc._REQ_SHUTDOWN, None))
        # Runs in this thread; returns on the shutdown message above.
        hc._compute_server_main(child)
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        structlog.configure(**saved_cfg)  # restore the suite's config
    tag, payload = parent.recv()
    assert tag == hc._RESP_OK
    assert payload is not None
    parent.close()
