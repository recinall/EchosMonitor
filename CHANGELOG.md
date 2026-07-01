# Changelog

All notable changes to EchosMonitor are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is
derived from the git tag (`v0.1.0` → `0.1.0`) by hatch-vcs; see ROADMAP.md for the
milestone plan and decision log.

## [Unreleased]

## [0.1.5] — 2026-07-01

### Fixed
- **Recurring multi-second overlaps in the SDS archive (reconnect replays).**
  On a WiFi/AP loss the SeedLink connection drops; on reconnect the Echos
  device re-streams its ring buffer — data already written — and obspy delivers
  it as a backward-timestamped trace. The append-only writer could not
  deduplicate, so it persisted these as multi-second overlaps (the deterministic
  ~00:23 and ~12:23 UTC events, initially misread as a device clock resync, plus
  any post-reconnect replay). The overlapping samples are byte-identical to the
  data already on disk (cross-correlation ≈ 1.0). The streaming engine now keeps
  a per-stream frontier and, for a backward step ≥ 3 s, **confirms the
  overlapping samples match what the ring buffer already holds** before dropping
  the full replay or trimming a partial one — ahead of the ring buffer, DSP, gap
  detector and writer. A large backward step whose samples do **not** match is a
  genuine clock reset carrying new data: it is kept and handled by the existing
  clock-jump path, so no live data can ever be silently dropped. (See
  docs/POSTMORTEMS.md, 2026-07-01.)

## [0.1.4] — 2026-06-19

### Fixed
- **HVSR worked on Linux/macOS but was dead on Windows (critical).** The v0.1.3
  off-process HVSR child crashed on every compute on real Windows with
  `cannot create weak reference to 'NoneType' object`, and the packaged
  `--check` failed — yet CI passed, so the broken bundle shipped. Cause: a
  windowed (`console=False`) PyInstaller **spawn** child has `sys.stdout is
  None`, and the child's structlog used the default `PrintLogger` (writes to
  `sys.stdout`), so the first compute log line blew up before numba even ran.
  CI runners attach a console, hiding it. The child now gives `None` std
  streams a `devnull` sink and pins structlog to it, so off-process HVSR works
  on Windows again (GIL protection restored). As belt-and-braces, the compute
  client now falls back to an in-process compute if the spawn child cannot run
  at all (degraded — GIL-bound — but HVSR stays functional), forwards the
  child's full traceback over the pipe, and `--check` logs a loud warning when
  it falls back. (See docs/POSTMORTEMS.md, 2026-06-19.)

## [0.1.3] — 2026-06-18

### Fixed
- **HVSR analysis no longer stutters the live stream (GIL fix).** hvsrpy's
  Konno-Ohmachi smoothing is numba-JIT and held the CPython GIL for several
  seconds per re-compute; run on a worker `QThread` it still froze the
  GUI/engine thread **and** the SeedLink worker, so during HVSR analysis the
  live plot and the SeedLink data path stalled (and the stall watchdog cried
  wolf). The compute now runs in a persistent **spawn subprocess**
  (`core/hvsr_compute.py`) for both the single-device and multi-device (array)
  engines, so the worker thread only blocks on a pipe `poll` (GIL released)
  while numba runs out-of-process — the GUI render and SeedLink keep
  scheduling. As a bonus the compute is now genuinely **interruptible**: a stop
  `terminate()`s the child, where in-process numba could only be abandoned
  mid-JIT. The packaged `--check` smoke round-trips one real subprocess compute
  so a broken frozen bundle fails CI (same discipline as the obspy-metadata
  smoke).
- **Stall watchdog forgives in-process GIL starvation (false stalls).** A
  multi-second GIL-holding compute on the engine thread froze packet processing
  and the stall scan together, so the next scan measured a false >5 s gap and
  logged `seedlink_stream_stalled` for a stream that never went silent.
  `_scan_stalls` now cross-checks its own scheduling delay and rebases every
  stream's liveness clock (logging `stall_scan_starved`) when the engine thread
  was frozen, while a genuine network silence is still flagged.

## [0.1.2] — 2026-06-17

### Fixed
- **Packaged obspy had an empty plugin registry (critical).** The PyInstaller
  bundle omitted obspy's distribution metadata, so obspy discovered **none** of
  its entry-point-resolved IO plugins. In the released v0.1.0/v0.1.1 binaries
  this silently broke everything that touches seismic IO: archive MiniSEED
  reads (`Format "MSEED" is not supported`), StationXML parsing (TypeError),
  **and SeedLink packet decoding** — so live recording received 0 packets and
  the stall watchdog tripped on `expected_interval_s=0.0`. Fixed by collecting
  `copy_metadata("obspy")` in the spec. The packaged `--check` smoke now
  round-trips MiniSEED + StationXML through the plugin registry, so this class
  of bundling regression fails CI instead of the field.

## [0.1.1] — 2026-06-17

Field-test fixes from the v0.1.0 Linux AppImage. From this release, build
artifacts are versioned ``<tag>+g<short-sha>`` (e.g. ``0.1.1+g29d5250``) so
each binary is traceable to its exact commit.

### Fixed
- **Device selectors (Bug 1 + Bug 3):** a device configured with no stream
  selectors connected but subscribed to nothing (obspy "No streams specified")
  and retry-looped forever with zero data. The device dialog now auto-fetches
  the device's **public** StationXML on open and auto-derives the selectors
  (replacing the `*.*.*` placeholder, never user-set ones), with a status line
  so the metadata is visibly retrieved; the worker now stops with a clear error
  instead of an endless silent reconnect when selectors are empty.
- **Streaming stall watchdog (Bug 2):** the REST poll backed off to the slow
  heartbeat purely on `ConnState.CONNECTED`, so a CONNECTED-but-silent stream
  was invisible until obspy's 120 s timeout. An engine watchdog now flags a
  silent stream using a **sampling-rate-derived** threshold (`npts/fs × factor`,
  clamped 5–60 s), resumes full-cadence REST polling, and logs
  `seedlink_stream_stalled`/`_resumed` for diagnosis — without forcing a
  reconnect (a transient gap recovers on the same socket with no loss).

### Documentation
- README documents the **macOS 13+** minimum (PySide6 6.11 ships only
  `macosx_13_0` wheels) and the unsigned-artifact bypass per OS.

## [0.1.0] — 2026-06-16

First end-to-end release of EchosMonitor as an Echos-specific monitor for
`firmware_seedlink` nodes (refactored from a generic SeedLink dashboard).
Highlights, by milestone:

### Added
- **M1 — Device management:** typed async Echos REST client (`core/echos_api.py`),
  OS-keyring credential storage with a file fallback, a worker-thread status
  poller, and a full device-configuration dialog (Connection / Acquisition /
  SeedLink server / Network / Maintenance) driving server-side config via the
  firmware's hot-reload restart.
- **M2 — Session control:** no autostart; explicit per-device Idle → Monitoring
  → Recording states; project-named recording sessions as the archive unit, with
  a session toolbar and crash-recovery sweep.
- **M3 — Archive:** session-centric browser (search + date filter, coverage
  strips), static 3C + spectrogram viewing with PNG export, per-interval MiniSEED
  and CSV exports, and an SDS re-indexer for archives copied between machines.
- **M4 — Map tab:** device positions resolved from StationXML / live GNSS with a
  manual override, drawn as a local east/north scatter (state-coloured markers,
  inter-device distances).
- **M5 — Multi-device (array) HVSR:** synchronous per-station HVSR over N Echos
  devices, position-aware, with a multi-station report.
- **M6 — Hardening & polish:** mDNS device discovery, first-run wizard, clock-
  health reporting, settings dialog.
- **M6.5 — Field-test hardening:** archive back-pressure as an in-flight gauge,
  millisecond gap-detector jitter tolerance with grid snapping, a faster MiniSEED
  writer hot path, and a viewport-following satellite basemap on the Map tab.
- **M6.6 — Pre-release refinements:** HVSR horizontals mapped by SEED orientation
  code (not alphabetically), an in-app Log tab, per-session StationXML auto-fetch
  and persistence, and a slow-heartbeat REST poll cadence while SeedLink streams.
- **M7 — Release engineering:** git-tag-driven versioning (hatch-vcs) with the
  version in the window title/About dialog; PyInstaller one-dir desktop bundles;
  a 3-OS GitHub Actions gate (ruff/mypy/pytest on Linux/Windows/macOS) and the
  cross-platform port it exposed (Windows MiniSEED int dtype + binary I/O, fsync,
  QSettings, worker-thread teardown); and a tag-driven release pipeline producing
  a Linux AppImage, a Windows Inno Setup installer + portable zip, and a macOS
  `.dmg`. macOS ships unsigned and its test suite is not yet green (tracked).

### Removed
- **M0:** the entire AI subsystem (agents/seisbench/torch/phasenet) and the
  generic-dashboard branding; the package was renamed to `echosmonitor`.

[Unreleased]: https://github.com/recinall/EchosMonitor/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/recinall/EchosMonitor/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/recinall/EchosMonitor/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/recinall/EchosMonitor/compare/v0.1.2...v0.1.3
[0.1.0]: https://github.com/recinall/EchosMonitor/releases/tag/v0.1.0
