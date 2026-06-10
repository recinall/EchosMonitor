---
name: qt-worker-threading
description: The proven QThread/worker/signal patterns this codebase standardised after real postmortems. ALWAYS consult before creating or modifying any worker, QThread, engine lifecycle (start/stop/shutdown), cross-thread signal, QTimer on a worker thread, or anything that polls/loads off the GUI thread (REST poller, archive loaders, HVSR compute, deconvolution).
---

# Qt worker patterns (EchosMonitor canon)

Copy these patterns; do not invent new ones. Each maps to a working class in
the repo — read it before writing yours.

## 1. The standard worker (reference: `ArchiveDetailLoader` / `HvsrEngine`)

- Worker = **parentless** `QObject`, `moveToThread(QThread)`.
- Owner→worker: private signals (`_loadRequested = Signal(object)`) connected
  with `Qt.ConnectionType.QueuedConnection` → slot body runs on the worker
  thread. Never call worker methods directly.
- Worker→owner: result signals, QueuedConnection → handlers run on the GUI
  thread. Payloads are frozen dataclasses passed as `Signal(object)` with an
  `isinstance` guard in the receiving slot (Qt type-erases them).
- Worker **never raises across the boundary**: every slot body is
  try/except → `failed` signal.
- Thread started lazily on first request; `setObjectName("…")` always.

## 2. Cooperative stop + latest-wins token

- `self._stop: bool` written GIL-atomically from the owner (direct attribute
  write IS the synchronous path; the queued `request_stop` slot is
  belt-and-suspenders for the idle case — a queued stop alone can never
  preempt an in-flight slot).
- Supersedable work: monotonic `token` written to `worker._active_token`
  from the owner; the worker checks `token != self._active_token` between
  phases and before emitting.
- Long loops poll the flag ≤ every 100 ms (rule 7).

## 3. Shutdown sequence (reference: `StreamingEngine.stop`, `AIEngine.disengage` pre-removal)

```
set stop flags synchronously
emit queued stop/release (belt-and-suspenders)
[if state must be cleared before quit] emit via BlockingQueuedConnection  ← barrier
thread.quit()
if not thread.wait(BOUNDED_MS): log.warning("…_join_timeout")
disconnect bridge signals only AFTER the join, wrapped in
    contextlib.suppress(RuntimeError, TypeError)
```

Why the barrier: `QThread.quit()` interrupts the dispatcher and can preempt
a plain-queued "clear state" slot → stale state across stop/start (the
~5 % flake postmortem). BlockingQueuedConnection is safe only because the
emitter thread ≠ receiver thread — assert that mentally every time.

## 4. Never park a worker thread

A queued slot cannot dispatch into a thread parked in `queue.get()` /
blocking `recv` / `time.sleep`. Workers are event-loop-driven (slots run
serially on `exec()`), OR — for blocking-by-design sessions like the
SeedLink client — the owner-side `stop()` is a **plain method (not a Slot)**
that flips flags and nudges the socket from the calling thread (reference:
`InfoWorker.stop`, `SeedLinkWorker.stop`).

Socket nudge order (Linux won't always wake a recv on cross-thread close):
`settimeout(0.1)` → `shutdown(SHUT_RDWR)` → `close()`.

## 5. QTimers on worker threads

Construct the timer inside a `@Slot() start()` invoked AFTER moveToThread
(so the timer's thread affinity is the worker thread) — reference:
`MseedWriter.start`. Owner kicks it with
`QMetaObject.invokeMethod(worker, "start", QueuedConnection)`.

## 6. GUI-thread budget

On the GUI thread you may: read ring-buffer snapshots (`read_recent`),
`setData` on plots (deferred via same-thread QueuedConnection so it never
runs inside the flush tick — rule 11), mutate ConfigStore, write the
metadata DAO (it is funnelled there by design). You may NOT: obspy.read,
httpx calls, fsync, big array builds, hvsrpy, deconvolution — the GIL hold
starves the SeedLink worker (the reverted Archive-Replay postmortem).

## 7. Bounds + logging

Every queue: explicit bound, drop-oldest, drop counter, warn log throttled
to one line / 5 s / stream. Every wait: timeout + structured start/done/
elapsed logs. Tests for new workers must include a start→stop→start cycle
and a stop-during-busy-slot case.
