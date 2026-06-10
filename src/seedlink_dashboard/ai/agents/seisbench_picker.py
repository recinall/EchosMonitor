"""SeisBench phase picker — the first concrete :class:`AIAgent` (M9 Stage B).

Wraps SeisBench's PhaseNet / EQTransformer / GPD behind the project's
agent interface. Verified against the installed API (seisbench 0.11.6, see
``scripts/check_seisbench.py``): ``from_pretrained`` / ``list_pretrained``
/ ``classify`` / ``annotate``, all three models ``sampling_rate=100`` with
``component_order="ZNE"``.

Import discipline (the app must run without the ``ai`` extra): the module
top imports only stdlib + numpy + obspy + the torch-free ``ai`` base. The
heavy ``torch`` / ``seisbench`` imports live **inside** ``warm_up`` /
``infer``, which run on the dedicated AI worker thread — never the GUI
thread, never the data-path thread. Constructing a :class:`SeisBenchPicker`
is cheap and torch-free; only ``warm_up`` touches the model.

Domain honesty: the picker declares a broadband-seismometer @100 Hz
tectonic-earthquake domain, so engaging it on the Echos accelerometer
(``HN*`` @500 Hz) produces an honest warning in the engagement UI.
"""

from __future__ import annotations

import functools
import time
from typing import Any

import numpy as np
import structlog
from obspy import Stream, Trace
from obspy.core.utcdatetime import UTCDateTime

from seedlink_dashboard.ai.base import AgentParam, AIAgent, AIAnnotation, InferContext
from seedlink_dashboard.ai.domain import DomainSpec

_log = structlog.get_logger(__name__)

# SeisBench model registry keyed by our ``model`` config string.
_MODEL_CLASSES = ("phasenet", "eqtransformer", "gpd")

# Post-processing constants (CLAUDE.md: no magic numbers).
_NMS_WINDOW_S = 0.5  # suppress same-phase picks within ±this of a stronger one
_EDGE_MARGIN_S = 1.0  # reject picks within this of a window edge (artifact-prone)
_PROB_CURVE_POINTS = 300  # decimate probability curves to ~this many points for meta
_FULL_COMPONENTS = ("Z", "N", "E")


@functools.lru_cache(maxsize=4)
def _load_model(model: str, weights: str, device: str) -> Any:
    """Load + cache a SeisBench model. Heavy (100s of MB); never per-window.

    Cached by ``(model, weights, device)`` so re-engaging the same picker
    reuses the warm model. ``release`` drops the agent's reference but the
    cache deliberately retains up to 4 models warm across engagements
    (matches the seisbench-models skill guidance).
    """
    import seisbench.models as sbm  # lazy — torch import happens here

    cls_map = {
        "phasenet": sbm.PhaseNet,
        "eqtransformer": sbm.EQTransformer,
        "gpd": sbm.GPD,
    }
    cls = cls_map[model]
    net = cls.from_pretrained(weights)
    net.eval()
    net.to(device)
    return net


class SeisBenchPicker(AIAgent):
    """PhaseNet / EQTransformer / GPD phase picker."""

    def __init__(
        self,
        model: str = "phasenet",
        weights: str = "instance",
        device: str = "cpu",
        threshold_p: float = 0.3,
        threshold_s: float = 0.3,
    ) -> None:
        if model not in _MODEL_CLASSES:
            raise ValueError(f"unknown model {model!r}; expected one of {_MODEL_CLASSES}")
        self._model = model
        self._weights = weights
        self._device = device
        self._threshold_p = threshold_p
        self._threshold_s = threshold_s
        self._net: Any | None = None
        # All current SeisBench picker weights train at 100 Hz; confirmed
        # at warm_up from the loaded model's ``sampling_rate``.
        self._sampling_rate = 100.0

    # ------------------------------------------------------------------
    # AIAgent interface
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return f"{self._model}/{self._weights}"

    @property
    def kind(self) -> str:
        # Detection.kind = the model family, per the M9 spec; the phase
        # (P/S) is carried in Detection.meta and drives marker colour.
        return self._model

    @property
    def domain_spec(self) -> DomainSpec:
        return DomainSpec(
            expected_instrument="broadband seismometer",
            expected_band_hz=(1.0, 45.0),
            expected_event_type="tectonic earthquake",
            trained_sampling_rate=self._sampling_rate,
            required_components=3,
            # PhaseNet/EQT/GPD want Z/N/E; we zero-pad missing components
            # but warn (single-component picks are degraded).
            allow_single_component=True,
            notes=(
                "Pretrained on broadband seismometers @100 Hz for tectonic "
                "earthquakes. Out-of-domain picks (accelerometers, very high "
                "or very low sample rates) may be meaningless."
            ),
        )

    def required_sampling_rate(self) -> float | None:
        return self._sampling_rate

    def required_components(self) -> int:
        return 3

    def engage_params(self) -> list[AgentParam]:
        return [
            AgentParam("model", "Model", "choice", "phasenet", choices=_MODEL_CLASSES),
            AgentParam("weights", "Weights", "text", "instance"),
            AgentParam(
                "threshold_p",
                "P threshold",
                "float",
                0.3,
                minimum=0.0,
                maximum=1.0,
                step=0.05,
                decimals=2,
            ),
            AgentParam(
                "threshold_s",
                "S threshold",
                "float",
                0.3,
                minimum=0.0,
                maximum=1.0,
                step=0.05,
                decimals=2,
            ),
        ]

    def warm_up(self) -> None:
        self._net = _load_model(self._model, self._weights, self._device)
        sr = getattr(self._net, "sampling_rate", None)
        if sr:
            self._sampling_rate = float(sr)

    def infer(self, context: InferContext) -> list[AIAnnotation]:
        net = self._net
        if net is None:  # pragma: no cover - warm_up always runs first
            raise RuntimeError("SeisBenchPicker.infer called before warm_up")
        if context.n_samples == 0:
            return []

        stream = self._build_stream(context)
        stream = self._resample(stream, context)

        thresholds = {"P_threshold": context.threshold_p, "S_threshold": context.threshold_s}
        picks = self._classify_picks(net, stream, thresholds)
        curves = self._probability_curves(net, stream)

        window_t_end = context.t_start + context.n_samples / context.fs
        nslc_z = context.nslc_by_component.get("Z") or next(
            iter(context.nslc_by_component.values())
        )
        return self._post_process(
            picks,
            context=context,
            window_t_end=window_t_end,
            default_nslc=nslc_z,
            curves=curves,
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
                # Zero-pad the missing component (documented degraded mode).
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
    def _classify_picks(net: Any, stream: Stream, thresholds: dict[str, float]) -> list[Any]:
        """Return SeisBench picks (objects with peak_time/phase/peak_value)."""
        result = net.classify(stream, **thresholds)
        picks = getattr(result, "picks", None)
        if picks is None:
            return []
        return list(picks)

    def _probability_curves(self, net: Any, stream: Stream) -> dict[str, Any]:
        """Decimated P/S probability traces for the detail-pane overlay.

        Best-effort: a failure here must not lose the picks, so it is
        guarded — picks come from ``classify`` independently.
        """
        try:
            annotations = net.annotate(stream)
        except Exception as exc:
            _log.debug("ai_annotate_failed", error=str(exc))
            return {}
        out: dict[str, Any] = {}
        for tr in annotations:
            chan = tr.stats.channel
            if chan.endswith("_P"):
                out["P"] = tr
            elif chan.endswith("_S"):
                out["S"] = tr
        return out

    def _post_process(
        self,
        picks: list[Any],
        *,
        context: InferContext,
        window_t_end: UTCDateTime,
        default_nslc: str,
        curves: dict[str, Any],
    ) -> list[AIAnnotation]:
        """Edge-reject, NMS per phase, then map to AIAnnotation."""
        edge_lo = context.t_start + _EDGE_MARGIN_S
        edge_hi = window_t_end - _EDGE_MARGIN_S
        kept = _nms(
            [
                p
                for p in picks
                if getattr(p, "phase", None) in ("P", "S")
                and edge_lo <= UTCDateTime(p.peak_time) <= edge_hi
            ]
        )
        meta_curves = _decimate_curves(curves)
        annotations: list[AIAnnotation] = []
        for p in kept:
            phase = str(p.phase)
            # SeisBench picks are station-level: ``p.trace_id`` is
            # "NET.STA.LOC." with an EMPTY channel, which is not a valid
            # 4-part NSLC and would be rejected by the persist path. Anchor
            # the pick to the concrete Z-component NSLC (the table/marker
            # stream); keep the raw station id in meta for provenance.
            meta: dict[str, object] = {
                "phase": phase,
                "pick_trace_id": str(getattr(p, "trace_id", "")),
                # Stamp the thresholds actually used so the detail pane can
                # draw the per-phase decision line at the real engagement
                # threshold (not the pick's own peak).
                "threshold_p": float(context.threshold_p),
                "threshold_s": float(context.threshold_s),
                **meta_curves,
            }
            annotations.append(
                AIAnnotation(
                    device=context.device,
                    nslc=default_nslc,
                    kind=self.kind,
                    phase=phase,
                    t=UTCDateTime(p.peak_time),
                    score=float(p.peak_value),
                    model_name=self._model,
                    model_weights=self._weights,
                    window_t_start=context.t_start,
                    window_t_end=window_t_end,
                    meta=meta,
                )
            )
        return annotations


def _component_nslc(ref_nslc: str, comp: str) -> str:
    """Derive a sibling component's NSLC by swapping the orientation letter."""
    net, sta, loc, cha = ref_nslc.split(".")
    if len(cha) == 3:
        cha = cha[:2] + comp
    return f"{net}.{sta}.{loc}.{cha}"


def _nms(picks: list[Any]) -> list[Any]:
    """Non-maximum suppression within ±_NMS_WINDOW_S per phase.

    Keeps the highest ``peak_value`` among picks of the same phase that
    fall within the window; a classic de-duplication of jittery onsets.
    """
    by_phase: dict[str, list[Any]] = {}
    for p in picks:
        by_phase.setdefault(str(p.phase), []).append(p)
    kept: list[Any] = []
    for phase_picks in by_phase.values():
        phase_picks.sort(key=lambda p: float(p.peak_value), reverse=True)
        chosen: list[Any] = []
        for p in phase_picks:
            t = UTCDateTime(p.peak_time)
            if all(abs(t - UTCDateTime(c.peak_time)) > _NMS_WINDOW_S for c in chosen):
                chosen.append(p)
        kept.extend(chosen)
    return kept


def _decimate_curves(curves: dict[str, Any]) -> dict[str, object]:
    """Decimate P/S probability traces to a small JSON-friendly payload.

    Stores ``prob_t0`` (POSIX seconds of sample 0), ``prob_dt`` (seconds
    per stored point) and the decimated ``prob_p`` / ``prob_s`` lists. The
    detail pane plots these against a wall-clock axis. Empty dict if no
    curves were produced.
    """
    if not curves:
        return {}
    out: dict[str, object] = {}
    ref: Trace | None = None
    for key in ("P", "S"):
        tr = curves.get(key)
        if tr is None:
            continue
        ref = tr if ref is None else ref
        data = np.asarray(tr.data, dtype=np.float32)
        step = max(1, len(data) // _PROB_CURVE_POINTS)
        out[f"prob_{key.lower()}"] = data[::step].tolist()
    if ref is None:
        return {}
    step = max(1, len(ref.data) // _PROB_CURVE_POINTS)
    out["prob_t0"] = float(ref.stats.starttime.timestamp)
    out["prob_dt"] = float(ref.stats.delta * step)
    return out
