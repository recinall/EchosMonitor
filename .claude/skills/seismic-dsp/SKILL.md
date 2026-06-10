---
name: seismic-dsp
description: Locked DSP conventions for live seismic streaming — Welch PSD parameters, rolling spectrogram sizing, causal-only live filtering, STA/LTA warm-up, detrend rules. Consult before changing anything in dsp/ (psd.py, spectrogram.py, stages.py, chain.py) or adding a new DSP stage; code comments reference this skill by name.
---

# Seismic DSP conventions

These parameters are LOCKED — changing them silently changes science output.

## Live chains are causal, streaming, stateful
- Forward-only filtering (`lfilter` + carried `zi`); `zerophase` is forced
  off in live chains (warn-log). `zi` initialised to steady state scaled by
  the first sample (kills the warm-up transient).
- No `taper` stage in live chains (per-packet taper = discontinuity every
  packet) — the factory rejects it.
- `detrend kind="constant"` (recursive mean, ~30 s track) for live;
  `linear` is per-buffer least squares → discouraged live, warned once per
  stream per session at chain install.
- Stages never mutate input arrays; same output streaming vs one-shot
  (modulo IIR warm-up window) is the contract tests assert.

## Welch PSD (dsp/psd.py)
- 8 s segments, 50 % overlap, Hann, linear detrend, density scaling,
  one-sided. `nperseg` clamped to input length. dB via floor 1e-30.

## Rolling spectrogram (dsp/spectrogram.py)
- Window = 2 s × fs (floor 64 samples), 50 % overlap, Hann, power
  normalised by Σw². One column per `nperseg − noverlap` new samples; the
  tail is buffered for cross-packet continuity. Reset on fs change
  (chain hot-reload may decimate).

## STA/LTA
- Recursive (obspy `recursive_sta_lta`); windows rounded as
  `nsta = max(1, round(sta_s·fs))`, `nlta = max(nsta+1, round(lta_s·fs))`.
- Detection suppressed until the estimator has seen one full LTA window
  (warm-up guard) — spurious giant ratios otherwise.
- One open trigger per stream; open marker emitted once (`t_off=None`),
  finalised by the packet that crosses `off_thr`. Recomputing a ratio over
  an archive window needs ≥2×lta_s of extra pre-roll rendered off-screen.

## Decimation
- IIR anti-aliased, factor 2–16, tail-carried for sample-accurate packet
  joins. Downstream consumers (spectrogram, plot buffers) must be re-sized
  to `fs_out` on install/hot-reload.
