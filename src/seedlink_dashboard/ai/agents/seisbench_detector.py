"""SeisBench EQTransformer detection agent — the 2nd concrete :class:`AIAgent`
(M10 Stage A).

This is the *control test* of the M9 :class:`AIAgent` abstraction: a second
pretrained, inference-only agent that produces a different *shape* of output
(span-style detection SEGMENTS, not instantaneous P/S onsets). It wraps
SeisBench's EQTransformer — the only supported model with a dedicated
**detection** probability channel — behind the same agent interface.

Import discipline mirrors :mod:`seedlink_dashboard.ai.agents.seisbench_picker`:
the module top imports only stdlib + numpy + obspy + the torch-free ``ai``
base. ``torch`` / ``seisbench`` are imported lazily inside ``warm_up`` /
``infer`` (which run on the dedicated AI worker thread), and the heavy model
load reuses the picker's LRU-cached loader so re-engaging stays warm.

Output shape — the abstraction gap this agent exercises. A phase pick is an
*instant* (``AIAnnotation.t``); an EQTransformer detection is a *segment*
(``start_time``..``end_time``). M9's :class:`AIAnnotation` modelled only the
instant; M10 adds one optional ``t_end`` field so a segment persists as a
CLOSED :class:`~seedlink_dashboard.core.models.Detection` (both ``t_on`` and
``t_off`` set), exactly like an STA/LTA detection — not a wrong instantaneous
line.

Domain honesty: identical to the picker — EQTransformer is a broadband
seismometer @100 Hz tectonic-earthquake model, so engaging it on the Echos
accelerometer (``HN*`` @500 Hz) produces the same honest warning.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import structlog
from obspy import Stream, Trace
from obspy.core.utcdatetime import UTCDateTime

from seedlink_dashboard.ai.agents.seisbench_picker import _component_nslc, _load_model
from seedlink_dashboard.ai.base import AgentParam, AIAgent, AIAnnotation, InferContext
from seedlink_dashboard.ai.domain import DomainSpec

_log = structlog.get_logger(__name__)

# EQTransformer is the only supported model with a detection channel.
_MODEL = "eqtransformer"

# Post-processing constants (CLAUDE.md: no magic numbers).
_EDGE_MARGIN_S = 1.0  # reject segments touching a window edge (artifact-prone)
_PROB_CURVE_POINTS = 300  # decimate the detection curve to ~this many points
_FULL_COMPONENTS = ("Z", "N", "E")

# The sentinel phase that drives the GREEN marker colour for span-style
# detections; threaded through the single-source ``marker_style.marker_color``
# (rule 10) — no second colour map anywhere.
_DETECTION_PHASE = "detection"


class SeisBenchDetector(AIAgent):
    """EQTransformer event-detection agent (span-style output).

    Emits one :class:`AIAnnotation` per detection segment, with ``t`` =
    segment start and ``t_end`` = segment end, so the engine persists a
    closed :class:`Detection` row. Fixed to EQTransformer (the only model
    exposing a ``_Detection`` channel).
    """

    def __init__(
        self,
        weights: str = "instance",
        device: str = "cpu",
        detection_threshold: float = 0.3,
    ) -> None:
        self._weights = weights
        self._device = device
        self._detection_threshold = detection_threshold
        self._net: Any | None = None
        # EQTransformer weights train at 100 Hz; confirmed at warm_up from
        # the loaded model's ``sampling_rate``.
        self._sampling_rate = 100.0

    # ------------------------------------------------------------------
    # AIAgent interface
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return f"{_MODEL}/{self._weights} (detection)"

    @property
    def kind(self) -> str:
        # Detection.kind for span-style EQTransformer detections; distinct
        # from the picker's model-family kind ("eqtransformer") so the
        # table/markers can tell an onset pick from a detection segment.
        return "eqt_detection"

    @property
    def domain_spec(self) -> DomainSpec:
        # Same domain as the picker: detection is still a tectonic,
        # broadband-seismometer @100 Hz model.
        return DomainSpec(
            expected_instrument="broadband seismometer",
            expected_band_hz=(1.0, 45.0),
            expected_event_type="tectonic earthquake",
            trained_sampling_rate=self._sampling_rate,
            required_components=3,
            allow_single_component=True,
            notes=(
                "EQTransformer detection channel, pretrained on broadband "
                "seismometers @100 Hz for tectonic earthquakes. Out-of-domain "
                "detections (accelerometers, very high or very low sample "
                "rates) may be meaningless."
            ),
        )

    def required_sampling_rate(self) -> float | None:
        return self._sampling_rate

    def required_components(self) -> int:
        return 3

    def engage_params(self) -> list[AgentParam]:
        # No model choice — the detector is fixed to EQTransformer (the only
        # model with a detection channel).
        return [
            AgentParam("weights", "Weights", "text", "instance"),
            AgentParam(
                "detection_threshold",
                "Detection threshold",
                "float",
                0.3,
                minimum=0.0,
                maximum=1.0,
                step=0.05,
                decimals=2,
            ),
        ]

    def warm_up(self) -> None:
        self._net = _load_model(_MODEL, self._weights, self._device)
        sr = getattr(self._net, "sampling_rate", None)
        if sr:
            self._sampling_rate = float(sr)

    def infer(self, context: InferContext) -> list[AIAnnotation]:
        net = self._net
        if net is None:  # pragma: no cover - warm_up always runs first
            raise RuntimeError("SeisBenchDetector.infer called before warm_up")
        if context.n_samples == 0:
            return []

        stream = self._build_stream(context)
        stream = self._resample(stream, context)

        # InferContext carries only threshold_p / threshold_s; EQTransformer's
        # detection channel is governed by ``detection_threshold``. We map
        # threshold_p → the detection threshold (the engagement UI exposes a
        # single primary threshold per agent; for a detector that is the
        # detection threshold). P/S thresholds keep their defaults — we do
        # not emit picks here, only segments.
        detections = self._classify_segments(net, stream, context.threshold_p)
        curve = self._detection_curve(net, stream)

        window_t_end = context.t_start + context.n_samples / context.fs
        nslc_z = context.nslc_by_component.get("Z") or next(
            iter(context.nslc_by_component.values())
        )
        return self._post_process(
            detections,
            context=context,
            window_t_end=window_t_end,
            default_nslc=nslc_z,
            curve=curve,
        )

    def release(self) -> None:
        # Drop our reference; _load_model's LRU cache keeps the model warm
        # for a fast re-engage (bounded at maxsize=4).
        self._net = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build_stream(self, context: InferContext) -> Stream:
        """Assemble an ObsPy Stream, zero-padding any missing ZNE component."""
        traces: list[Trace] = []
        ref_nslc = next(iter(context.nslc_by_component.values()))
        n = context.n_samples
        for comp in _FULL_COMPONENTS:
            nslc = context.nslc_by_component.get(comp)
            if nslc is not None and comp in context.samples_by_component:
                data = np.asarray(context.samples_by_component[comp], dtype=np.float32)
            else:
                nslc = _component_nslc(ref_nslc, comp)
                data = np.zeros(n, dtype=np.float32)
            net, sta, loc, cha = nslc.split(".")
            tr = Trace(data=data.astype(np.float64))
            tr.stats.network, tr.stats.station = net, sta
            tr.stats.location, tr.stats.channel = loc, cha
            tr.stats.sampling_rate = context.fs
            tr.stats.starttime = context.t_start
            traces.append(tr)
        return Stream(traces=traces)

    def _resample(self, stream: Stream, context: InferContext) -> Stream:
        """Resample to the model's rate ourselves (rule 7: observable wait)."""
        target = self._sampling_rate
        if abs(context.fs - target) < 1e-6:
            return stream
        t0 = time.monotonic()
        stream = stream.copy()
        stream.resample(target, no_filter=False)
        _log.debug(
            "ai_resample_done",
            from_fs=context.fs,
            to_fs=target,
            elapsed_ms=round((time.monotonic() - t0) * 1000.0, 1),
        )
        return stream

    @staticmethod
    def _classify_segments(net: Any, stream: Stream, detection_threshold: float) -> list[Any]:
        """Return EQTransformer detection segments (start/end/peak_value)."""
        result = net.classify(stream, detection_threshold=detection_threshold)
        detections = getattr(result, "detections", None)
        if detections is None:
            return []
        return list(detections)

    def _detection_curve(self, net: Any, stream: Stream) -> Trace | None:
        """Decimated ``_Detection`` probability trace for the detail pane.

        Best-effort: a failure here must not lose the segments (which come
        from ``classify`` independently), so it is guarded.
        """
        try:
            annotations = net.annotate(stream)
        except Exception as exc:
            _log.debug("ai_annotate_failed", error=str(exc))
            return None
        for tr in annotations:
            if tr.stats.channel.endswith("_Detection"):
                return tr
        return None

    def _post_process(
        self,
        detections: list[Any],
        *,
        context: InferContext,
        window_t_end: UTCDateTime,
        default_nslc: str,
        curve: Trace | None,
    ) -> list[AIAnnotation]:
        """Edge-reject segments, then map each to one span-style AIAnnotation."""
        edge_lo = context.t_start + _EDGE_MARGIN_S
        edge_hi = window_t_end - _EDGE_MARGIN_S
        meta_curve = _decimate_curve(curve)
        annotations: list[AIAnnotation] = []
        for d in detections:
            t_on = UTCDateTime(d.start_time)
            t_off = UTCDateTime(d.end_time)
            # Reject segments whose start OR end falls within the edge margin
            # (artifact-prone, like the picker's edge rejection).
            if t_on < edge_lo or t_off > edge_hi:
                continue
            peak = getattr(d, "peak_value", None)
            score = float(peak) if peak is not None else float(context.threshold_p)
            meta: dict[str, object] = {
                "phase": _DETECTION_PHASE,
                "detection_trace_id": str(getattr(d, "trace_id", "")),
                "detection_threshold": float(context.threshold_p),
                **meta_curve,
            }
            annotations.append(
                AIAnnotation(
                    device=context.device,
                    nslc=default_nslc,
                    kind=self.kind,
                    phase=_DETECTION_PHASE,
                    t=t_on,
                    t_end=t_off,
                    score=score,
                    model_name=_MODEL,
                    model_weights=self._weights,
                    window_t_start=context.t_start,
                    window_t_end=window_t_end,
                    meta=meta,
                )
            )
        return annotations


def _decimate_curve(curve: Trace | None) -> dict[str, object]:
    """Decimate the ``_Detection`` probability trace to a JSON-friendly payload.

    Stores ``prob_t0`` (POSIX seconds of sample 0), ``prob_dt`` (seconds per
    stored point) and the decimated ``prob_det`` list (≤ ~300 points — the
    ``meta_json`` column must not carry full-rate arrays). Empty dict if no
    curve was produced.
    """
    if curve is None:
        return {}
    data = np.asarray(curve.data, dtype=np.float32)
    if data.size == 0:
        return {}
    step = max(1, len(data) // _PROB_CURVE_POINTS)
    return {
        "prob_det": data[::step].tolist(),
        "prob_t0": float(curve.stats.starttime.timestamp),
        "prob_dt": float(curve.stats.delta * step),
    }
