"""The pluggable AI-agent interface (M9).

This module is the **foundation** of the AI subsystem and is deliberately
*torch-free*: it imports nothing from ``seisbench`` or ``torch`` so the
whole app — and the default test gate — runs without the optional ``ai``
extra installed. Concrete agents (e.g. :mod:`seedlink_dashboard.ai.agents.
seisbench_picker`) import the heavy libraries *lazily*, inside ``warm_up``
/ ``infer``, which run off the GUI thread.

An :class:`AIAgent` is engaged on demand on chosen device/channels by the
user. It is a *best-effort consumer* (CLAUDE.md rule 11): the engine pulls
recent samples from the ring buffer and hands the agent a fully
self-describing :class:`InferContext`; the agent never reaches back into
the engine, so it can never back-pressure acquisition/DSP/detection/
storage. An agent emits domain-neutral :class:`AIAnnotation` objects which
:class:`seedlink_dashboard.core.ai_engine.AIEngine` maps onto the M8
:class:`~seedlink_dashboard.core.models.Detection` for persistence,
table, markers and the detail pane.

The abstraction is intentionally agnostic to *what kind* of agent it is.
A pretrained phase picker or detector implements a three-method
*inference-only* lifecycle (``warm_up`` / ``infer`` / ``release``) and
returns :class:`AIAnnotation`. The only extension points are the ``kind``
string and the ``meta`` dict — both open by construction — so a new
inference agent type plugs in without touching the engine.

**M10 fit-then-infer extension.** A *learning* agent (a classifier or
anomaly detector that must learn "normal" from the user's own channel
before it can run) needs an OPTIONAL fourth phase that the three-method
lifecycle does not model: ``fit`` (learn a baseline) plus
``serialize_state`` / ``load_state`` (persist what was learned so the
fit runs once, not every engage). These have concrete, inert DEFAULTS —
:attr:`AIAgent.requires_fit` returns ``False``, :meth:`AIAgent.fit`
raises, :meth:`AIAgent.serialize_state` returns ``None`` and
:meth:`AIAgent.load_state` is a no-op — so every pretrained
inference-only agent (the picker, the detector) is byte-for-byte
unaffected and the engine's inference path is unchanged. ``fit`` is
handed a self-describing :class:`FitContext` (the same rule-11
discipline as :class:`InferContext`: the engine hands in the raw
baseline samples; the agent never reaches back) and returns a
human-facing :class:`FitResult`; the *learned parameters* live inside
the agent, which serialises them as opaque bytes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from obspy.core.utcdatetime import UTCDateTime

    from seedlink_dashboard.ai.domain import DomainSpec


def _never_stop() -> bool:
    """Default :attr:`FitContext.should_stop` — never asks to stop.

    A module-level function (not a lambda) so :class:`FitContext` can stay
    ``slots=True`` with a clean ``Callable[[], bool]`` default.
    """
    return False


def _no_progress(fraction: float, message: str) -> None:
    """Default :attr:`FitContext.progress` — a no-op sink.

    A module-level function (not a lambda) for the same slots/typing
    cleanliness reason as :func:`_never_stop`.
    """
    return None


@dataclass(frozen=True, slots=True)
class AgentParam:
    """One engage-time parameter an agent exposes to the engage dialog.

    The engage dialog renders one field per :class:`AgentParam` and passes
    the collected values to the agent factory — so the agent is the *single
    source of truth* for its own engage-time parameters (parallel to
    :attr:`AIAgent.requires_fit`). Adding a new agent that overrides
    :meth:`AIAgent.engage_params` makes the dialog grow the right fields with
    no dialog-code change.

    ``name`` is the constructor kwarg name; ``kind`` selects the widget the
    dialog renders (``"choice"`` → combo, ``"float"`` → double-spin,
    ``"int"`` → spin, ``"text"`` → line-edit). ``default`` seeds the field;
    ``choices`` populates a choice combo; ``minimum`` / ``maximum`` / ``step``
    / ``decimals`` bound the numeric spins. The dataclass is plain data
    (torch-free) so reading it costs nothing.
    """

    name: str
    label: str
    kind: str  # "choice" | "float" | "int" | "text"
    default: object
    choices: tuple[str, ...] = ()  # for kind=="choice"
    minimum: float = 0.0  # for float/int
    maximum: float = 1.0
    step: float = 0.05
    decimals: int = 2  # for float


@dataclass(frozen=True, slots=True)
class InferContext:
    """One self-describing inference request.

    Built on the GUI thread (cheap, lock-protected ring-buffer reads via
    :meth:`StreamingEngine.read_recent`) and consumed on the AI worker
    thread. It carries the **raw numpy windows** for every component so
    the worker never calls back into the engine — the single most
    important invariant for rule 11 (the agent cannot touch, and so
    cannot stall, the data path).

    Components are keyed by their SEED orientation letter (``"Z"`` /
    ``"N"`` / ``"E"``). ``samples_by_component`` arrays are float32 and
    all the same length; ``t_start`` is the wall-clock time of sample 0
    (shared across components). ``fs`` is the *native* sampling rate of
    the pulled window — the agent resamples to its own required rate and
    restores native timing in the annotations.
    """

    device: str
    station_key: str  # "NET.STA" — the grouping unit for 3-component picking
    nslc_by_component: dict[str, str]
    samples_by_component: dict[str, np.ndarray]
    fs: float
    t_start: UTCDateTime
    window_seconds: float
    live: bool = True  # False for archive replay (Stage C)
    threshold_p: float = 0.3
    threshold_s: float = 0.3

    @property
    def n_samples(self) -> int:
        """Length of the (aligned) component windows; 0 if empty."""
        for arr in self.samples_by_component.values():
            return int(arr.shape[0])
        return 0


@dataclass(frozen=True, slots=True)
class FitContext:
    """One self-describing *baseline-learning* request (M10 fit-then-infer).

    The learning analogue of :class:`InferContext`, with the SAME rule-11
    discipline: it carries the **raw baseline samples** the engine pulled
    for the agent, so :meth:`AIAgent.fit` never calls back into the engine
    and so can never back-pressure the data path. The baseline window is
    longer than an inference window (``baseline_seconds``, e.g. minutes of
    quiet data the UI asked the user to pick).

    Interruptibility + observability (CLAUDE.md rule 7): ``should_stop`` is
    a cooperative cancellation flag the agent must poll inside any long
    loop (the engine wires it to the worker's stop flag, so disengaging
    mid-fit returns within one polling period); ``progress`` is a
    ``(fraction, message)`` sink the agent calls to keep the structured-log
    channel alive during the wait.
    """

    device: str
    station_key: str  # "NET.STA"
    nslc_by_component: dict[str, str]
    samples_by_component: dict[str, np.ndarray]  # float32 baseline windows
    fs: float
    t_start: UTCDateTime
    baseline_seconds: float
    should_stop: Callable[[], bool] = _never_stop
    progress: Callable[[float, str], None] = _no_progress

    @property
    def n_samples(self) -> int:
        """Length of the (aligned) component baseline windows; 0 if empty."""
        for arr in self.samples_by_component.values():
            return int(arr.shape[0])
        return 0


@dataclass(frozen=True, slots=True)
class FitResult:
    """The human-facing outcome of :meth:`AIAgent.fit` (M10).

    This is NOT the learned state blob — the fitted parameters live inside
    the agent and are persisted via :meth:`AIAgent.serialize_state`. This
    object is a summary for the panel / structured log only.
    """

    summary: str
    n_windows: int = 0
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AIAnnotation:
    """A single, domain-neutral model output.

    A phase picker emits one annotation per phase pick; a future
    classifier would emit one per labelled window; an anomaly agent one
    per flagged span. The mapping to :class:`Detection` (done by
    :class:`AIEngine`) is purely mechanical:

    * ``Detection.kind`` ← :attr:`kind` (the model family, e.g.
      ``"phasenet"`` — NOT the phase; the phase lives in ``meta``),
    * ``Detection.t_on`` ← :attr:`t`, ``Detection.t_off`` ← :attr:`t_end`
      (``None`` for an instantaneous pick → an open/onset row; a real
      end time for a span-style detection → a closed segment row),
    * ``Detection.score`` ← :attr:`score` (peak probability),
    * ``Detection.meta`` ← :attr:`meta` merged with provenance
      (``phase``, ``agent``, ``weights``, the decimated probability
      curves ``prob_t0`` / ``prob_dt`` / ``prob_p`` / ``prob_s``, and
      the window bounds).

    Probability curves stored in ``meta`` MUST be decimated to a few
    hundred points before they reach the DAO — the ``meta_json`` column
    must not carry full-rate per-sample arrays.
    """

    device: str
    nslc: str  # the component the annotation lands on (Z for P/S picks)
    kind: str  # model family — "phasenet" / "eqtransformer"
    phase: str  # "P" / "S" / ... — drives marker colour, lives in meta
    t: UTCDateTime
    score: float
    model_name: str
    model_weights: str
    window_t_start: UTCDateTime
    window_t_end: UTCDateTime
    meta: dict[str, object] = field(default_factory=dict)
    # The segment end for span-style detections (e.g. eqt_detection);
    # ``None`` for instantaneous picks (P/S onsets). Conceptually pairs
    # with ``t`` (the segment start): the engine maps ``t`` → ``t_on`` and
    # ``t_end`` → ``t_off`` so a span persists as a CLOSED segment row
    # (both set), exactly like an STA/LTA detection. Defaulting to ``None``
    # keeps the picker's instantaneous behaviour (t_off=None) byte-for-byte
    # unchanged. (Placed last because dataclass fields with defaults must
    # follow the non-default ones above.)
    t_end: UTCDateTime | None = None


class AIAgent(ABC):
    """The pluggable inference agent.

    Lifecycle: ``warm_up`` (load the model — heavy, hundreds of MB, runs
    on the worker thread), then repeated ``infer`` calls, then
    ``release`` (free model / device memory on disengage). All three run
    off the GUI thread *and* off the data-path threads.

    Implementations must keep ``__init__`` cheap and torch-free —
    construction happens on the GUI thread when the engagement dialog is
    populated. Heavy imports (``torch``, ``seisbench``) belong in
    ``warm_up`` / ``infer``.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-facing identifier, e.g. ``"PhaseNet (instance)"``."""

    @property
    @abstractmethod
    def kind(self) -> str:
        """The ``Detection.kind`` this agent produces (``"phasenet"`` …)."""

    @property
    @abstractmethod
    def domain_spec(self) -> DomainSpec:
        """The agent's declared domain of validity (the honesty layer)."""

    @abstractmethod
    def required_sampling_rate(self) -> float | None:
        """Sampling rate the model expects, or ``None`` if rate-agnostic."""

    @abstractmethod
    def required_components(self) -> int:
        """Number of components the model wants (e.g. 3 for Z/N/E)."""

    @abstractmethod
    def warm_up(self) -> None:
        """Load the model. Heavy; called once on engage on the worker thread."""

    @abstractmethod
    def infer(self, context: InferContext) -> list[AIAnnotation]:
        """Run inference over one window; return zero or more annotations."""

    def release(self) -> None:
        """Free model / device memory. Default no-op; override if needed."""
        return None

    def engage_params(self) -> list[AgentParam]:
        """Engage-time parameters the dialog should collect for this agent.

        The dialog renders one field per :class:`AgentParam` and passes the
        collected ``{param.name: value}`` to the agent factory — the agent is
        the single source of truth for its own parameters. Default: none.
        Override to expose the agent's real constructor params (model /
        weights / thresholds / etc.). Reading this is torch-free.
        """
        return []

    # ------------------------------------------------------------------
    # M10 fit-then-infer extension (OPTIONAL fourth phase).
    #
    # All four members below have concrete, inert defaults: inference-only
    # agents (the pretrained picker / detector) use them as-is and are
    # completely unaffected — they are NOT @abstractmethod. Only a learning
    # agent (a classifier / anomaly detector that learns a baseline from the
    # user's own channel) overrides them.
    # ------------------------------------------------------------------
    @property
    def requires_fit(self) -> bool:
        """Whether this agent must :meth:`fit` a baseline before inference.

        M10 fit-then-infer extension; inference-only agents use the default
        ``False`` and are unaffected — the engine then skips the fit phase
        entirely and never touches the state store.
        """
        return False

    def fit(self, context: FitContext) -> FitResult:
        """Learn a baseline from ``context`` (the baseline window).

        M10 fit-then-infer extension; inference-only agents leave the
        default, which raises. Learning agents override to learn from the
        raw baseline samples, store the fitted parameters on ``self`` and
        return a :class:`FitResult` summary. Must poll ``context.should_stop``
        inside any long loop (rule 7 interruptibility).
        """
        raise NotImplementedError("agent does not support fit")

    def serialize_state(self) -> bytes | None:
        """Serialise the learned parameters to opaque bytes, or ``None``.

        M10 fit-then-infer extension; inference-only agents return the
        default ``None`` (nothing learned to persist). Learning agents
        override to return their fitted state in any self-chosen format
        (JSON, npz, torch) — the engine / state store treats it as opaque
        bytes, never learning the agent's internals.
        """
        return None

    def load_state(self, data: bytes) -> None:
        """Restore the learned parameters from :meth:`serialize_state` bytes.

        M10 fit-then-infer extension; inference-only agents leave the
        default no-op. Learning agents override to restore from bytes so a
        persisted fit is reused and the fit phase is skipped on re-engage.
        """
        return None
