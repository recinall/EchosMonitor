# Changelog

All notable changes to EchosMonitor are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is
derived from the git tag (`v0.1.0` → `0.1.0`) by hatch-vcs; see ROADMAP.md for the
milestone plan and decision log.

## [Unreleased]

### Added
- **M7-A** — Release versioning: the package version is now derived from the git
  tag via hatch-vcs (with an `importlib.metadata` → generated `_version.py` →
  `0.0.0+dev` fallback). The main-window title now shows the version, and the
  application/window icon is wired from a packaged resource.

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

### Removed
- **M0:** the entire AI subsystem (agents/seisbench/torch/phasenet) and the
  generic-dashboard branding; the package was renamed to `echosmonitor`.

[Unreleased]: https://github.com/recinall/EchosMonitor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/recinall/EchosMonitor/releases/tag/v0.1.0
