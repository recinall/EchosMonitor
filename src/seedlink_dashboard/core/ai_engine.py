"""AI engagement + execution engine (M9).

:class:`AIEngine` is a *peer* of :class:`~seedlink_dashboard.core.
streaming_engine.StreamingEngine`, owned by the main window and living on
the GUI thread. It manages the on-demand engagement of an :class:`~
seedlink_dashboard.ai.base.AIAgent` over a chosen device/channel group and
runs inference **off** the GUI thread *and* **off** the data-path threads,
on a dedicated ``_ai_thread`` (a ``QObject`` worker moved to a ``QThread``,
mirroring :class:`~seedlink_dashboard.core.dsp_router._DspRouter`).

Best-effort consumer (CLAUDE.md rule 11). The engine *pulls* the most
recent window from the ring buffer via :meth:`StreamingEngine.read_recent`
(a cheap, lock-protected read) on its own ``QTimer``; it never sits on,
and so can never back-pressure, acquisition / DSP / detection / storage.
If inference can't keep up, the bounded in-flight slot is full and the
tick **drops** the window (logged once per 5 s, ``agentBackpressure``
emitted) — it never queues unboundedly and never blocks the data path.

Persistence ordering (rule 8). An annotation is mapped to a
:class:`~seedlink_dashboard.core.models.Detection` and persisted via
:meth:`StreamingEngine.record_ai_detection` (which commits) BEFORE the
``aiAnnotation`` signal is emitted — durable before announced. The DAO is
touched only on the GUI thread (the worker→engine ``annotated`` signal is
a ``QueuedConnection``, so ``_on_annotated`` runs on the GUI thread).

Wait observability (rule 7). Model load, resample and inference each log
start / end / elapsed on the worker thread; the worker logs when a window
takes longer than the configured step interval (the agent is falling
behind). Disengage sets a cooperative stop flag and joins the thread with
a bounded ``wait`` so a multi-second model load neither hangs the GUI nor
is silently ignored.

This module is torch-free — concrete agents import the heavy libraries
lazily.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import platformdirs
import structlog
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from seedlink_dashboard.ai.base import AIAnnotation, FitContext, FitResult, InferContext
from seedlink_dashboard.ai.state_store import StateStore
from seedlink_dashboard.core.models import Detection

if TYPE_CHECKING:
    from seedlink_dashboard.ai.base import AIAgent
    from seedlink_dashboard.config.schema import AiConfig, PersistOnDetectionConfig
    from seedlink_dashboard.core.streaming_engine import StreamingEngine
    from seedlink_dashboard.storage.archive_reader import ArchiveReader

_log = structlog.get_logger(__name__)

# How long disengage waits for the worker thread to finish the in-flight
# inference + release the model before giving up the join. Generous: a CPU
# inference window can take a few hundred ms and model release frees
# hundreds of MB. The wait is bounded (rule 7) — never an unbounded join.
_THREAD_JOIN_MS = 8000

# Throttle the drop log to one line per this many seconds (rule 5) — a
# sustained overload otherwise floods the structured-log channel.
_DROP_LOG_INTERVAL_S = 5.0

# Component windows pulled per tick may differ by a sample at the tail;
# require each present component to be at least this fraction of the
# requested window before we bother running inference.
_MIN_WINDOW_FILL = 0.5

# M10 Stage D — persist-on-detection. Bound on the number of in-flight
# post-roll capture timers (one per LIVE detection awaiting its post-roll).
# A sustained burst of high-score detections must not accumulate timers
# unboundedly (rule 11 — same drop-under-overload discipline as inference):
# beyond this cap the capture is dropped with one log line.
_MAX_PENDING_CAPTURES = 32


class AgentState(StrEnum):
    """Lifecycle of an engagement, surfaced to the panel."""

    IDLE = "idle"
    LOADING = "loading"
    # M10 fit-then-infer: a learning agent learns its baseline before it can
    # run. Entered on engage only when ``agent.requires_fit`` and no
    # persisted state exists; an inference-only agent never enters it.
    FITTING = "fitting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass(slots=True)
class EngagementSummary:
    """Immutable-ish snapshot handed to the UI on engage / state change."""

    engagement_id: str
    agent_name: str
    kind: str
    device: str
    nslc_by_component: dict[str, str]
    live: bool
    state: AgentState = AgentState.IDLE
    windows_done: int = 0
    dropped: int = 0
    last_infer_ms: float = 0.0
    last_error: str = ""


@dataclass(slots=True)
class _InferResult:
    """Worker → engine payload (type-erased through the Qt signal)."""

    engagement_id: str
    annotations: list[AIAnnotation]
    elapsed_ms: float


@dataclass(slots=True)
class _FitResult:
    """Worker → engine fit payload (type-erased through the Qt signal).

    Carries the human-facing :class:`FitResult` AND the agent's serialised
    state bytes together: the worker holds the learned params and calls
    ``serialize_state`` on its own thread right after a successful ``fit``,
    so no extra round-trip is needed. The engine then performs the file
    persistence (``StateStore.save``) on the GUI thread (rule-8 ordering:
    storage-ish writes happen GUI-side, durable before the agent runs).
    """

    engagement_id: str
    result: FitResult
    state: bytes | None
    elapsed_ms: float


@dataclass(slots=True)
class _Engagement:
    """Engine-side bookkeeping for the single active engagement."""

    engagement_id: str
    agent: AIAgent
    device: str
    group: dict[str, str]  # component letter -> nslc
    window_seconds: float
    step_seconds: float
    threshold_p: float
    threshold_s: float
    # M10 fit-then-infer: baseline window length for a learning agent's fit.
    baseline_seconds: float
    live: bool
    state: AgentState = AgentState.IDLE
    pending: int = 0
    windows_done: int = 0
    dropped: int = 0
    last_infer_ms: float = 0.0
    last_error: str = ""
    last_drop_log: float = 0.0
    # Archive replay (Stage C): a list of pre-built windows fed one at a
    # time so replay self-throttles (next fed on each result).
    archive_windows: list[InferContext] = field(default_factory=list)
    archive_cursor: int = 0
    # M10 Stage D — persist-on-detection policy for this engagement. The
    # SOLE bridge from an agent annotation to a storage side-effect: when
    # ``enabled`` and a detection clears ``min_score`` the engine emits
    # ``persistRequested`` (the agent is never involved). ``None`` disables.
    persist_cfg: PersistOnDetectionConfig | None = None


class _AiWorker(QObject):
    """Lives on ``_ai_thread``. Owns the (possibly torch-bearing) agent.

    Mirrors :class:`_DspRouter`: parentless ``QObject``, slots invoked via
    ``QueuedConnection``, never raises across the thread boundary (a failed
    inference becomes a ``failed`` signal, not a crashed thread).
    """

    ready = Signal(str)  # engagement_id
    annotated = Signal(object)  # _InferResult
    fitted = Signal(object)  # _FitResult (M10 fit-then-infer)
    failed = Signal(str, str, str)  # engagement_id, phase, message

    def __init__(self) -> None:
        super().__init__()
        self._agent: AIAgent | None = None
        self._stop = False

    @Slot(object)
    def install_agent(self, agent: object) -> None:
        from seedlink_dashboard.ai.base import AIAgent

        if not isinstance(agent, AIAgent):  # defensive — type-erased through Signal
            _log.warning("ai_worker_invalid_agent", type=type(agent).__name__)
            return
        self._agent = agent
        self._stop = False

    @Slot(str, object)
    def load_state(self, engagement_id: str, data: object) -> None:
        """Restore persisted learned state into the agent (M10).

        Runs on ``_ai_thread`` (QueuedConnection). A failure becomes a
        ``failed`` signal with phase ``"fit"`` (the fit phase failed to
        resume from disk) — never a crashed thread.
        """
        agent = self._agent
        if agent is None or not isinstance(data, bytes):
            self.failed.emit(engagement_id, "fit", "no agent / bad state bytes")
            return
        try:
            agent.load_state(data)
        except Exception as exc:
            _log.error("ai_load_state_failed", engagement=engagement_id, error=str(exc))
            self.failed.emit(engagement_id, "fit", str(exc))
            return
        _log.info("ai_fit_state_loaded", engagement=engagement_id, bytes=len(data))

    @Slot(str, object)
    def fit(self, engagement_id: str, ctx: object) -> None:
        """Learn the baseline (M10 fit-then-infer). Rule-7 observable.

        Runs off the GUI thread AND off the data-path threads (it executes
        on ``_ai_thread``, like ``warm_up``/``infer``). Interruptible: the
        :class:`FitContext` handed in carries a ``should_stop`` that reads
        this worker's cooperative ``self._stop`` flag, so a disengage during
        fit (which sets the flag via ``request_stop``) makes the agent's fit
        loop return within one polling period — a bounded, observable wait.
        After a successful fit the worker serialises the learned state on
        this same thread (it holds the params) and hands both the
        :class:`FitResult` and the bytes back so the engine persists them
        GUI-side. A failed fit becomes a ``failed`` signal (phase ``"fit"``),
        never a crashed thread.
        """
        agent = self._agent
        if agent is None or not isinstance(ctx, FitContext):
            self.failed.emit(engagement_id, "fit", "no agent / bad context")
            return
        t0 = time.monotonic()
        _log.info("ai_fit_start", engagement=engagement_id, agent=agent.name)
        try:
            result = agent.fit(ctx)
            state = agent.serialize_state()
        except Exception as exc:
            _log.error("ai_fit_failed", engagement=engagement_id, error=str(exc))
            self.failed.emit(engagement_id, "fit", str(exc))
            return
        elapsed = (time.monotonic() - t0) * 1000.0
        _log.info(
            "ai_fit_done",
            engagement=engagement_id,
            agent=agent.name,
            elapsed_ms=round(elapsed, 1),
            n_windows=result.n_windows,
            has_state=state is not None,
        )
        if self._stop:
            # Disengaged mid-fit — do not announce completion.
            _log.info("ai_fit_aborted_after_fit", engagement=engagement_id)
            return
        self.fitted.emit(_FitResult(engagement_id, result, state, elapsed))

    @Slot(str, float)
    def warm_up(self, engagement_id: str, step_seconds: float) -> None:
        """Load the model. Heavy; rule-7 observable; honours the stop flag."""
        agent = self._agent
        if agent is None:
            self.failed.emit(engagement_id, "load", "no agent installed")
            return
        t0 = time.monotonic()
        _log.info("ai_warm_up_start", engagement=engagement_id, agent=agent.name)
        try:
            agent.warm_up()
        except Exception as exc:
            _log.error("ai_warm_up_failed", engagement=engagement_id, error=str(exc))
            self.failed.emit(engagement_id, "load", str(exc))
            return
        elapsed = (time.monotonic() - t0) * 1000.0
        _log.info(
            "ai_warm_up_done",
            engagement=engagement_id,
            agent=agent.name,
            elapsed_ms=round(elapsed, 1),
        )
        if self._stop:
            # Disengaged mid-load — do not start running.
            _log.info("ai_warm_up_aborted_after_load", engagement=engagement_id)
            return
        self.ready.emit(engagement_id)

    @Slot(str, float, object)
    def infer(self, engagement_id: str, step_seconds: float, ctx: object) -> None:
        """Run one inference window. Rule-7 observable; never raises out."""
        agent = self._agent
        if agent is None or not isinstance(ctx, InferContext):
            self.failed.emit(engagement_id, "infer", "no agent / bad context")
            return
        t0 = time.monotonic()
        _log.debug("ai_infer_start", engagement=engagement_id, t_start=str(ctx.t_start))
        try:
            annotations = agent.infer(ctx)
        except Exception as exc:
            _log.error("ai_infer_failed", engagement=engagement_id, error=str(exc))
            self.failed.emit(engagement_id, "infer", str(exc))
            return
        elapsed = (time.monotonic() - t0) * 1000.0
        # Rule 7 / rule 11: if a window takes longer than its step interval
        # the agent is falling behind — say so in the structured log.
        if step_seconds > 0 and elapsed > step_seconds * 1000.0:
            _log.warning(
                "ai_infer_behind",
                engagement=engagement_id,
                elapsed_ms=round(elapsed, 1),
                step_ms=round(step_seconds * 1000.0, 1),
                n_annotations=len(annotations),
            )
        else:
            _log.debug(
                "ai_infer_done",
                engagement=engagement_id,
                elapsed_ms=round(elapsed, 1),
                n_annotations=len(annotations),
            )
        self.annotated.emit(_InferResult(engagement_id, annotations, elapsed))

    @Slot()
    def request_stop(self) -> None:
        """Cooperative stop flag, checked after the model load completes."""
        self._stop = True

    @Slot()
    def release(self) -> None:
        agent = self._agent
        if agent is not None:
            try:
                agent.release()
            except Exception as exc:
                _log.warning("ai_release_failed", error=str(exc))
        self._agent = None


class AIEngine(QObject):
    """Owns one active engagement and its dedicated inference thread.

    Single active engagement at a time (one warm model on one worker
    thread): engaging while one is active disengages the previous first.
    The engagement-id flows through every signal so the panel and any
    future multi-agent extension can disambiguate; stale results from a
    disengaged agent are dropped.
    """

    agentEngaged = Signal(str, object)  # engagement_id, EngagementSummary  # noqa: N815
    agentStateChanged = Signal(str, str)  # engagement_id, AgentState.value  # noqa: N815
    aiAnnotation = Signal(object)  # Detection (post-persist)  # noqa: N815
    agentBackpressure = Signal(str, int)  # engagement_id, dropped  # noqa: N815
    # M10 Stage D — persist-on-detection. Emitted by the engagement POLICY
    # (not the agent) when a durable Detection clears ``min_score``: carries
    # an ``EventPersistRequest`` that the storage-thread ``EventPersister``
    # turns into a curated event. Wired engine-side via QueuedConnection so
    # the write runs on the storage thread (rule 8). The agent has zero
    # knowledge of this signal — it only ever returned an ``AIAnnotation``.
    persistRequested = Signal(object)  # EventPersistRequest  # noqa: N815

    # Engine → worker drive signals. Connected to the worker's slots with
    # QueuedConnection so the slot body runs on ``_ai_thread`` — the same
    # cross-thread dispatch pattern the engine uses for ``_DspRouter``
    # (a signal+QueuedConnection, never a direct slot call). Leading
    # underscore marks them private; ``noqa: N815`` for the Qt mixedCase.
    _installRequested = Signal(object)  # agent  # noqa: N815
    _warmUpRequested = Signal(str, float)  # engagement_id, step_seconds  # noqa: N815
    _inferRequested = Signal(str, float, object)  # eng_id, step, ctx  # noqa: N815
    _fitRequested = Signal(str, object)  # engagement_id, FitContext  # noqa: N815
    _loadStateRequested = Signal(str, object)  # engagement_id, bytes  # noqa: N815
    _stopRequested = Signal()  # noqa: N815
    _releaseRequested = Signal()  # noqa: N815

    def __init__(
        self,
        engine: StreamingEngine,
        cfg: AiConfig,
        parent: QObject | None = None,
        *,
        data_dir: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._cfg = cfg
        self._engagement: _Engagement | None = None
        self._seq = 0  # monotonic engagement-id counter
        # M10 fit-then-infer learned-state persistence. ``data_dir`` is
        # resolved the SAME way the storage layer resolves its roots (see
        # ``StreamingEngine._resolve_db_root``): the caller's ``data_dir``
        # if given, else ``app.archive_root`` parent, else the platformdirs
        # default. Learned state lands under ``<data_dir>/models/`` — NOT in
        # the SDS archive (it is derived, not science data).
        self._state_store = StateStore(self._resolve_data_dir(data_dir))

        self._worker = _AiWorker()
        self._ai_thread = QThread()
        self._ai_thread.setObjectName("ai-worker")
        self._worker.moveToThread(self._ai_thread)

        # Worker → engine: QueuedConnection so these slots run on the GUI
        # thread (where the DAO is written). Rule 8 ordering depends on it.
        self._worker.ready.connect(self._on_ready, Qt.ConnectionType.QueuedConnection)
        self._worker.annotated.connect(self._on_annotated, Qt.ConnectionType.QueuedConnection)
        self._worker.fitted.connect(self._on_fitted, Qt.ConnectionType.QueuedConnection)
        self._worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)

        # Engine → worker: QueuedConnection so the slot body runs on
        # ``_ai_thread`` (never the GUI thread, never the data-path thread).
        self._installRequested.connect(
            self._worker.install_agent, Qt.ConnectionType.QueuedConnection
        )
        self._warmUpRequested.connect(self._worker.warm_up, Qt.ConnectionType.QueuedConnection)
        self._inferRequested.connect(self._worker.infer, Qt.ConnectionType.QueuedConnection)
        self._fitRequested.connect(self._worker.fit, Qt.ConnectionType.QueuedConnection)
        self._loadStateRequested.connect(
            self._worker.load_state, Qt.ConnectionType.QueuedConnection
        )
        self._stopRequested.connect(self._worker.request_stop, Qt.ConnectionType.QueuedConnection)
        self._releaseRequested.connect(self._worker.release, Qt.ConnectionType.QueuedConnection)

        # GUI-thread pull timer (live mode). Interval set per engagement.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        # M10 Stage D — in-flight post-roll capture timers (live detections).
        # Each entry is a single-shot QTimer that fires after ``post_seconds``
        # to grab the [t_on-pre, t_off+post] window from the ring and emit
        # the persist request. Tracked so they are cancelled on
        # disengage()/shutdown() (rule 7 — no leaked timers, bounded waits).
        self._pending_captures: set[QTimer] = set()
        # M10 Stage D — the storage-thread persister ``persistRequested`` is
        # currently connected to (lazily, on the first persist-enabled engage).
        # Connecting it is what makes the file write run on the storage thread;
        # without it the policy would emit into the void. Keyed on the persister
        # IDENTITY (not a sticky bool): if the engine is stopped — which drops
        # its persister — and later re-engaged, ``attach_event_persister``
        # returns a NEW object, so the guard reconnects; a normal re-engage
        # returns the SAME object, so it does not double-connect.
        self._wired_persister: object | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def engage(
        self,
        agent: AIAgent,
        device: str,
        group: dict[str, str],
        *,
        threshold_p: float | None = None,
        threshold_s: float | None = None,
        window_seconds: float | None = None,
        step_seconds: float | None = None,
        persist_cfg: PersistOnDetectionConfig | None = None,
    ) -> str:
        """Engage ``agent`` live on ``device``'s ``group`` (component→nslc).

        Returns the new engagement id. Disengages any prior engagement
        first. The model loads off-thread; the engagement starts in
        ``LOADING`` and transitions to ``RUNNING`` when ``warm_up`` is done.

        ``persist_cfg`` (M10 Stage D) carries the persist-on-detection
        policy; defaults to ``cfg.persist_on_detection`` when not given.
        """
        return self._engage_common(
            agent,
            device,
            group,
            live=True,
            archive_windows=None,
            threshold_p=threshold_p,
            threshold_s=threshold_s,
            window_seconds=window_seconds,
            step_seconds=step_seconds,
            persist_cfg=persist_cfg,
        )

    def engage_archive(
        self,
        agent: AIAgent,
        device: str,
        group: dict[str, str],
        windows: list[InferContext],
        *,
        threshold_p: float | None = None,
        threshold_s: float | None = None,
        persist_cfg: PersistOnDetectionConfig | None = None,
    ) -> str:
        """Engage ``agent`` over a fixed list of historical ``windows``.

        Same worker and persist-then-emit path as live; windows are fed
        one at a time (next on each result) so replay self-throttles and
        never floods the worker queue. Annotations persist with
        ``meta["source"]="archive"`` to distinguish them from live picks.

        ``persist_cfg`` (M10 Stage D) carries the persist-on-detection
        policy; archive detections persist immediately with ``samples=None``
        (the persister reads the window from SDS) — no post-roll timer.
        """
        return self._engage_common(
            agent,
            device,
            group,
            live=False,
            archive_windows=windows,
            threshold_p=threshold_p,
            threshold_s=threshold_s,
            window_seconds=None,
            step_seconds=None,
            persist_cfg=persist_cfg,
        )

    def disengage(self, engagement_id: str | None = None) -> None:
        """Stop the active engagement (or the one named) and free the model.

        Idempotent. Stops the pull timer, sets a cooperative stop flag,
        joins the worker thread with a bounded wait (rule 7), releases the
        model and returns to IDLE.
        """
        eng = self._engagement
        if eng is None:
            return
        if engagement_id is not None and engagement_id != eng.engagement_id:
            return
        self._timer.stop()
        # Cancel any in-flight post-roll persist captures (rule 7 — no
        # leaked timers; a queued one-shot would otherwise fire after the
        # engagement is gone, build a request against a dead engagement).
        self._cancel_pending_captures()
        self._set_state(eng, AgentState.STOPPING)
        # Cross the thread boundary SYNCHRONOUSLY for the stop flag. The
        # worker may be busy inside a long ``fit`` (or ``warm_up``), so a
        # QueuedConnection ``request_stop`` cannot run until that returns —
        # it could never preempt the in-flight wait (rule 7). A direct write
        # of the bool ``_stop`` is GIL-atomic and is observed by the agent's
        # ``should_stop`` poll within one polling period, so a disengage
        # mid-fit actually interrupts the fit loop. The queued
        # ``_stopRequested`` below stays as a belt-and-suspenders for the
        # idle-worker case and keeps the public stop slot wired.
        self._worker._stop = True
        self._stopRequested.emit()
        self._releaseRequested.emit()
        if self._ai_thread.isRunning():
            self._ai_thread.quit()
            if not self._ai_thread.wait(_THREAD_JOIN_MS):
                _log.warning("ai_thread_join_timeout", engagement=eng.engagement_id)
        self._set_state(eng, AgentState.IDLE)
        self._engagement = None

    def active_engagement(self) -> EngagementSummary | None:
        """Snapshot of the current engagement for the panel, or ``None``."""
        eng = self._engagement
        if eng is None:
            return None
        return self._summary(eng)

    def shutdown(self) -> None:
        """Tear down for app exit — disengage and stop the thread."""
        self.disengage()
        if self._ai_thread.isRunning():
            self._ai_thread.quit()
            self._ai_thread.wait(_THREAD_JOIN_MS)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _resolve_data_dir(self, data_dir: Path | None) -> Path:
        """Resolve the root under which learned state (``models/``) lives.

        Mirrors :meth:`StreamingEngine._resolve_db_root` EXACTLY so learned
        state lands beside the metadata DB / SDS root, not one level above
        it: the explicit ``data_dir`` override if given, else
        ``app.archive_root`` (the data root storage itself uses — the DB and
        SDS live under it), else ``user_data_dir(...)/archive`` (the same
        platformdirs fallback storage uses). :class:`StateStore` appends
        ``models/``, so learned state ends up a sibling of the DB and the
        SDS tree but OUTSIDE the SDS ``YEAR/NET/...`` waveform tree.
        """
        if data_dir is not None:
            return Path(data_dir)
        archive_root = self._engine._cfg.app.archive_root
        if archive_root is not None:
            return Path(archive_root)
        return Path(platformdirs.user_data_dir("seedlink_dashboard", "SeedTiLa")) / "archive"

    def _begin_fit_or_resume(self, eng: _Engagement) -> bool:
        """Resume persisted state, or start the fit phase (M10).

        Returns ``True`` if the engage should proceed to LOADING→warm_up
        (state was restored, or no baseline could be built and we fall back
        to running anyway), ``False`` if we entered FITTING and warm_up must
        wait for the ``fitted`` round-trip.

        * If persisted state exists: hand it to the worker (``load_state``)
          and proceed straight to warm_up→RUNNING — the fit is skipped.
        * Else: build a :class:`FitContext` from the most-recent baseline
          window and enter FITTING; warm_up runs after ``_on_fitted``.
        """
        kind = eng.agent.kind
        nslc_z = eng.group.get("Z") or next(iter(eng.group.values()))
        if self._state_store.has(kind, eng.device, nslc_z):
            data = self._state_store.load(kind, eng.device, nslc_z)
            if data is not None:
                _log.info(
                    "ai_fit_state_resume",
                    engagement=eng.engagement_id,
                    kind=kind,
                    device=eng.device,
                    nslc=nslc_z,
                )
                self._loadStateRequested.emit(eng.engagement_id, data)
                return True  # skip fit; proceed to warm_up→RUNNING
        ctx = self._pull_baseline(eng)
        if ctx is None:
            # No baseline available (e.g. archive engage or an empty ring).
            # Honest fallback: do not silently fit on nothing — proceed to
            # warm_up and let the agent run un-fitted (it may still infer on
            # the default state). A real archive-fit plumbing is out of scope
            # for this stage (TODO: archive baseline selection).
            _log.warning(
                "ai_fit_no_baseline",
                engagement=eng.engagement_id,
                live=eng.live,
            )
            return True
        self._set_state(eng, AgentState.FITTING)
        self._fitRequested.emit(eng.engagement_id, ctx)
        return False  # warm_up deferred until _on_fitted

    def _engage_common(
        self,
        agent: AIAgent,
        device: str,
        group: dict[str, str],
        *,
        live: bool,
        archive_windows: list[InferContext] | None,
        threshold_p: float | None,
        threshold_s: float | None,
        window_seconds: float | None,
        step_seconds: float | None,
        persist_cfg: PersistOnDetectionConfig | None = None,
    ) -> str:
        if self._engagement is not None:
            self.disengage()
        self._seq += 1
        engagement_id = f"eng-{self._seq}"
        eng = _Engagement(
            engagement_id=engagement_id,
            agent=agent,
            device=device,
            group=dict(group),
            window_seconds=(
                float(self._cfg.window_seconds) if window_seconds is None else float(window_seconds)
            ),
            step_seconds=(
                float(self._cfg.step_seconds) if step_seconds is None else float(step_seconds)
            ),
            threshold_p=self._cfg.threshold_p if threshold_p is None else float(threshold_p),
            threshold_s=self._cfg.threshold_s if threshold_s is None else float(threshold_s),
            baseline_seconds=float(self._cfg.baseline_seconds),
            live=live,
            archive_windows=list(archive_windows or []),
            persist_cfg=(
                persist_cfg if persist_cfg is not None else self._cfg.persist_on_detection
            ),
        )
        self._engagement = eng
        # M10 Stage D — if this engagement persists on detection, make sure
        # ``persistRequested`` is wired to the storage-thread persister so
        # the policy's requests actually reach storage (the seam between the
        # engine policy and the storage write). Idempotent + lazy: only the
        # first persist-enabled engage stands the persister up.
        self._ensure_persist_wiring(eng)
        self.agentEngaged.emit(engagement_id, self._summary(eng))

        if not self._ai_thread.isRunning():
            self._ai_thread.start()
        self._installRequested.emit(agent)
        # M10 fit-then-infer: a learning agent may need to learn (or restore)
        # a baseline before it can run. An inference-only agent
        # (``requires_fit`` False — the picker / detector) skips this branch
        # entirely: same LOADING→RUNNING path as before, state store untouched.
        if agent.requires_fit and not self._begin_fit_or_resume(eng):
            return engagement_id
        self._set_state(eng, AgentState.LOADING)
        self._warmUpRequested.emit(engagement_id, eng.step_seconds)
        return engagement_id

    @Slot(str)
    def _on_ready(self, engagement_id: str) -> None:
        eng = self._engagement
        if eng is None or eng.engagement_id != engagement_id:
            return  # stale
        self._set_state(eng, AgentState.RUNNING)
        if eng.live:
            self._timer.setInterval(max(1, int(eng.step_seconds * 1000)))
            self._timer.start()
        else:
            self._feed_next_archive_window(eng)

    @Slot(object)
    def _on_annotated(self, result: object) -> None:
        if not isinstance(result, _InferResult):
            return
        eng = self._engagement
        if eng is None or eng.engagement_id != result.engagement_id:
            return  # stale result from a disengaged agent
        eng.pending = max(0, eng.pending - 1)
        eng.windows_done += 1
        eng.last_infer_ms = result.elapsed_ms
        for ann in result.annotations:
            self._persist_and_emit(ann, eng)
        if not eng.live:
            # Feed the next historical window (self-throttling replay).
            if eng.archive_cursor >= len(eng.archive_windows):
                _log.info(
                    "ai_archive_replay_done",
                    engagement=eng.engagement_id,
                    windows=eng.windows_done,
                )
                self._set_state(eng, AgentState.IDLE)
            else:
                self._feed_next_archive_window(eng)

    @Slot(object)
    def _on_fitted(self, payload: object) -> None:
        """Fit done (M10): persist learned state GUI-side, then warm_up.

        Runs on the GUI thread (QueuedConnection). Persists the agent's
        serialised state via the :class:`StateStore` (a file write done
        GUI-side for rule-8-style ordering consistency: durable BEFORE the
        agent starts running), then transitions LOADING→warm_up→RUNNING. The
        state was already restored onto the worker's live agent during fit,
        so warm_up uses the fitted agent directly.
        """
        if not isinstance(payload, _FitResult):
            return
        eng = self._engagement
        if eng is None or eng.engagement_id != payload.engagement_id:
            return  # stale fit result from a disengaged agent
        if payload.state is not None:
            nslc_z = eng.group.get("Z") or next(iter(eng.group.values()))
            self._state_store.save(eng.agent.kind, eng.device, nslc_z, payload.state)
        else:
            _log.warning(
                "ai_fit_no_state_to_persist",
                engagement=eng.engagement_id,
                summary=payload.result.summary,
            )
        # Proceed to the standard load→run path with the now-fitted agent.
        self._set_state(eng, AgentState.LOADING)
        self._warmUpRequested.emit(eng.engagement_id, eng.step_seconds)

    @Slot(str, str, str)
    def _on_failed(self, engagement_id: str, phase: str, message: str) -> None:
        eng = self._engagement
        if eng is None or eng.engagement_id != engagement_id:
            return
        if phase == "infer":
            eng.pending = max(0, eng.pending - 1)
            # A single bad window must not kill the engagement; keep running.
            if not eng.live:
                if eng.archive_cursor < len(eng.archive_windows):
                    self._feed_next_archive_window(eng)
                else:
                    self._set_state(eng, AgentState.IDLE)
            return
        # Load failure (e.g. extra not installed) — terminal for this engage.
        self._timer.stop()
        eng.last_error = message
        self._set_state(eng, AgentState.ERROR)

    @Slot()
    def _tick(self) -> None:
        """Live pull tick — best-effort, never blocks the data path (rule 11)."""
        eng = self._engagement
        if eng is None or not eng.live or eng.state is not AgentState.RUNNING:
            return
        if eng.pending >= 1:
            # Bounded in-flight slot is full → drop this window (rule 5/11).
            eng.dropped += 1
            now = time.monotonic()
            if now - eng.last_drop_log >= _DROP_LOG_INTERVAL_S:
                _log.warning(
                    "ai_window_dropped",
                    engagement=eng.engagement_id,
                    dropped_total=eng.dropped,
                )
                eng.last_drop_log = now
            self.agentBackpressure.emit(eng.engagement_id, eng.dropped)
            return
        ctx = self._pull_window(eng)
        if ctx is None:
            return
        eng.pending += 1
        self._inferRequested.emit(eng.engagement_id, eng.step_seconds, ctx)

    def _pull_window(self, eng: _Engagement) -> InferContext | None:
        """Snapshot the most-recent window for every component (GUI thread).

        Cheap, lock-protected reads (``read_recent``). Returns ``None`` when
        no component yet holds enough samples — the agent simply waits.
        """
        samples: dict[str, np.ndarray] = {}
        fs_ref = 0.0
        latest_ref: UTCDateTime | None = None
        for comp, nslc in eng.group.items():
            arr, fs, latest = self._engine.read_recent(eng.device, nslc, eng.window_seconds)
            if arr.size == 0 or fs <= 0 or latest is None:
                continue
            samples[comp] = arr
            fs_ref = fs
            latest_ref = latest
        if not samples or fs_ref <= 0 or latest_ref is None:
            return None
        # Align components to the shortest length (they share station timing).
        min_len = min(int(a.shape[0]) for a in samples.values())
        need = int(eng.window_seconds * fs_ref * _MIN_WINDOW_FILL)
        if min_len < max(1, need):
            return None
        aligned = {c: a[-min_len:].astype(np.float32, copy=False) for c, a in samples.items()}
        t_start = latest_ref - (min_len - 1) / fs_ref
        nslc_z = eng.group.get("Z") or next(iter(eng.group.values()))
        station_key = ".".join(nslc_z.split(".")[:2])
        return InferContext(
            device=eng.device,
            station_key=station_key,
            nslc_by_component=dict(eng.group),
            samples_by_component=aligned,
            fs=fs_ref,
            t_start=t_start,
            window_seconds=eng.window_seconds,
            live=True,
            threshold_p=eng.threshold_p,
            threshold_s=eng.threshold_s,
        )

    def _pull_baseline(self, eng: _Engagement) -> FitContext | None:
        """Snapshot a ``baseline_seconds`` window for the fit phase (GUI thread).

        Mirrors :meth:`_pull_window` but pulls the longer baseline window a
        learning agent fits on. Live only: the UI asks the user to engage
        during a quiet period, and we pull the most-recent ``baseline_seconds``
        (kept simple and documented — no separate quiet-period picker in this
        stage). Returns ``None`` when no component holds a usable baseline yet
        (the caller then falls back to running un-fitted).

        ``should_stop`` reads the worker's cooperative stop flag, so a
        disengage mid-fit (which sets the flag) makes the agent's fit loop
        return within one polling period (rule 7 interruptibility). ``progress``
        logs each reported step so the structured-log channel stays alive
        during the fit wait (rule 7 observability).
        """
        if not eng.live:
            return None
        samples: dict[str, np.ndarray] = {}
        fs_ref = 0.0
        latest_ref: UTCDateTime | None = None
        for comp, nslc in eng.group.items():
            arr, fs, latest = self._engine.read_recent(eng.device, nslc, eng.baseline_seconds)
            if arr.size == 0 or fs <= 0 or latest is None:
                continue
            samples[comp] = arr
            fs_ref = fs
            latest_ref = latest
        if not samples or fs_ref <= 0 or latest_ref is None:
            return None
        min_len = min(int(a.shape[0]) for a in samples.values())
        # Require the baseline to be at least mostly full before fitting on
        # it: a near-empty ring would otherwise persist a degenerate "fit"
        # (e.g. a handful of samples) that the resume path would then reuse
        # forever. An under-filled ring falls back to the un-fitted path
        # (caller logs ``ai_fit_no_baseline``).
        need = int(eng.baseline_seconds * fs_ref * _MIN_WINDOW_FILL)
        if min_len < max(1, need):
            return None
        aligned = {c: a[-min_len:].astype(np.float32, copy=False) for c, a in samples.items()}
        t_start = latest_ref - (min_len - 1) / fs_ref
        nslc_z = eng.group.get("Z") or next(iter(eng.group.values()))
        station_key = ".".join(nslc_z.split(".")[:2])
        engagement_id = eng.engagement_id

        def _should_stop() -> bool:
            return bool(self._worker._stop)

        def _progress(fraction: float, message: str) -> None:
            _log.info(
                "ai_fit_progress",
                engagement=engagement_id,
                fraction=round(float(fraction), 3),
                message=message,
            )

        return FitContext(
            device=eng.device,
            station_key=station_key,
            nslc_by_component=dict(eng.group),
            samples_by_component=aligned,
            fs=fs_ref,
            t_start=t_start,
            baseline_seconds=eng.baseline_seconds,
            should_stop=_should_stop,
            progress=_progress,
        )

    def _feed_next_archive_window(self, eng: _Engagement) -> None:
        if eng.archive_cursor >= len(eng.archive_windows):
            return
        ctx = eng.archive_windows[eng.archive_cursor]
        eng.archive_cursor += 1
        eng.pending += 1
        # step=0 → no "behind" warning in replay (no real-time deadline).
        self._inferRequested.emit(eng.engagement_id, 0.0, ctx)

    def _persist_and_emit(self, ann: AIAnnotation, eng: _Engagement) -> None:
        """Map annotation → Detection, persist (commit), THEN emit (rule 8).

        After the Detection is durable, evaluate the persist-on-detection
        engagement POLICY (M10 Stage D): this is the SOLE bridge from an
        agent annotation to a storage side-effect. The agent never knows
        any of this happened — it only returned an :class:`AIAnnotation`.
        """
        live = eng.live
        meta: dict[str, object] = dict(ann.meta)
        meta.setdefault("phase", ann.phase)
        meta.setdefault("agent", ann.model_name)
        meta.setdefault("weights", ann.model_weights)
        meta["window_t_start"] = str(ann.window_t_start)
        meta["window_t_end"] = str(ann.window_t_end)
        if not live:
            meta["source"] = "archive"
        detection = Detection(
            device=ann.device,
            nslc=ann.nslc,
            kind=ann.kind,
            t_on=ann.t,
            # None for an instantaneous pick (P/S onset → open/onset row);
            # a real end time for a span-style detection (eqt_detection →
            # closed segment row, like STA/LTA). The picker leaves t_end
            # None, so its behaviour is unchanged.
            t_off=ann.t_end,
            score=float(ann.score),
            detected_at=UTCDateTime(),
            meta=meta,
        )
        det_id = self._engine.record_ai_detection(ann.device, ann.nslc, detection)
        if det_id is None:
            _log.warning(
                "ai_annotation_not_persisted",
                device=ann.device,
                nslc=ann.nslc,
                kind=ann.kind,
            )
            return
        # Durable now — safe to announce (rule 8).
        self.aiAnnotation.emit(detection)
        # Engagement policy: persist-on-detection (rule 8 bridge). The agent
        # is NOT involved; this is entirely the engine-side policy.
        self._maybe_persist_event(detection, eng)

    # ------------------------------------------------------------------
    # M10 Stage D — persist-on-detection engagement policy
    # ------------------------------------------------------------------
    def _ensure_persist_wiring(self, eng: _Engagement) -> None:
        """Connect ``persistRequested`` → the storage-thread persister once.

        The seam between the engine-side policy (which emits
        ``persistRequested`` on the GUI thread) and the storage-side
        :class:`~seedlink_dashboard.storage.event_persister.EventPersister`
        (whose ``persist`` slot runs on the storage thread). Lazy + idempotent:
        only a persist-enabled engagement stands the persister up (via
        ``StreamingEngine.attach_event_persister``), and the cross-thread
        ``QueuedConnection`` is made exactly once. Without this the policy
        would emit into the void — the agent → policy → storage chain would
        stop at the signal. Storage still owns the writer (rule 8); the engine
        only wires its own signal to it.
        """
        cfg = eng.persist_cfg
        if cfg is None or not cfg.enabled:
            return
        persister = self._engine.attach_event_persister()
        if persister is None or persister is self._wired_persister:
            return
        self.persistRequested.connect(persister.persist, Qt.ConnectionType.QueuedConnection)
        self._wired_persister = persister

    def _maybe_persist_event(self, detection: Detection, eng: _Engagement) -> None:
        """Evaluate the persist-on-detection policy for one durable detection.

        The bridge agent → storage. If the policy is enabled and the
        detection clears ``min_score``, build an ``EventPersistRequest`` and
        emit ``persistRequested`` (handled on the storage thread). The agent
        never reaches storage — this policy is the only path.

        Live detections defer to capture the post-roll: a one-shot QTimer
        fires after ``post_seconds`` then grabs ``[t_on-pre, t_off+post]``
        from the ring (cheap GUI-thread ``read_recent`` — the same read the
        inference tick uses, never the data path) and emits the request.
        Archive detections (``live=False``) emit immediately with
        ``samples=None`` so the persister reads the window from SDS.
        """
        cfg = eng.persist_cfg
        if cfg is None or not cfg.enabled:
            return
        if float(detection.score) < float(cfg.min_score):
            return

        pre = float(cfg.pre_seconds)
        post = float(cfg.post_seconds)
        t_on = detection.t_on
        t_ref_end = detection.t_off if detection.t_off is not None else detection.t_on
        t_start = t_on - pre
        t_end = t_ref_end + post

        if not eng.live:
            # Historical window — emit immediately; persister reads from SDS.
            self._emit_persist_request(
                detection, eng, cfg, t_start, t_end, samples=None, fs=0.0, samples_t0=None
            )
            return

        # Live: bound in-flight captures (rule 11 drop-under-overload).
        if len(self._pending_captures) >= _MAX_PENDING_CAPTURES:
            _log.warning(
                "ai_persist_capture_dropped",
                engagement=eng.engagement_id,
                device=detection.device,
                nslc=detection.nslc,
                pending=len(self._pending_captures),
            )
            return

        device = detection.device
        nslc = detection.nslc
        span = float(t_end - t_start)
        engagement_id = eng.engagement_id

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(max(1, int(post * 1000)))

        def _on_fire() -> None:
            self._pending_captures.discard(timer)
            timer.deleteLater()
            cur = self._engagement
            if cur is None or cur.engagement_id != engagement_id:
                return  # disengaged while waiting — drop (timer already gone)
            arr, fs, latest = self._engine.read_recent(device, nslc, span)
            if arr.size == 0 or fs <= 0 or latest is None:
                # Ring scrolled out / never filled — fall back to SDS read.
                _log.info(
                    "ai_persist_capture_empty_ring",
                    engagement=engagement_id,
                    device=device,
                    nslc=nslc,
                )
                self._emit_persist_request(
                    detection, cur, cfg, t_start, t_end, samples=None, fs=0.0, samples_t0=None
                )
                return
            cap_t0 = latest - (int(arr.shape[0]) - 1) / fs
            if cap_t0 > t_start + 1.0 / max(fs, 1.0):
                # Pre-roll start scrolled out of the ring: capture what we
                # have but do NOT silently claim the full window (rule 7).
                _log.warning(
                    "ai_persist_capture_truncated",
                    engagement=engagement_id,
                    device=device,
                    nslc=nslc,
                    requested_t_start=str(t_start),
                    captured_t_start=str(cap_t0),
                )
            self._emit_persist_request(
                detection, cur, cfg, t_start, t_end, samples=arr, fs=fs, samples_t0=cap_t0
            )

        timer.timeout.connect(_on_fire)
        self._pending_captures.add(timer)
        timer.start()

    def _emit_persist_request(
        self,
        detection: Detection,
        eng: _Engagement,
        cfg: PersistOnDetectionConfig,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        *,
        samples: np.ndarray | None,
        fs: float,
        samples_t0: UTCDateTime | None,
    ) -> None:
        from seedlink_dashboard.storage.event_persister import EventPersistRequest

        request = EventPersistRequest(
            device=detection.device,
            nslc=detection.nslc,
            detection_id=detection.id,
            mode=cfg.mode,
            t_start=t_start,
            t_end=t_end,
            score=float(detection.score),
            pre_seconds=float(cfg.pre_seconds),
            post_seconds=float(cfg.post_seconds),
            samples=samples,
            fs=fs,
            samples_t_start=samples_t0,
            meta={
                "engagement_id": eng.engagement_id,
                "agent": eng.agent.name,
                "kind": detection.kind,
                "phase": detection.meta.get("phase"),
                "source": "archive" if not eng.live else "live",
            },
        )
        self.persistRequested.emit(request)

    def _cancel_pending_captures(self) -> None:
        """Stop + drop all in-flight post-roll capture timers (rule 7)."""
        for timer in list(self._pending_captures):
            timer.stop()
            timer.deleteLater()
        self._pending_captures.clear()

    def _set_state(self, eng: _Engagement, state: AgentState) -> None:
        eng.state = state
        self.agentStateChanged.emit(eng.engagement_id, state.value)

    def _summary(self, eng: _Engagement) -> EngagementSummary:
        return EngagementSummary(
            engagement_id=eng.engagement_id,
            agent_name=eng.agent.name,
            kind=eng.agent.kind,
            device=eng.device,
            nslc_by_component=dict(eng.group),
            live=eng.live,
            state=eng.state,
            windows_done=eng.windows_done,
            dropped=eng.dropped,
            last_infer_ms=eng.last_infer_ms,
            last_error=eng.last_error,
        )


def build_archive_windows(
    reader: ArchiveReader,
    device: str,
    group: dict[str, str],
    t_start: UTCDateTime,
    t_end: UTCDateTime,
    window_seconds: float,
    step_seconds: float,
    *,
    threshold_p: float = 0.3,
    threshold_s: float = 0.3,
) -> list[InferContext]:
    """Slice an archived time range into :class:`InferContext` windows.

    The bridge between :class:`~seedlink_dashboard.storage.archive_reader.
    ArchiveReader` (read-only file access, storage layer) and the agent
    interface, for :meth:`AIEngine.engage_archive`. Reads each component
    once over ``[t_start, t_end]`` then steps a ``window_seconds`` window by
    ``step_seconds``. A window is **dropped** unless every *read* component
    (those that had any data in the range) has full, gap-free coverage of
    it (no masked samples, full sample count) — honest about gaps rather
    than feeding zeros or a partial component set to the model (rule 8: the
    file is the truth). Returns ``InferContext.live=False``.

    Raises:
        ValueError: if ``step_seconds`` is not positive (would not advance).
    """
    from seedlink_dashboard.core.models import StreamID

    if step_seconds <= 0:
        raise ValueError(f"step_seconds must be positive, got {step_seconds}")

    traces: dict[str, object] = {}
    fs = 0.0
    for comp, nslc in group.items():
        try:
            sid = StreamID.from_trace_id(nslc)
        except ValueError:
            continue
        st = reader.read_window(sid, t_start, t_end, device_name=device)
        if len(st) == 0:
            continue
        tr = st[0]
        traces[comp] = tr
        fs = float(tr.stats.sampling_rate)
    if not traces or fs <= 0:
        return []

    need = max(1, round(window_seconds * fs))
    windows: list[InferContext] = []
    t = t_start
    while t + window_seconds <= t_end + 1e-9:
        samples: dict[str, np.ndarray] = {}
        for comp, tr in traces.items():
            seg = tr.slice(t, t + window_seconds)  # type: ignore[attr-defined]
            data = seg.data
            # Reject windows that straddle a gap (masked) or fall short.
            if np.ma.isMaskedArray(data) and np.ma.is_masked(data):
                continue
            arr = np.ma.getdata(data).astype(np.float32)
            if arr.shape[0] < need:
                continue
            samples[comp] = arr[:need]
        # Drop the whole window unless EVERY read component covers it
        # (matches the docstring: no partial component set reaches the
        # model). ``traces`` holds only components that had data in range.
        if samples and len(samples) == len(traces):
            station_key = ".".join(next(iter(group.values())).split(".")[:2])
            windows.append(
                InferContext(
                    device=device,
                    station_key=station_key,
                    nslc_by_component=dict(group),
                    samples_by_component=samples,
                    fs=fs,
                    t_start=t,
                    window_seconds=window_seconds,
                    live=False,
                    threshold_p=threshold_p,
                    threshold_s=threshold_s,
                )
            )
        t = t + step_seconds
    return windows
