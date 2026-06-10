---
name: hvsr-array
description: Design rules for the multi-device (array) HVSR feature — what is scientifically honest to compute with N independent Echos 3C stations, how to extend the existing single-station HvsrEngine/HvsrAccumulator, windowing across devices, position-aware presentation, and reporting. ALWAYS consult before touching core/hvsr_array.py, the HVSR widget's multi-device mode, the map f0 overlay, or the multi-station report.
---

# Multi-device HVSR

## Scientific scope (do not overreach)

Each Echos device is an independent 3C station. With N of them you can
honestly do **synchronous per-station HVSR**: N independent H/V curves
computed over the same time interval, then COMPARED spatially. hvsrpy owns
all per-station physics (H/V ratio, Konno-Ohmachi, horizontal combination,
Cox-2020 rejection, SESAME) — never re-implement, never average curves
ACROSS stations into one "array curve" (f0 varies with subsurface; a
cross-station mean is not a defined quantity). True array methods
(SPAC, FK, dispersion) are a separate research-grade milestone — refuse to
improvise them; if requested, plan them explicitly with literature first.

What the feature delivers:
- N H/V curves overlaid (one colour per device) + faint per-window curves
  per device on demand.
- Per-device results table: f0 ± σ, A0, T0 = 1/f0, windows valid/total,
  SESAME reliability/clarity verdicts.
- Map overlay (M4): markers coloured/sized by f0 (or A0) → spatial
  variation of site response. Distances matrix from `core/positions.py`.
- Multi-station report: one section per station + a comparison page with
  geometry; the per-station pages reuse the existing report renderer.

## Architecture

Extend, don't rewrite:
- `HvsrAccumulator` stays single-station and pure. `core/hvsr_array.py`
  holds an `ArrayMeasurement` = {device → accumulator} + shared settings +
  geometry snapshot.
- One worker thread, one compute request per recompute cycle that runs the
  N `accumulator.snapshot().compute()` calls serially (they are seconds-
  scale; N is small). Same skeleton as `HvsrEngine` (skill:
  qt-worker-threading): pending≤1, skip-with-throttled-log under load
  (rule 11), bounded join on stop.
- Result type: `ArrayHvsrResult = {device: HvsrResult} + geometry +
  settings` — frozen, GUI-facing, no hvsrpy objects (the existing boundary
  rule).

## Windowing across devices (M5-A decision)

Two valid modes — implement per-device-independent first, it degrades
gracefully:
1. **Independent windows** (default): each device accumulates its own
   gap-free windows; devices with dropouts just have fewer windows. Honest
   and robust; curves remain comparable because the interval and settings
   are shared.
2. **Common windows** (optional toggle): a window is accepted only when ALL
   selected devices have gap-free coverage of the same span (the
   `slice_archive_windows` gate generalised to N devices). Stricter
   comparability; throughput collapses if one device is flaky — surface the
   per-device rejection reason.

Same-fs is NOT required across devices (each accumulator checks internal fs
consistency only). Same settings (window length, band, smoothing, rejection)
ARE required across the array — one settings panel drives all.

## Counts vs physical units

H/V cancels the response per station, so counts are valid per station given
identical Z/N/E sensors (the existing `responses_identical` honesty layer —
run it PER DEVICE and surface each verdict). Cross-station comparison of f0
needs no response removal; cross-station comparison of A0 amplitudes is
response-sensitive — annotate it as such in UI and report.

## Live + archive

Both modes mirror the single-station flows: live pulls `read_recent` per
device on the timer tick; archive slices per device via `ArchiveReader`
rooted at the SESSION path (rule 14). Reuse `slice_archive_windows`
unchanged per device.
