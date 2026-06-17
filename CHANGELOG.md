# Changelog

All notable changes to EchosMonitor are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is
derived from the git tag (`v0.1.0` → `0.1.0`) by hatch-vcs; see ROADMAP.md for the
milestone plan and decision log.

## [Unreleased]

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

[Unreleased]: https://github.com/recinall/EchosMonitor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/recinall/EchosMonitor/releases/tag/v0.1.0
