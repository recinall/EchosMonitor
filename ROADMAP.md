# ROADMAP.md — EchosMonitor refactor

Source: generic seedlink-dashboard → target: Echos-specific monitor
(`firmware_seedlink` nodes). Rules live in CLAUDE.md; this file is the plan
and the living state. Check boxes as stages land; record decisions inline.

Milestones are ordered by dependency. Each stage should be one reviewable
change with a green gate (`uv run pytest && ruff && mypy`).

---

## M0 — Rebrand + AI removal (foundation)

Goal: the repo compiles, tests pass, and contains zero AI code (rule 12).

Audit 2026-06-10 (docs/AUDIT.md): baseline gate is NOT green — mypy has 3
`unused-ignore` errors (`core/streaming_engine.py:739,1940,2000`), there is
**no `tests/` directory at all** (nothing to prune — C re-scoped to
*create* the scaffold), and `docs/POSTMORTEMS.md` was not carried over even
though code comments cite its entries. Order decided: **B before A** (see
decision log).

- [x] **B. Remove AI**: delete `ai/`, `core/ai_engine.py`, `ai_panel.py`,
      engage dialogs, `AiConfig` + `persist_on_detection`, `ai`/`remote`
      extras, `ai` pytest marker, `record_ai_detection`,
      `attach_event_persister`/`EventPersister` (no non-AI consumer — remove;
      keep the `events` DDL migration as a no-op stub so schema_version
      history stays linear). Strip AI wiring from `main_window.py`,
      `default.yaml`, README.
      Audit additions (complete map in docs/AUDIT.md §2):
  - [x] also delete `storage/event_persister.py` and the events DAO surface:
        `Event` dataclass + `record_event`/`events_for_detection`/
        `recent_events`/`_row_to_event` (`storage/dao.py:50, 505–608` —
        AI-only callers, rule 12); in `storage/db.py`, dropping the
        `_EVENTS_DDL` body must include its concatenation into
        `_CREATE_SCHEMA_SQL` at `db.py:180`.
  - [x] also remove the Archive tab's AI surface — **not caught by the
        acceptance grep**: `aiRequested` signal (`archive_tab.py:166`),
        `_ai_button` "Run AI agent on this window" + wiring
        (`archive_tab.py:314–315,326,336,432,629,646`), and in
        `main_window.py` the connect at `:494` plus the whole
        `_handoff_archive_to_ai` method (`:1653–…`).
  - [x] also remove `PersistOnDetectionConfig` (`config/schema.py:276–307`,
        a separate class from `AiConfig`), the `AiConfig` re-export in
        `config/__init__.py:7,27`, the seisbench/torch mypy override
        (`pyproject.toml:87–90`), the "with AI" project description
        (`pyproject.toml:4`), and the `ai:` section in **both**
        `default.yaml` copies (the packaged `src/.../config/default.yaml`
        is the one actually loaded, `loader.py:38`).
  - [x] loader strips a legacy top-level `ai:` key with a one-time warning —
        schema is `extra="forbid"` (`config/schema.py:26`), so old user
        configs would otherwise fail validation.
  - [x] verified safe: STA/LTA detection path (`dao.record_detection`,
        `detectionRecorded`) is independent and untouched.
  - [x] discovered during removal (audit had under-scoped these — the
        widgets were NOT purely kind-generic): `detection_detail.py` AI
        rendering (P/S pick-probability curves, autoencoder recon-error
        curve, EQTransformer detection curve, −272 lines),
        `marker_style.py` phase-colour palette + `marker_color` and the
        `phase` parameter threaded through trace_plot/spectrogram/
        live_tabs marker APIs, `detection_table.py` Kind filter combo +
        AI-kind row tint, and `StreamingEngine.live_streams()` (orphaned,
        only engage dialogs called it) — all removed per rule 12.
  - [x] fix the two surviving `unused-ignore` mypy errors
        (`streaming_engine.py:1940,2000`; the third dies with the
        event-persister block) so the gate is green from this commit.
- [x] **A. Rename** package `seedlink_dashboard` → `echosmonitor`; entry point
      `echosmonitor`; update pyproject, imports, QSettings org/app keys
      (decided: reset, log once — see decision log), platformdirs app name
      (`echosmonitor`). Full 13-surface checklist in docs/AUDIT.md §2; the
      non-import surfaces are:
  - [x] QSettings org/app `"SeedLinkDashboard"` (`gui/main_window.py:86–87`,
        `gui/widgets/station_browser.py:92–93`).
  - [x] platformdirs data dir `("seedlink_dashboard", "SeedTiLa")` in
        `streaming_engine.py:1689,1900`; config dir `"seedlink-dashboard"`
        in `config/loader.py:27,55`. No data migration (decision log).
  - [x] distribution name (`pyproject.toml:2`) + `version("seedlink-dashboard")`
        lookups (`__init__.py:8`, `streaming_engine.py:1869`); entry point
        (`pyproject.toml:52`); hatch packages + `default.yaml` force-include
        (`pyproject.toml:59,62`); `importlib.resources` anchor
        (`loader.py:38`).
  - [x] CLI `prog` (`__main__.py:34`), structlog `app=` binding
        (`utils/logging.py:99`), exception base `SeedLinkDashboardError`
        (`core/exceptions.py:11` + subclasses), UI strings
        (`main_window.py:246,1043,1112–1113`, `first_run_wizard.py:185,190,564`,
        `hvsr_report.py:329`), path docstrings (`loader.py:6`, `schema.py:34`).
  - [x] verified 2026-06-10: acceptance grep empty (only the deliberate
        `_LEGACY_*` constants remain); ruff + mypy green;
        `uv run echosmonitor --version` works; offscreen smoke passed with
        both one-time notices observed (`legacy_config_ignored`,
        `qsettings_reset_after_rename`). Reviewed: code-reviewer approve.
- [x] **C. Gate green + tests** (re-scoped twice: the original project at
      `~/Dati/Sources/SeedTiLa` still had the FULL suite — 116 files /
      24k lines — so it was carried over instead of writing a scaffold):
  - [x] carry over `tests/` from SeedTiLa: package renamed, 5 wholly-AI
        test files + `tests/ai/` deleted, ~30 files adapted (AiConfig
        fixtures, phase-less marker APIs, kind-filter removal, events
        table) with non-AI assertions kept at full strength; final gate
        **737 passed, 5 perf-deselected**, ruff src+tests clean,
        mypy --strict clean — all without torch.
  - [x] new regression tests for the M0 decisions: loader legacy `ai:`
        strip (×3), `legacy_config_ignored` (×2), v2→v3 no-op migration
        incl. detections survival, fresh-DB-has-no-events,
        `qsettings_reset_after_rename` (×3), no-kind-filter contract (×2),
        amber-marker/no-phase contract (×2).
  - [x] test-guardian found+fixed a suite isolation bug: conftest
        redirected only IniFormat QSettings — NativeFormat (the default)
        was writing to the REAL `~/.config/EchosMonitor` during tests.
  - [x] gate runnable via plain `uv sync`: dev tools moved from the
        optional extra to PEP-735 `[dependency-groups]` (uv installs the
        `dev` group by default).
  - [x] `docs/POSTMORTEMS.md` carried over verbatim (776 lines) with a
        provenance header; `docs/MANUAL_TESTS.md` carried over with AI
        sections removed + renamed (a `utils/docs.py` test depends on it).

Acceptance: `grep -ri "seisbench\|torch\|phasenet\|ai_engine\|AiConfig" src tests` → empty.

## M1 — Echos device management

Goal: list Echos devices and configure them completely — client side (how the
app connects) AND server side (the firmware's own config) — from one dialog.

- [ ] **A. REST client** `core/echos_api.py` (skill: `echos-rest-api`).
      Typed async httpx client: status, config get/post, network config,
      seedlink status/clients/config (hot-reload 202 + `restart-status`
      poll), disconnect client, calibration (full sweep + status poll),
      stationxml, OTA status, auth password change, reboot. Basic Auth,
      timeout-bounded, 429/Retry-After backoff, never logs credentials.
      Unit tests against an `httpx.MockTransport` fake of the firmware.
- [ ] **B. Credentials store** (keyring with file fallback, rule 15) +
      device schema extension: `echos: {http_port: 80, position_override,
      poll_interval_s}`.
- [ ] **C. Status poller** `core/echos_status.py` on a worker QThread
      (pattern: InfoWorker). Feeds DevicePanel columns: firmware version,
      uptime, clients connected, ring usage, GNSS lock, calibration state.
- [ ] **D. Device dialog**: tabs *Connection* (client-side: name, host,
      SeedLink port, selectors auto-derived from StationXML channels),
      *Acquisition* (server-side: OSR, per-channel gains, emit_hn1),
      *SeedLink server* (port, ring size, auth gate, record size, StationXML
      profile — writes via hot-reload with progress UI), *Network* (WiFi
      STA/AP), *Maintenance* (calibration trigger + progress, OTA status,
      password rotation, reboot). Every server write = read-modify-write with
      confirmation; surface lockout state honestly.

Acceptance: against a fake firmware server, a full round-trip edit of
acquisition + seedlink config works, including the simulated 7-step restart.

## M2 — Session control (no autostart)

Goal: rules 13–14 implemented end to end.

- [ ] **A. Engine**: remove autostart from `MainWindow` — single site,
      `main_window.py:432–434` (`if self._config.devices: self._engine.start()`);
      split per-device lifecycle into `start_monitoring(name)` /
      `start_recording(name)` / `stop(name)` — note the engine API is
      currently **global-only** (`streaming_engine.py:574–622`) though
      per-device machinery exists internally (`_start_device` `:1051–1118`),
      so this is an engine-API change, not just UI; archive writers are
      created only on Recording (today they are config-driven at
      `_start_device` time via `archive.enabled`, `:1116–1117`).
- [ ] **B. Session model** `core/session.py` + `storage` sessions index:
      project name (sanitised, injectivity-checked), started/ended, devices,
      archive path `<root>/<project>/<device>/<SDS…>`. New-session dialog
      (name + which devices record). Audit: a `sessions` table **already
      exists** (`storage/db.py:52–58`: started_at, ended_at, host, version,
      config_hash) — extend it (project_name, device membership; schema v4
      migration) rather than create it. Reuse `sanitize_device_name` +
      injectivity guard (`storage/sds.py:43–88`, `config/schema.py:388–416`)
      for project names (rule 14). Funnel the archive-root resolution sites
      (`_resolve_archive_root` `:1888–1900`, `_resolve_db_root` `:1680–1689`,
      reader snapshot `core/archive_detail_loader.py:322`) through one
      session-rooted resolver.
- [ ] **C. UI**: global Session toolbar (project name, ▶ Monitor, ⏺ Record,
      ⏹ Stop, elapsed, bytes written) + per-device state badges
      Idle/Monitoring/Recording. Crash-recovery: an unclosed session is
      closed-as-dirty on next launch (log + DB flag).

Acceptance: launch → nothing connects; Monitor shows live traces with zero
disk writes; Record creates `<project>/` SDS tree; Stop closes session row.

## M3 — Archive: sessions browser + missing features

Goal: the Archive tab is fully functional and session-centric.

Audit precision on "not completely functional": the tab is further along
than assumed — `archive_extent`/`archive_coverage` (`storage/dao.py:653–717`)
are **already wired** to a working `CoverageStrip` (`archive_tab.py:111–158`),
and the 3C view, spectrogram, unit switching (Counts/Vel/Acc/Disp via the
deconvolution worker) and measurement cursors all exist. The real gaps are:
no session concept in the UI, no trace-window MiniSEED/CSV export (only
HVSR CSV/JSON exist), no PNG export, re-indexer unbuilt (`parse_sds_path`
at `storage/sds.py:130–168` has zero callers).

- [ ] **A. Session browser**: list sessions by name/date (search + date
      filter), per-session device/stream tree with coverage strips
      (extent/coverage DAO + strip widget already exist — re-scope them
      per-session).
- [ ] **B. Window viewing**: verify + polish the static 3C view +
      spectrogram for any session/interval (mostly implemented; remaining:
      zoom/pan ergonomics, unit switching with gaps, export PNG).
- [ ] **C. Exports**: per-interval MiniSEED export, CSV of a trace window.
- [ ] **D. Re-indexer**: rebuild the DB from the SDS tree
      (`parse_sds_path` exists) for archives copied from another machine.
- [ ] **E. Hand-offs**: Archive → HVSR keeps working with the session-rooted
      reader.

## M4 — Map tab

Goal: a Map tab showing each device's position and live state.

- [ ] **A. Position resolver** `core/positions.py`: StationXML lat/lon/elev
      via the M1 client, manual override wins, cached, refreshed on demand
      (rule 16).
- [ ] **B. Map widget**: decision recorded here → tiles (QtWebEngine/Leaflet,
      offline-capable?) vs pyqtgraph scatter with background image. Markers:
      device name, state colour (Idle/Monitoring/Recording/Error), click →
      select device in panel. Inter-device distance readout (needed by M5).
- [ ] **C. HVSR/M5 hooks**: expose station geometry (distances matrix).

## M5 — Multi-device HVSR

Goal: run HVSR over N Echos devices simultaneously, position-aware.

Honest scope (skill: `hvsr-array`): synchronous per-station HVSR — each
device gets its own accumulator/curve (hvsrpy owns the physics, never
re-implemented); the array layer adds comparison and spatial context. True
array methods (SPAC/FK) are explicitly OUT of scope unless re-planned.

- [ ] **A. `core/hvsr_array.py`**: drive N `HvsrAccumulator`s from N devices'
      ring buffers on the existing worker pattern; common time windows
      (windows accepted only when all selected devices have gap-free
      coverage — or per-device independent windows; decide, record here).
      Audit: confirmed seams/blockers — `HvsrEngine` holds exactly one
      `_Measurement` (`core/hvsr_engine.py:219`); `HvsrAccumulator` is
      cleanly per-device (`core/hvsr.py:255`), so the array layer is
      N accumulators + orchestration; the widget's
      `three_component_groups()` (`hvsr_widget.py:113–139`) is already
      multi-device-aware; `responses_identical` (`core/hvsr.py:718–745`)
      generalizes per device.
- [ ] **B. UI**: device multi-select; overlay of N H/V curves; per-device f0
      table (f0, σ, A0, SESAME verdicts); map overlay colouring markers by
      f0 (uses M4) — the spatial-variation view.
- [ ] **C. Report**: multi-station PDF/JSON export (extend
      `storage/hvsr_report.py`): one section per station + the comparison
      page with the geometry (positions, distances).
- [ ] **D. Archive mode**: same analysis over a recorded session.

## M6 — Hardening & polish

- [ ] First-run wizard rewritten for Echos (discover device on
      `192.168.4.1` AP / mDNS `*.local`, set admin password, add device).
- [ ] mDNS discovery of Echos nodes on the LAN (optional, zeroconf).
- [ ] Device clock/GNSS health surfaced (PPS lock from status poller).
- [ ] Settings dialog (archive root, theme, display caps).
- [ ] Docs: user manual for the field workflow (deploy → configure →
      record → HVSR → report).

## M7 — Release: Windows / Linux / macOS

Goal: a tagged version produces installable artifacts for the three desktop
platforms from CI, reproducibly.

- [ ] **A. Versioning + changelog**: semver from git tag (the package already
      reads `importlib.metadata`); CHANGELOG.md kept per milestone; window
      title + About dialog show the version.
- [ ] **B. Packaging tool decision** (record in the decision log):
      PyInstaller (one-dir) vs Briefcase. Constraints to verify on all 3 OS:
      PySide6 plugin bundling, obspy data files (e.g. seedlink/StationXML
      schemas), scipy/numpy size, hvsrpy + its transitive matplotlib +
      the IPython workaround pin. Produce a working local build first
      (`scripts/build.{sh,ps1}`), with hidden-imports/spec files versioned.
- [ ] **C. CI matrix** (GitHub Actions): on every PR run the gate on
      ubuntu/windows/macos; on tag `v*` build artifacts:
      Windows → installer (Inno Setup/NSIS) + portable zip;
      Linux → AppImage (preferred) or tar.gz;
      macOS → .dmg. Upload to the GitHub Release.
- [ ] **D. Signing** (open question 6): Windows code signing and macOS
      notarization need certificates/Apple account — ship unsigned first
      with documented Gatekeeper/SmartScreen instructions; wire signing as
      optional CI secrets.
- [ ] **E. Packaged smoke test**: CI launches the built binary headless
      (`QT_QPA_PLATFORM=offscreen`) with `--version` and a minimal
      start/quit, so a broken bundle fails the release, not the user.
- [ ] **F. Runtime sanity in bundle**: platformdirs paths, keyring backend
      availability per OS (fallback path tested), QSettings org/app, log
      file location documented.

Acceptance: pushing tag `v0.1.0` yields three downloadable artifacts that
launch on a clean machine of each OS and complete the M2 happy path
(connect → Monitor → Record → Stop) against a fake/real device.

---

## Decision log

| Date | Decision | Why |
|------|----------|-----|
| 2026-06-10 | M0 order: **B (AI removal) before A (rename)** | Removal shrinks the rename surface (deletes one of three platformdirs sites, `ai_engine.py:593`, plus extras/overrides); deletion is verifiable by the acceptance grep + gate even with no tests. docs/AUDIT.md §4. |
| 2026-06-10 | M0-A QSettings: **reset, log once** — no migration | QSettings stores only window geometry/dock layout/column state (`main_window.py:2084`, `station_browser.py:602,948`); migration code would outweigh the value. |
| 2026-06-10 | M0-A storage paths: **no migration** of old `SeedTiLa` data dirs | Product refactor with no deployed base assumed; M2 re-roots archives per project anyway. Old default was `user_data_dir("seedlink_dashboard","SeedTiLa")/archive` (`streaming_engine.py:1689,1900`). Revisit before M0 ships if field archives exist. |
| 2026-06-10 | M0-B legacy `ai:` key in user configs: **loader strips + warns once** | Schema is `extra="forbid"` (`config/schema.py:26`); without stripping, every pre-existing user config fails validation after `AiConfig` is removed. |
| 2026-06-10 | M0-B events schema: keep `SCHEMA_VERSION = 3`, v2→v3 migration becomes a **no-op stub**; drop `_EVENTS_DDL` body | Keeps old DBs' schema_version history linear (`storage/db.py:31,141–178,239–252`); `detections` DDL stays (STA/LTA writes it). |
| 2026-06-10 | M2-B: **extend** the existing `sessions` table (project_name + device membership, schema v4) instead of creating one | `sessions` already exists at `storage/db.py:52–58`. |
| 2026-06-10 | M0-C: **carry over** the SeedTiLa test suite + POSTMORTEMS.md + MANUAL_TESTS.md instead of writing a scaffold | The audit's "tests not carried over" finding was about THIS repo; the originals exist at `~/Dati/Sources/SeedTiLa` (116 test files, 776-line postmortems). Real regression coverage beats a 4-file scaffold. |
| 2026-06-10 | Dev tools live in PEP-735 `[dependency-groups]`, not an extra | Plain `uv sync` installs the `dev` group by default, making CLAUDE.md's gate commands literally correct. |

## Open questions (resolve before the milestone that needs them)

1. ~~M0-A: migrate old QSettings or reset?~~ **Resolved 2026-06-10: reset,
   log once** (see decision log — QSettings holds only window/layout state).
2. M1: does the deployed firmware support CORS for a desktop client? N/A —
   we call from Python, not a browser; confirm no Origin checks server-side.
3. M2: should Monitoring without Recording still write the metadata DB
   (detections)? Proposed: yes — detections are cheap and useful; waveforms no.
4. M4: tile stack choice (offline requirement? QtWebEngine weight?).
5. M5: common-window vs per-device windows for array HVSR (see M5-A).
6. M7: signing — is a Windows cert / Apple Developer account available, or
   do we ship unsigned with documented bypass instructions?
