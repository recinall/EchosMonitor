"""Dense autoencoder anomaly detector — the ML learning agent (M10 Stage C).

The SECOND *learning* agent (``requires_fit=True``) and the ML counterpart to
the interpretable :class:`~seedlink_dashboard.ai.agents.heuristic_classifier.
HeuristicClassifier`. It learns "normal" for WHATEVER the channel is —
seismometer background, machine hum, ocean microseism — by training a small
dense autoencoder to reconstruct the log power spectrum of short sub-windows.
At inference it slides that sub-window across the inference window, computes
the reconstruction error per position (a recon-error CURVE over time) and
flags the span(s) whose error exceeds a threshold learned from the training
errors. It is domain-AGNOSTIC by construction: it models the channel's own
spectral signature, not earthquakes.

Import discipline (the app must import this module without ``torch``): the
module top imports only stdlib + numpy + the torch-free ``ai`` base. ``torch``
is imported LAZILY inside ``fit`` / ``infer`` / ``serialize_state`` /
``load_state`` — all of which run on the dedicated AI worker thread, never the
GUI or data-path threads. Constructing the agent is cheap and torch-free.

Architecture (a SMALL dense AE over a fixed-length spectral feature):

* Feature: the log power spectrum of a sub-window (:data:`_SPECTRAL_BINS`
  log-spaced-magnitude bins of the single-sided FFT power), z-scored using
  per-bin mean/std learned at fit time. This is fs-agnostic — the bin COUNT
  is fixed, so the same network shape works at any sample rate.
* Network: ``D -> 32 -> 8 -> 32 -> D`` (:data:`_HIDDEN` / :data:`_LATENT`),
  ReLU activations, MSE reconstruction loss, Adam, :data:`_EPOCHS` epochs.
  CPU-trainable in seconds on a few minutes of baseline.
* Threshold: ``mean + k * std`` of the training reconstruction errors with
  ``k`` = :data:`_THRESHOLD_K` — a window reconstructs worse than ``k`` std
  above the training-normal error → an anomaly.

Output: one span-style :class:`AIAnnotation` per exceeding region (``t`` =
region start, ``t_end`` = region end — a SEGMENT like the EQTransformer
detector), ``phase="anomaly"``, ``score`` = the peak normalized error in the
region. The DECIMATED reconstruction-error curve is stored in ``meta`` for the
detail pane under the keys (documented for the separate detail-pane agent):

* ``recon_t0``        — POSIX epoch of curve sample 0,
* ``recon_dt``        — seconds per curve point,
* ``recon_err``       — the decimated recon-error list (≤ ~300 points),
* ``recon_threshold`` — the fitted anomaly threshold (same units as
  ``recon_err``), so the pane can draw the decision line.

Domain honesty: ``instrument_agnostic`` + ``rate_agnostic`` — it adapts to
any instrument / sample rate, so the engagement UI must NOT emit the
pretrained picker's "not a seismometer / data will be resampled" warning.
"""

from __future__ import annotations

import io
import json
from typing import Any

import numpy as np

from seedlink_dashboard.ai.base import (
    AgentParam,
    AIAgent,
    AIAnnotation,
    FitContext,
    FitResult,
    InferContext,
)
from seedlink_dashboard.ai.domain import DomainSpec

# Spectral feature length (fixed → the network shape is fs-agnostic).
_SPECTRAL_BINS = 64
# Sub-window the spectral feature is computed over (seconds).
_SUBWINDOW_SECONDS = 2.0
# Sub-window hop for the inference slide (seconds) — finer than the window so
# the recon-error curve has good temporal resolution.
_HOP_SECONDS = 0.5
# Network sizes.
_HIDDEN = 32
_LATENT = 8
# Training budget (BOUNDED — CPU-trainable in seconds).
_EPOCHS = 40
_BATCH = 16
_LEARNING_RATE = 1e-3
# Anomaly threshold = mean + k * std of training recon errors.
_THRESHOLD_K = 4.0
# Decimate the recon-error curve to at most this many points for meta.
_RECON_CURVE_POINTS = 300
# Floor for z-scoring / log so we never hit log(0) or divide-by-zero.
_EPS = 1e-9
# Progress cadence over epochs (rule 7 observability).
_PROGRESS_EVERY = 8


def spectral_feature(samples: np.ndarray, fs: float) -> np.ndarray:
    """Fixed-length log-power-spectrum feature for one sub-window.

    Pure, torch-free. Computes the single-sided FFT power, takes ``log1p``,
    then resamples (by interpolation) the spectrum onto a FIXED grid of
    :data:`_SPECTRAL_BINS` bins so the feature length is independent of the
    sub-window sample count / sample rate. A degenerate input returns zeros.

    Args:
        samples: 1-D sub-window samples.
        fs: Sample rate in Hz (must be > 0).

    Returns:
        Float32 feature vector of length :data:`_SPECTRAL_BINS`.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 2 or fs <= 0:
        return np.zeros(_SPECTRAL_BINS, dtype=np.float32)
    x = x - float(np.mean(x))
    power = np.abs(np.fft.rfft(x)) ** 2
    log_power = np.log1p(power)
    # Resample the (variable-length) spectrum onto the fixed bin grid.
    src = np.linspace(0.0, 1.0, num=log_power.size)
    dst = np.linspace(0.0, 1.0, num=_SPECTRAL_BINS)
    feat: np.ndarray = np.asarray(np.interp(dst, src, log_power), dtype=np.float32)
    return feat


def _build_feature_matrix(
    arr: np.ndarray, fs: float, n_sub: int, n_hop: int
) -> tuple[np.ndarray, np.ndarray]:
    """Slide the sub-window over ``arr`` → (feature matrix, start-sample index).

    Returns ``(M, starts)`` where ``M`` is ``(n_windows, _SPECTRAL_BINS)`` and
    ``starts`` are the sub-window start sample indices (for mapping the recon
    error back to a time on the inference window).
    """
    feats: list[np.ndarray] = []
    starts: list[int] = []
    if arr.size < n_sub:
        return np.zeros((0, _SPECTRAL_BINS), dtype=np.float32), np.zeros(0, dtype=np.int64)
    i = 0
    while i + n_sub <= arr.size:
        feats.append(spectral_feature(arr[i : i + n_sub], fs))
        starts.append(i)
        i += n_hop
    return np.vstack(feats).astype(np.float32), np.asarray(starts, dtype=np.int64)


class AutoencoderAnomaly(AIAgent):
    """Dense-autoencoder anomaly detector (span-style output, learns normal)."""

    def __init__(
        self,
        *,
        device: str = "cpu",
        epochs: int = _EPOCHS,
        seed: int = 0,
    ) -> None:
        self._device = device
        self._epochs = int(epochs)
        self._seed = int(seed)
        # Learned state (None until fit / load_state).
        self._model: Any | None = None  # torch.nn.Module
        self._feat_mu: np.ndarray | None = None
        self._feat_sigma: np.ndarray | None = None
        self._threshold: float | None = None

    # ------------------------------------------------------------------
    # AIAgent interface
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return "Autoencoder anomaly detector"

    @property
    def kind(self) -> str:
        return "autoencoder_anomaly"

    @property
    def domain_spec(self) -> DomainSpec:
        # Domain-AGNOSTIC: it adapts to whatever the channel is. The opt-out
        # flags make the honesty layer skip the instrument / rate warnings.
        return DomainSpec(
            expected_instrument="any waveform",
            expected_band_hz=(0.0, 0.0),
            expected_event_type="learned spectral anomaly",
            trained_sampling_rate=0.0,
            required_components=1,
            allow_single_component=True,
            instrument_agnostic=True,
            rate_agnostic=True,
            notes=(
                "Trains a small autoencoder on the channel's own spectral "
                "background; works on any instrument / sample rate. Flags "
                "spans whose reconstruction error exceeds the learned normal."
            ),
        )

    def required_sampling_rate(self) -> float | None:
        return None  # rate-agnostic; the engine must not resample for this agent

    def required_components(self) -> int:
        return 1

    def engage_params(self) -> list[AgentParam]:
        return [
            AgentParam(
                "epochs",
                "Training epochs",
                "int",
                _EPOCHS,
                minimum=5.0,
                maximum=200.0,
                step=5.0,
            ),
        ]

    def warm_up(self) -> None:
        if self._model is None or self._threshold is None:
            raise RuntimeError("AutoencoderAnomaly.warm_up before fit/load_state")

    # ------------------------------------------------------------------
    # M10 fit-then-infer overrides
    # ------------------------------------------------------------------
    @property
    def requires_fit(self) -> bool:
        return True

    def _make_model(self, torch_mod: Any) -> Any:
        """Build the dense AE ``D -> 32 -> 8 -> 32 -> D`` (ReLU, MSE)."""
        nn = torch_mod.nn
        return nn.Sequential(
            nn.Linear(_SPECTRAL_BINS, _HIDDEN),
            nn.ReLU(),
            nn.Linear(_HIDDEN, _LATENT),
            nn.ReLU(),
            nn.Linear(_LATENT, _HIDDEN),
            nn.ReLU(),
            nn.Linear(_HIDDEN, _SPECTRAL_BINS),
        )

    def fit(self, context: FitContext) -> FitResult:
        """Train the AE on the baseline spectral features (bounded epochs).

        Builds the sliding spectral feature matrix, z-scores it (storing the
        per-bin stats), trains for :data:`_EPOCHS` epochs polling
        ``context.should_stop`` between epochs (rule 7 — interruptible: a
        stop returns early WITHOUT a usable threshold), then sets the anomaly
        threshold from the training reconstruction errors.
        """
        import torch

        torch.manual_seed(self._seed)
        arr = _primary(context.samples_by_component)
        fs = context.fs
        n_sub = max(2, int(_SUBWINDOW_SECONDS * fs))
        n_hop = max(1, int(_HOP_SECONDS * fs))
        matrix, _starts = _build_feature_matrix(arr, fs, n_sub, n_hop)
        if matrix.shape[0] == 0:
            return FitResult(summary="baseline too short to train", n_windows=0)

        mu = matrix.mean(axis=0)
        sigma = np.maximum(matrix.std(axis=0), _EPS)
        self._feat_mu = mu
        self._feat_sigma = sigma
        z = (matrix - mu) / sigma

        device = torch.device(self._device)
        x = torch.from_numpy(z.astype(np.float32)).to(device)
        model = self._make_model(torch).to(device)
        opt = torch.optim.Adam(model.parameters(), lr=_LEARNING_RATE)
        loss_fn = torch.nn.MSELoss()

        n = x.shape[0]
        progress_step = max(1, self._epochs // _PROGRESS_EVERY)
        model.train()
        for epoch in range(self._epochs):
            if context.should_stop():
                # Interrupted: no usable threshold → leave model/threshold so
                # warm_up will reject (rule 7 observable interruption).
                self._model = None
                self._threshold = None
                return FitResult(summary="fit interrupted", n_windows=0)
            perm = torch.randperm(n, device=device)
            for start in range(0, n, _BATCH):
                idx = perm[start : start + _BATCH]
                batch = x[idx]
                opt.zero_grad()
                recon = model(batch)
                loss = loss_fn(recon, batch)
                loss.backward()
                opt.step()
            if epoch % progress_step == 0:
                context.progress(
                    (epoch + 1) / self._epochs,
                    f"training autoencoder (epoch {epoch + 1}/{self._epochs})",
                )

        # Training reconstruction errors → threshold (mean + k*std).
        model.eval()
        with torch.no_grad():
            recon = model(x)
            per_window = torch.mean((recon - x) ** 2, dim=1).cpu().numpy()
        thr = float(per_window.mean() + _THRESHOLD_K * per_window.std())
        if not np.isfinite(thr) or thr <= 0.0:
            thr = float(per_window.max()) if per_window.size else 1.0
        self._model = model
        self._threshold = thr

        context.progress(1.0, "autoencoder trained")
        return FitResult(
            summary=(
                f"trained AE over {matrix.shape[0]} sub-window(s); anomaly threshold {thr:.5g}"
            ),
            n_windows=int(matrix.shape[0]),
            meta={"threshold": thr, "epochs": self._epochs, "bins": _SPECTRAL_BINS},
        )

    def serialize_state(self) -> bytes | None:
        """``torch.save`` the AE state_dict + threshold + norm stats → bytes.

        Layout: a length-prefixed JSON header (threshold + normalization
        stats + shapes) followed by the raw ``torch.save`` blob of the
        state_dict, so ``load_state`` can rebuild the network and restore both
        the weights and the inference-time normalization.
        """
        if (
            self._model is None
            or self._threshold is None
            or self._feat_mu is None
            or self._feat_sigma is None
        ):
            return None
        import torch

        header = json.dumps(
            {
                "threshold": self._threshold,
                "feat_mu": self._feat_mu.tolist(),
                "feat_sigma": self._feat_sigma.tolist(),
                "bins": _SPECTRAL_BINS,
                "hidden": _HIDDEN,
                "latent": _LATENT,
            }
        ).encode("utf-8")
        buf = io.BytesIO()
        torch.save(self._model.state_dict(), buf)
        body = buf.getvalue()
        return len(header).to_bytes(4, "big") + header + body

    def load_state(self, data: bytes) -> None:
        import torch

        header_len = int.from_bytes(data[:4], "big")
        header = json.loads(data[4 : 4 + header_len].decode("utf-8"))
        body = data[4 + header_len :]
        self._threshold = float(header["threshold"])
        self._feat_mu = np.asarray(header["feat_mu"], dtype=np.float32)
        self._feat_sigma = np.maximum(np.asarray(header["feat_sigma"], dtype=np.float32), _EPS)
        model = self._make_model(torch)
        # weights_only=True (the torch 2.6+ default; pinned explicitly here) —
        # the blob is a plain state_dict of tensors, never arbitrary pickled
        # objects, so the secure loader suffices and protects older torch too.
        state = torch.load(io.BytesIO(body), map_location=self._device, weights_only=True)
        model.load_state_dict(state)
        model.eval()
        self._model = model

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------
    def infer(self, context: InferContext) -> list[AIAnnotation]:
        if self._model is None or self._threshold is None or self._feat_mu is None:
            raise RuntimeError("AutoencoderAnomaly.infer before fit/load_state")
        if context.n_samples == 0:
            return []
        import torch

        arr = _primary(context.samples_by_component)
        fs = context.fs
        n_sub = max(2, int(_SUBWINDOW_SECONDS * fs))
        n_hop = max(1, int(_HOP_SECONDS * fs))
        matrix, starts = _build_feature_matrix(arr, fs, n_sub, n_hop)
        if matrix.shape[0] == 0:
            return []

        z = (matrix - self._feat_mu) / self._feat_sigma
        with torch.no_grad():
            x = torch.from_numpy(z.astype(np.float32)).to(self._device)
            recon = self._model(x)
            errors = torch.mean((recon - x) ** 2, dim=1).cpu().numpy().astype(np.float64)

        thr = float(self._threshold)
        # Curve time base: each error is anchored to the CENTRE of its
        # sub-window for a fair time mapping.
        centre_offsets = (starts + n_sub / 2.0) / fs  # seconds from window start
        t0 = context.t_start + float(centre_offsets[0])
        dt = float(n_hop / fs)

        annotations = self._segments_from_errors(
            errors=errors,
            centre_offsets=centre_offsets,
            n_sub=n_sub,
            context=context,
            thr=thr,
            t0_posix=float(t0.timestamp),
            dt=dt,
        )
        return annotations

    def release(self) -> None:
        self._model = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _segments_from_errors(
        self,
        *,
        errors: np.ndarray,
        centre_offsets: np.ndarray,
        n_sub: int,
        context: InferContext,
        thr: float,
        t0_posix: float,
        dt: float,
    ) -> list[AIAnnotation]:
        """Group threshold-exceeding error points into span annotations."""
        nslc = context.nslc_by_component.get("Z") or next(iter(context.nslc_by_component.values()))
        window_t_end = context.t_start + context.n_samples / context.fs
        meta_curve = _decimate_recon_curve(errors, t0_posix, dt, thr)
        peak_err = float(errors.max()) if errors.size else 0.0
        norm = max(peak_err, thr, _EPS)

        annotations: list[AIAnnotation] = []
        above = errors >= thr
        i = 0
        n = errors.size
        half_sub_s = (n_sub / 2.0) / context.fs
        while i < n:
            if not above[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and above[j + 1]:
                j += 1
            # Region [i..j] of exceeding sub-windows → a segment spanning from
            # the first sub-window's start to the last sub-window's end.
            seg_start = context.t_start + (float(centre_offsets[i]) - half_sub_s)
            seg_end = context.t_start + (float(centre_offsets[j]) + half_sub_s)
            region_peak = float(errors[i : j + 1].max())
            annotations.append(
                AIAnnotation(
                    device=context.device,
                    nslc=nslc,
                    kind=self.kind,
                    phase="anomaly",
                    t=seg_start,
                    t_end=seg_end,
                    score=min(1.0, region_peak / norm),
                    model_name="autoencoder_anomaly",
                    model_weights="fitted",
                    window_t_start=context.t_start,
                    window_t_end=window_t_end,
                    meta={
                        "phase": "anomaly",
                        "peak_recon_error": region_peak,
                        "recon_threshold": thr,
                        **meta_curve,
                    },
                )
            )
            i = j + 1
        return annotations


def _decimate_recon_curve(
    errors: np.ndarray, t0_posix: float, dt: float, threshold: float
) -> dict[str, object]:
    """Decimate the recon-error curve to a JSON-friendly meta payload.

    Stores ``recon_t0`` (POSIX epoch of sample 0), ``recon_dt`` (s per point),
    ``recon_err`` (≤ ~300 points) and ``recon_threshold`` for the detail pane.
    """
    if errors.size == 0:
        return {}
    step = max(1, errors.size // _RECON_CURVE_POINTS)
    return {
        "recon_t0": float(t0_posix),
        "recon_dt": float(dt * step),
        "recon_err": errors[::step].astype(np.float64).tolist(),
        "recon_threshold": float(threshold),
    }


def _primary(samples_by_component: dict[str, np.ndarray]) -> np.ndarray:
    """The primary component samples (Z if present, else the first)."""
    arr = samples_by_component.get("Z")
    if arr is None:
        arr = next(iter(samples_by_component.values()))
    return np.asarray(arr, dtype=np.float64)
