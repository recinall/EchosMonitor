"""Off-process HVSR compute boundary (rule 1 / rule 10 GIL fix).

``hvsrpy``'s Konno-Ohmachi smoothing is numba-JIT and holds the CPython GIL
for several seconds per re-compute. Run on a ``QThread`` it STILL freezes the
GUI/engine thread and the SeedLink worker — the 2026-06-18 stall-watchdog
postmortem (a GIL-holding compute reads as a "silent" device) named the only
real fix: move the compute into a separate OS PROCESS so it cannot hold the
in-process GIL.

This module owns that boundary. It imports no Qt: the ``QThread`` worker
calls :meth:`HvsrComputeClient.compute` and blocks on the subprocess pipe —
a ``poll``/``recv`` syscall that RELEASES the GIL — so while ``hvsrpy`` runs
in the child the GUI render and the SeedLink data path keep scheduling.

Two implementations behind one Protocol:

* :class:`SubprocessHvsrComputeClient` — production. A persistent child
  process (``multiprocessing`` *spawn* context — never *fork*: forking a
  multi-``QThread`` Qt process inherits locks held by other threads and
  deadlocks, and inherits the SeedLink socket fds). The child is spawned
  lazily on the first compute, kept warm across recomputes and measurements
  (so numba JITs once), respawned if it dies, and torn down (bounded) on
  :meth:`close`. A compute is now genuinely INTERRUPTIBLE: ``should_stop``
  is polled every :data:`_POLL_INTERVAL_S` and a cancel ``terminate()``s the
  child (today's in-process numba compute could only be abandoned, never
  stopped — so a stop waited out the whole JIT).
* :class:`InProcessHvsrComputeClient` — calls ``accumulator.compute()``
  directly on the calling (worker) thread. Reproduces the pre-subprocess
  behaviour verbatim; it is the test suite's default (``tests/conftest.py``)
  so the Qt-threading tests stay fast and deterministic.

``HvsrAccumulator`` snapshots, ``HvsrSettings`` and ``HvsrResult`` are all
picklable (plain dataclasses / pydantic v2 / numpy / ``UTCDateTime``); the
spawn child inherits no file descriptors or sockets and imports ``hvsrpy``
only inside the compute, so the parent process never loads numba at all.

A spawn child re-imports the launcher's module graph to rebuild ``__main__``
(in a ``uv run`` checkout that is the console-script wrapper → this module's
target; in a PyInstaller bundle it re-runs ``packaging/entry.py``, which does
pull in PySide6 before :func:`multiprocessing.freeze_support` short-circuits
the child). The child therefore is NOT guaranteed Qt-free — but it never
constructs a ``QApplication`` or runs the GUI/event loop, only the compute
loop; importing PySide6 without instantiating it is harmless.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import sys
import threading
import time
import traceback
from typing import TYPE_CHECKING, Protocol

import structlog

from echosmonitor.core.exceptions import HvsrError

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess

    from echosmonitor.core.hvsr import HvsrAccumulator, HvsrResult

_log = structlog.get_logger(__name__)

# ``should_stop`` poll cadence while waiting on the child (rule 7: ≤ 100 ms).
_POLL_INTERVAL_S = 0.1
# Bounded join when terminating the child (rule 7).
_TERMINATE_JOIN_S = 2.0
# Bounded join for a graceful close before falling back to terminate.
_CLOSE_JOIN_S = 5.0

# Pipe message tags (parent → child / child → parent).
_REQ_COMPUTE = "compute"
_REQ_SHUTDOWN = "shutdown"
_RESP_OK = "ok"
_RESP_ERR = "err"

# structlog level (== logging.CRITICAL) below which the child drops its own
# compute log lines: the parent owns observability, and the child's default
# (unconfigured) logger would otherwise interleave on the shared stderr.
_CHILD_LOG_FLOOR = 50


class HvsrComputeClient(Protocol):
    """Run one ``accumulator.compute()`` off the calling thread (or in it)."""

    def compute(
        self, accumulator: HvsrAccumulator, *, should_stop: Callable[[], bool]
    ) -> HvsrResult | None:
        """Return the result, ``None`` if cancelled, or raise ``HvsrError``."""
        ...

    def close(self, *, timeout_s: float = _CLOSE_JOIN_S) -> None:
        """Release any resources (bounded, rule 7). Idempotent."""
        ...


class InProcessHvsrComputeClient:
    """Synchronous, same-thread compute — the pre-subprocess behaviour.

    ``should_stop`` is intentionally unobserved: an in-process numba compute
    cannot be interrupted mid-flight (the very problem the subprocess client
    solves). The worker's ``_superseded`` checks AROUND this call provide the
    same latest-wins guarantee the engine relied on before this module
    existed.
    """

    def compute(
        self, accumulator: HvsrAccumulator, *, should_stop: Callable[[], bool]
    ) -> HvsrResult | None:
        return accumulator.compute()

    def close(self, *, timeout_s: float = _CLOSE_JOIN_S) -> None:
        return None


def _compute_server_main(conn: Connection) -> None:
    """Child entry: serve compute requests over ``conn`` until shutdown.

    Top-level so ``multiprocessing`` spawn can pickle it by reference. Imports
    ``hvsrpy`` only transitively via ``accumulator.compute()``. Never raises
    across the pipe — a failed compute is an ``("err", message)`` response, a
    closed pipe ends the loop.
    """
    # A windowed (console=False) PyInstaller child has sys.stdout/sys.stderr ==
    # None. structlog's DEFAULT PrintLogger writes to sys.stdout, so the first
    # log line inside accumulator.compute() (and any matplotlib/hvsrpy print)
    # blows up with "cannot create weak reference to 'NoneType' object" — the
    # whole v0.1.3 Windows HVSR field bug, invisible on CI runners (which DO
    # have a console). Two defences: give the streams a real sink, AND pin the
    # child's structlog to that sink so it never reaches for sys.stdout at all.
    sink = open(os.devnull, "w")  # noqa: SIM115 (lives for the child's lifetime)
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink
    # Silence the compute's own INFO/WARNING lines (the parent owns
    # observability) AND route them to devnull, never the inherited stdout.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(_CHILD_LOG_FLOOR),
        logger_factory=structlog.PrintLoggerFactory(file=sink),
    )
    try:
        while True:
            try:
                tag, payload = conn.recv()
            except EOFError:
                return  # parent closed the pipe
            if tag == _REQ_SHUTDOWN:
                return
            if tag != _REQ_COMPUTE:
                continue
            try:
                result = payload.compute()
            except Exception as exc:
                # Forward the FULL traceback, not just str(exc): the child is a
                # separate OS process, so this pipe is the parent's ONLY window
                # into a failure. Without it the v0.1.3 Windows field bug read
                # as a bare "cannot create weak reference to 'NoneType' object"
                # with no frame — it was actually structlog's PrintLogger hitting
                # the windowed child's None sys.stdout (fixed in
                # _compute_server_main), NOT numba. Tuple: (short, traceback).
                conn.send((_RESP_ERR, (str(exc), traceback.format_exc())))
                continue
            conn.send((_RESP_OK, result))
    finally:
        with contextlib.suppress(OSError, ValueError):
            conn.close()


class SubprocessHvsrComputeClient:
    """Persistent spawn child running the compute off-process (GIL-free).

    Owned by ONE worker thread (the caller of :meth:`compute`); :meth:`close`
    is called by the owner only after that thread has joined. The internal
    lock is belt-and-suspenders against a stray concurrent call.

    **Degraded fallback.** If the spawn child cannot run the compute at all
    (its ENVIRONMENT is broken), the client re-runs that compute in-process
    and, on success, latches :attr:`subprocess_broken` so every later compute
    skips the doomed child. This forfeits the GIL protection (rule 1) on that
    platform but keeps HVSR FUNCTIONAL — a hard failure of the whole feature is
    the worse outcome. It is belt-and-suspenders: the one known break (the
    v0.1.3 None-``sys.stdout`` structlog crash in a windowed frozen child) is
    fixed at the source in :func:`_compute_server_main`, so on a current bundle
    the child runs and this path never triggers. See
    :meth:`_fallback_in_process`.
    """

    def __init__(self) -> None:
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._proc: BaseProcess | None = None
        self._conn: Connection | None = None
        self._closed = False
        # Latched True when the spawn child proves its ENVIRONMENT is unusable
        # (it fails a compute the parent then completes in-process). Set once,
        # never cleared: every later compute then skips the doomed child and
        # runs in-process. See :meth:`_fallback_in_process`.
        self._subprocess_broken = False

    @property
    def subprocess_broken(self) -> bool:
        """True once the client has fallen back to in-process compute.

        The off-process boundary is the GIL fix (rule 1); if the child cannot
        run at all the client degrades to in-process so HVSR still WORKS.
        Callers (e.g. the packaged ``--check``) read this to surface that the
        GIL protection is inactive without failing.
        """
        return self._subprocess_broken

    def _ensure_child(self) -> Connection:
        proc = self._proc
        if proc is not None and proc.is_alive() and self._conn is not None:
            return self._conn
        self._drop_child()
        parent_conn, child_conn = self._ctx.Pipe()
        proc = self._ctx.Process(
            target=_compute_server_main,
            args=(child_conn,),
            name="hvsr-compute",
            daemon=True,
        )
        proc.start()
        child_conn.close()  # the parent keeps only its own end
        self._proc = proc
        self._conn = parent_conn
        _log.info("hvsr_compute_subprocess_spawned", pid=proc.pid)
        return parent_conn

    def compute(
        self, accumulator: HvsrAccumulator, *, should_stop: Callable[[], bool]
    ) -> HvsrResult | None:
        with self._lock:
            if self._closed:
                raise HvsrError("hvsr compute client is closed")
            if self._subprocess_broken:
                # Environment proven unusable on a prior compute: skip the
                # doomed spawn and run in-process (degraded, GIL-bound). No
                # log here — the fallback was announced once when it latched.
                return self._compute_in_process(accumulator)
            conn = self._ensure_child()
            n_windows = accumulator.n_windows
            t0 = time.monotonic()
            _log.info("hvsr_subprocess_compute_start", n_windows=n_windows)
            try:
                conn.send((_REQ_COMPUTE, accumulator))
            except (OSError, ValueError) as exc:
                self._drop_child()
                raise HvsrError(f"hvsr compute subprocess send failed: {exc}") from exc
            while True:
                if should_stop():
                    # Forced interrupt: numba cannot be unwound, so kill the
                    # child (next compute respawns). Prompt — within one poll.
                    self._drop_child()
                    _log.info("hvsr_subprocess_compute_cancelled", n_windows=n_windows)
                    return None
                try:
                    ready = conn.poll(_POLL_INTERVAL_S)
                except (OSError, ValueError) as exc:
                    self._drop_child()
                    raise HvsrError(f"hvsr compute subprocess poll failed: {exc}") from exc
                if ready:
                    break
                proc = self._proc
                if proc is None or not proc.is_alive():
                    self._drop_child()
                    raise HvsrError("hvsr compute subprocess died mid-compute")
            try:
                tag, payload = conn.recv()
            except (EOFError, OSError) as exc:
                self._drop_child()
                raise HvsrError(f"hvsr compute subprocess recv failed: {exc}") from exc
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            if tag == _RESP_ERR:
                short, child_tb = (
                    payload if isinstance(payload, tuple) else (str(payload), "")
                )
                _log.warning(
                    "hvsr_subprocess_compute_error",
                    error=short,
                    child_traceback=child_tb,
                    elapsed_ms=round(elapsed_ms, 1),
                )
                # Distinguish a real input error from a broken child ENVIRONMENT
                # by re-running in-process (see _fallback_in_process).
                return self._fallback_in_process(accumulator, short)
            _log.info(
                "hvsr_subprocess_compute_done",
                n_windows=n_windows,
                elapsed_ms=round(elapsed_ms, 1),
            )
            return payload  # type: ignore[no-any-return]

    def _fallback_in_process(
        self, accumulator: HvsrAccumulator, child_error: str
    ) -> HvsrResult:
        """Resolve a child compute failure by re-running in-process.

        The child raised. Two causes are indistinguishable from the parent: a
        genuine INPUT error (a degenerate accumulator — in-process raises the
        SAME ``HvsrError``) and a broken child ENVIRONMENT (the parent process
        computes fine — as HVSR did before it moved off-process). So re-run the
        compute here:

        * in-process RAISES → it was a real input error: surface the child's
          (already-logged) message and keep the healthy child untouched.
        * in-process SUCCEEDS → the child environment is unusable. Latch the
          fallback (every later compute goes straight in-process), drop the
          doomed child, and announce it once. Degraded — the compute is now
          GIL-bound (rule 1's stutter) — but HVSR WORKS, which beats a hard
          failure for the whole feature on that platform.

        Caller holds ``self._lock``.
        """
        try:
            result = accumulator.compute()
        except Exception as exc:
            raise HvsrError(child_error) from exc
        if not self._subprocess_broken:
            self._subprocess_broken = True
            _log.warning(
                "hvsr_subprocess_fallback_in_process",
                reason="off-process compute failed but in-process succeeded",
                detail="GIL protection (rule 1) is now inactive for HVSR",
                child_error=child_error,
            )
            self._drop_child()
        return result

    def _compute_in_process(self, accumulator: HvsrAccumulator) -> HvsrResult:
        """Run the compute on the calling thread (latched-fallback fast path).

        Like :class:`InProcessHvsrComputeClient`, ``should_stop`` is unobserved
        here: numba cannot be unwound mid-flight, so a cancel waits out the
        whole compute (rule 7 is degraded in this already-degraded path). The
        worker's latest-wins checks AROUND the call still hold between computes.
        """
        try:
            return accumulator.compute()
        except HvsrError:
            raise
        except Exception as exc:
            raise HvsrError(f"in-process hvsr compute failed: {exc}") from exc

    def close(self, *, timeout_s: float = _CLOSE_JOIN_S) -> None:
        with self._lock:
            self._closed = True
            proc = self._proc
            conn = self._conn
            if proc is None:
                return
            if conn is not None and proc.is_alive():
                with contextlib.suppress(OSError, ValueError):
                    conn.send((_REQ_SHUTDOWN, None))
                proc.join(timeout_s)
            self._terminate(proc)
            self._proc = None
            self._conn = None

    def _drop_child(self) -> None:
        """Terminate and forget the current child (the next compute respawns)."""
        proc, conn = self._proc, self._conn
        self._proc = None
        self._conn = None
        if conn is not None:
            with contextlib.suppress(OSError, ValueError):
                conn.close()
        if proc is not None:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc: BaseProcess) -> None:
        if not proc.is_alive():
            return
        proc.terminate()
        proc.join(_TERMINATE_JOIN_S)
        if proc.is_alive():
            proc.kill()
            proc.join(_TERMINATE_JOIN_S)


def make_default_compute_client() -> HvsrComputeClient:
    """Factory for the production (subprocess) client.

    The engines call THIS (module-attribute lookup at worker-build time) when
    no explicit factory is injected, so ``tests/conftest.py`` can point it at
    the in-process client for the whole suite while the new boundary tests
    construct :class:`SubprocessHvsrComputeClient` directly.
    """
    return SubprocessHvsrComputeClient()
