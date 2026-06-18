"""The off-process HVSR compute boundary (``core/hvsr_compute.py``).

These tests drive the REAL spawn subprocess (not the in-process default the
rest of the suite uses) — that is the point: they pin the serialization
round-trip, prompt cancellation (the child is actually killed), the error
channel, automatic respawn after a child death, and bounded close. They run
``hvsrpy`` in the child, so each cold client pays one numba JIT; kept small
on purpose (2 s windows, 64 centre frequencies).
"""

from __future__ import annotations

import numpy as np
import pytest
from obspy import UTCDateTime

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
