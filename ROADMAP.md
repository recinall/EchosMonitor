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

- [x] **A. REST client** `core/echos_api.py` (skill: `echos-rest-api`).
      Typed async httpx client: status, config get/post, network config,
      seedlink status/clients/config (hot-reload 202 + `restart-status`
      poll), disconnect client, calibration (full sweep + status poll),
      stationxml, OTA status, auth password change, reboot. Basic Auth,
      timeout-bounded, 429/Retry-After backoff, never logs credentials.
      Unit tests against an `httpx.MockTransport` fake of the firmware.
      Landed 2026-06-10 (34 tests, gate 771 green, code-reviewer approve):
  - [x] frozen pydantic models (`extra="ignore"`), closed error set
        `EchosErrorKind` in `core/models.py` + `EchosApiError` hierarchy in
        `core/exceptions.py` (auth_failed / locked_out / unreachable /
        timeout / protocol) — dialog branches on type/kind, never text.
  - [x] lockout: 429 stores a monotonic `Retry-After` deadline; every
        authenticated request fast-fails client-side until expiry (proved
        by a zero-device-traffic test). GETs ≤2 transport retries; writes
        zero. Hot-reload poll tolerates the mid-restart HTTP drop and is
        deadline-bounded (worst case `timeout_s` + one in-flight poll).
  - [x] fake firmware lives in `tests/core/echos_fake.py` for M1-D reuse
        (auth/lockout ladder, 7-step restart sim with fail/hang/drop knobs,
        3-phase calibration, fault injection, per-path request log).
  - [x] deferred to M1-C/D (reviewer notes): consider `retries=0` +
        tolerating transient 5xx for restart-status polls once the wire
        contract is pinned against real firmware.
- [x] **B. Credentials store** (keyring with file fallback, rule 15) +
      device schema extension: `echos: {http_port: 80, position_override,
      poll_interval_s}`. Landed 2026-06-10 (27 tests, gate 798 green,
      code-reviewer findings fixed + regression-tested):
  - [x] `config/credentials.py` `CredentialsStore`: OS keyring primary
        (injectable for tests — the real keyring is never touched), JSON
        file fallback at `user_data_dir/credentials.json` chmod 0600 with
        atomic writes and a loud one-time warning. Read order
        keyring→file; a keyring write purges any stale plaintext copy;
        delete is idempotent across both backends. Keyring errors logged
        by exception TYPE only (backends can echo call args). Lookup key
        = device config `name` (the rule-15 "credentials reference").
        Blocking caveat documented: keyring can block on D-Bus/unlock —
        never call from the GUI thread (rule 1; M1-C/D run it on workers).
  - [x] review blocker fixed: stale `.tmp` from a hard kill used to make
        every future fallback write fail on O_EXCL forever (and could
        strand a secret) — now pre-unlinked, with regression test.
  - [x] schema: optional `DeviceConfig.echos` (None = generic SeedLink
        device, e.g. the public-server examples) with `http_port`,
        `poll_interval_s` (1–3600 s, poller cadence) and rule-16
        `position_override {lat, lon, elev_m}`; `extra="forbid"` keeps
        password-shaped keys out of the YAML. Commented example block in
        both `default.yaml` copies (byte-identical, loader test enforces)
        pinned by `tests/config/test_schema_echos.py`.
- [x] **C. Status poller** `core/echos_status.py` on a worker QThread
      (pattern: InfoWorker). Feeds DevicePanel columns: firmware version,
      uptime, clients connected, ring usage, GNSS lock, calibration state.
      Landed 2026-06-10 (15 tests; gate 813 green; code-reviewer +
      qt-concurrency-auditor both passed after one shared finding fixed):
  - [x] `EchosStatusWorker`: one shared worker; QTimer built inside the
        queued `start()` slot (skill §5); each due device polled with
        `asyncio.run` inside the tick slot (thread never parked — skill
        §4); public GETs only (status/seedlink-status/calibrate-status),
        sequential on one keep-alive connection (ESP32-polite), client
        built with `get_retries=0` and NO credentials → cannot trip the
        lockout. `stop()` is a plain method: lock-guarded in-flight
        (loop, task) + `call_soon_threadsafe(task.cancel)` — the asyncio
        analogue of the socket nudge; pinned by a hanging-transport test
        that only passes if the cancel really lands (rule 7).
  - [x] payloads `EchosPollTarget` / `EchosDeviceSnapshot` (frozen, in
        `core/models.py`), Signal(object) + isinstance guards (rule 4);
        targets re-pushed on every `configChanged` (only devices with an
        `echos:` section are polled). Poller is passive fleet status —
        rule 13 (engine autostart) untouched, M2 owns that.
  - [x] DevicePanel: 5th column "Echos" (`fw · up · cli · ring · GNSS` +
        `cal` only when running/failed; full detail in tooltip); failed
        polls replace stale numbers with `(kind)` in dim amber. The old
        4-column defensive pin consciously updated to 5 columns.
  - [x] review finding fixed + regression-tested: a poll in flight when
        its device is removed used to resurrect the row as a ghost —
        late payloads for unknown rows are now dropped at the receiver.
  - [x] carry-forward re-scoped 2026-06-11 (decision log): the wire
        contract remains fake-pinned; the real-device smoke check is now
        the explicit **M1 closure gate** item at the end of this
        milestone (M1-D's own acceptance is fake-based by design).
- [x] **D. Device dialog**: tabs *Connection* (client-side: name, host,
      SeedLink port, selectors auto-derived from StationXML channels),
      *Acquisition* (server-side: OSR, per-channel gains; emit_hn1 moved
      to the SeedLink tab — decision log), *SeedLink server* (port, ring
      size, auth gate, record size, StationXML profile — writes via
      hot-reload with progress UI), *Network* (WiFi STA/AP), *Maintenance*
      (calibration trigger + progress, OTA status, password rotation,
      reboot). Every server write = read-modify-write with confirmation;
      surface lockout state honestly. Landed 2026-06-11 (35 new tests;
      gate green; code-reviewer + qt-concurrency-auditor findings all
      fixed with regression tests):
  - [x] `core/echos_device_worker.py`: per-dialog request engine on a
        QThread (M1-C pattern: asyncio.run per queued slot, plain-method
        stop() with task-cancel). Aggregate load (configs + OTA + cal +
        StationXML channels via obspy ON the worker); keyring access only
        on the worker thread; password rotation updates the store only
        after the device's 200 (skill ordering, pinned by test).
  - [x] `gui/dialogs/echos_tabs.py`: four tabs, one write endpoint each;
        edits are `model_copy` of the last-loaded baseline; SeedLink
        apply captures the POSTed config for rebaseline/port-sync (live
        widgets can be edited mid-restart — review finding), shows the
        7-step progress bar, and a server port change syncs the
        Connection tab so OK saves the right endpoint. 429 → all server
        writes disabled with a countdown banner.
  - [x] audit findings fixed: teardown latch (late queued calibration
        status can no longer resurrect a worker thread on a closed
        dialog), calibration poll is ping-pong-gated and stops on poll
        failure, join-timeout keeps worker refs (deliberate leak, no
        use-after-free), host/http-port edits drop loaded baselines,
        first-run wizard's credential row hidden (no silent dead-end
        Store button), password constraints mirrored at the button.
  - [x] acceptance met against the fake firmware (per this milestone's
        acceptance line): `test_acquisition_roundtrip_acceptance` +
        `test_seedlink_roundtrip_with_seven_step_restart_acceptance`
        drive the real widgets through the real worker into the fake,
        including the simulated 7-step restart.
  - [ ] known minor gap (M6 wizard/polish): renaming a device orphans
        its stored credential under the old key (writes then fail
        auth_failed until the password is re-stored).

Acceptance: against a fake firmware server, a full round-trip edit of
acquisition + seedlink config works, including the simulated 7-step restart.
**Met 2026-06-11** (see M1-D notes). M1 CLOSURE additionally gates on the
real-device wire-contract smoke check below.

- [x] **M1 closure gate — real-device smoke check.** Done 2026-06-11
      (user-authorized, read-only public GETs, no lockout exposure):
      both echos.local and pihw.local (fw 1aa72cbe, project
      Echos_lite_seedlink) answered identically; the fake-derived
      contract was WRONG on 8/10 endpoints and was fully reconciled —
      models, fake, status poller, dialog tabs and tests now mirror the
      real wire shapes; final typed smoke = 11/11 endpoints validating
      on BOTH devices. **M1 CLOSED.** Notable real-contract facts:
      `/api/status` carries position+PPS (rule 16/M4 can use it) but no
      uptime (SeedLink `uptime_ms` is the proxy); acquisition gains are
      flat `gain_ch0..3` and OSR is a register index; the ring is sized
      in kB; calibration is an 8-step PGA gain ladder, not 3 phases.
      Still UNPINNED (write-gated or unobserved — revisit at first real
      write / M6): restart-status in-progress shape (idle =
      `{"state":"idle","applied":{}}` is pinned; client terminal
      heuristic is provisional), calibration sweep `phase` vocabulary,
      seedlink client entry shape, network-config POST schema (Network
      tab is READ-ONLY until pinned — decision log), and the
      `/api/auth/password` + reboot + disconnect + calibrate/full write
      replies (skill-documented, not yet exercised on hardware).

## M2 — Session control (no autostart)

Goal: rules 13–14 implemented end to end.

- [x] **A. Engine**: remove autostart from `MainWindow` — single site,
      `main_window.py:432–434` (`if self._config.devices: self._engine.start()`);
      split per-device lifecycle into `start_monitoring(name)` /
      `start_recording(name)` / `stop(name)` — note the engine API is
      currently **global-only** (`streaming_engine.py:574–622`) though
      per-device machinery exists internally (`_start_device` `:1051–1118`),
      so this is an engine-API change, not just UI; archive writers are
      created only on Recording (today they are config-driven at
      `_start_device` time via `archive.enabled`, `:1116–1117`).
      *Done 2026-06-11:* `AcquisitionState` (IDLE/MONITORING/RECORDING) in
      `core/models.py`; engine API `start_monitoring`/`start_recording`/
      `stop(name)` + `acquisition_state(name)` + `acquisitionStateChanged`
      signal (int payload, `deviceStateChanged` pattern — M2-C consumes);
      Monitoring↔Recording transitions attach/detach the writer without
      socket churn; hot-reload buckets are state-aware (see decision log);
      tests in `tests/core/test_engine_session_lifecycle.py`.
- [x] **B. Session model** `core/session.py` + `storage` sessions index:
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
      *Done 2026-06-11* (dialog deferred to M2-C — it is pure UI and lands
      with the toolbar): `core/session.py` (SessionInfo, sanitiser alias,
      path grammar) + `storage/sessions.py` (injectivity guard against
      existing project dirs; raw name read from the project's own
      archive.db); schema v4 (project_name, closed_dirty, session_devices)
      with idempotent v3→v4 step; engine `start_session`/`end_session`/
      `active_session` + `sessionChanged` signal; `start_recording`
      requires the active session and joins membership; all archive paths
      funnel through `_session_rooted()`; crash-dirty rows swept on
      session-DB open (`close_dirty_sessions` — M2-C wires the launch
      sweep); tests in `tests/core/test_session.py` +
      `tests/core/test_engine_sessions.py`.
- [x] **C. UI**: global Session toolbar (project name, ▶ Monitor, ⏺ Record,
      ⏹ Stop, elapsed, bytes written) + per-device state badges
      Idle/Monitoring/Recording. Crash-recovery: an unclosed session is
      closed-as-dirty on next launch (log + DB flag).
      *Done 2026-06-11:* `gui/widgets/session_toolbar.py` (Monitor =
      per-device `start_monitoring` over idle devices; Record… = the
      M2-B-deferred `NewSessionDialog` → `start_session`; Stop = global
      `engine.stop()`; status label shows project · elapsed ·
      bytes-this-session via per-member baselines); DevicePanel gained
      the Acq badge column (Idle/Monitoring/● REC); launch sweep
      `sweep_dirty_sessions(resolve_base_archive_root(cfg))` runs
      synchronously in `MainWindow.__init__` before the engine exists.
      Tests: `tests/gui/test_session_toolbar.py` (full acceptance cycle
      end-to-end against a fake server) + panel/MainWindow additions.

Acceptance: launch → nothing connects; Monitor shows live traces with zero
disk writes; Record creates `<project>/` SDS tree; Stop closes session row.
**Met 2026-06-11** — pinned end-to-end by
`test_toolbar_monitor_record_stop_cycle` (+ the M2-A/B engine suites).

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

- [x] **A. Session browser**: list sessions by name/date (search + date
      filter), per-session device/stream tree with coverage strips
      (extent/coverage DAO + strip widget already exist — re-scope them
      per-session). NOTE (M2-B consequence): between sessions
      `engine.archive_root()`/`archive_dao()` expose only the bare base
      root — data recorded in CLOSED sessions lives under
      `<base>/<project>/` and is unreachable by the live readers until
      this browser opens project DBs explicitly (scan project dirs'
      `archive.db`s + the base monitoring index). Detection replay /
      Archive→HVSR hand-offs for closed sessions depend on this.
      *Done 2026-06-11* (gate green; code-reviewer + qt-concurrency-auditor
      on the diff):
  - [x] `storage/sessions.discover_sessions(base_root)`: scans the base
        monitoring index + every project dir's `archive.db` **read-only**
        (`db.connect(read_only=True)`: `mode=ro` + `query_only`, no
        schema/migration — a browse never rewrites a DB, rule 8); per-DB
        errors logged + skipped. Returns `SessionEntry` (SessionRecord +
        session_root + db_path, `core/models.py`) — everything a reader
        needs to reach a CLOSED session with no live engine context.
  - [x] `core/archive_browser_loader.py`: standard worker (skill §1–§2)
        with TWO latest-wins token streams (list vs detail — neither
        supersedes the other); detail builds the per-device 3C station
        tree (`three_component_groups_from_pairs`, the pure core twin of
        the live HVSR grouping) + extent/coverage clipped to the session
        span, opening each session DB read-only and closing it before the
        slot returns. Start→stop→start + stop-during-busy pinned.
  - [x] ArchiveTab rebuilt session-centric: session list (name search +
        date filter, `⚠ dirty` amber, `● open` green), per-session
        device/station tree with CoverageStrips over the session span,
        default interval inside the session's REAL coverage, the main
        coverage strip re-sliced client-side (zero GUI-thread DB reads —
        the old ctor-DAO `archive_extent` calls on the GUI thread are
        gone with the ctor DAO itself).
  - [x] stale-DAO debt rewired: `ArchiveDetailLoader`/`ArchiveWindowLoader`
        take NO constructor DAO; each request snapshots `db_path` (the
        tab's selected `SessionEntry`, else `engine.archive_db_path()` —
        new per-context accessor) and the worker opens it read-only per
        load, closing in `finally` (own-thread connection, the M2-B leak
        note). Missing/corrupt index degrades to the canonical SDS scan,
        never a failure (both pinned).
  - [x] M3-E seam: an Archive→HVSR hand-off stores the browsed session
        root keyed to the device AND interval (±1 s, the prefill's
        second-resolution round-trip); `_run_hvsr_archive` reads that
        root (index-less reader — no cross-thread DAO lifetime) so "Run
        on archive" works for closed sessions; ANY manual re-target
        (other device or other interval) falls back to the live engine
        roots — regression-tested (the device-only keying was a
        code-reviewer major: it silently re-routed later same-device
        manual runs to the stale session).
  - [x] acceptance pinned end-to-end:
        `test_closed_session_waveforms_load_without_active_session`
        (MainWindow + real loaders + real SDS files, engine fully idle).
        Bonus regression caught by it: `_on_archive_window_loaded` had a
        latent `UTCDateTime` NameError — the loaded-path had never been
        driven through the real loader before.
  - [x] review findings fixed + regression-tested: closeEvent severs the
        `sessionChanged`→refresh bridge BEFORE joining the browser thread
        (qt-auditor F1 — `engine.stop()`'s queued emit used to lazily
        RESTART the just-joined thread mid-teardown → Qt abort at exit);
        `_clear_session_detail` invalidates the detail token (F2 — a late
        `detailLoaded` for a vanished session resurrected a ghost tree
        whose Load fell back to the wrong root); discovery polls a
        cooperative `should_stop` between DB opens (F3, rule 7); ro URI
        percent-encoded (paths with `?`/`#`/`%`); probe-failure DAO
        closed explicitly; the per-session "streams indexed, no files"
        empty state re-pinned.
- [x] **B. Window viewing**: verify + polish the static 3C view +
      spectrogram for any session/interval (mostly implemented; remaining:
      zoom/pan ergonomics, unit switching with gaps, export PNG).
      *Done 2026-06-11* (gate 940 green; code-reviewer findings all fixed
      with regression tests; no thread surface → no concurrency audit):
  - [x] zoom/pan ergonomics: mouse drives X only with Y auto-fitting the
        VISIBLE data ("seismologist zoom"); the spectrogram is X-linked
        to the visible trace view; pan/zoom clamped to the loaded window
        ± one span; Stacked↔Overlaid carries the time zoom by a one-shot
        range copy in the toggle (decision log: the hidden view must NOT
        be statically X-linked).
  - [x] unit switching with gaps stays honest per component: gappy
        components are still skipped by decon (FFT response removal
        would smear NaNs — `miniseed-sds` gap discipline), but are now
        labelled "(counts — gaps)" with a status notice; the readout
        reports each component in ITS unit; the overlaid plot's single
        y axis shows the common unit or "mixed units — see stacked
        view" (it used to stay 'counts' forever). Pinned tab-level and
        through the real decon worker.
  - [x] Export PNG: toolbar button (enabled only with a loaded window),
        widget-grab of the traces+spectrogram exactly as shown, default
        filename from device + window start, one-shot GUI-thread write
        (the HVSR report/CSV precedent).
  - [x] review findings fixed + regression-tested: empty/failed re-loads
        no longer leak the previous window's unit labels or rebind the
        loaded-window metadata (requests are committed into the
        `_loaded_*` fields only when their result renders); a component
        absent from a new load is relabelled.
- [x] **C. Exports**: per-interval MiniSEED export, CSV of a trace window.
      *Done 2026-06-11* (gate 956 green; code-reviewer APPROVE +
      qt-concurrency-auditor PASS, all minors folded in):
  - [x] `storage/exports.py`: atomic writers (tmp in same dir → fsync →
        `os.replace`; tmp unlinked on EVERY non-success path — a
        cancelled/failed export never leaves a partial file). MiniSEED
        from the SPLIT window stream → gaps stay separate records,
        samples bit-identical; CSV on one shared 1/fs grid, gaps as
        EMPTY cells (never interpolated), `# key: value` header meta,
        cooperative-stop poll every 50k rows (rule 7).
  - [x] `core/archive_export_worker.py`: new worker on the loader
        skeleton with a decision-logged deviation — exports are a
        SERIAL QUEUE, never latest-wins (each request is an explicit
        save; tokens only route results). Stop flag is shutdown-only and
        re-armed exclusively via the queued clear (auditor: a sync clear
        without a token supersede could resurrect an export queued
        behind a shutdown). Per-request ro DAO; reads from the
        session-scoped root (rule 14) snapshotted at request time.
  - [x] Archive tab: Export MiniSEED…/CSV… beside Load, sharing its
        precondition (selected station + session coverage) — exports
        re-read the SELECTED interval from the archive, no render
        required; default filename from device + interval start.
  - [x] acceptance pinned end-to-end:
        `test_closed_session_export_end_to_end` (MainWindow → real
        worker → obspy-readable 3-channel MiniSEED, engine fully idle)
        + worker/storage suites (gap-split roundtrip, CSV grid + gap
        cells, serial-queue-not-latest-wins, shutdown-during-busy
        leaves no artifact and restarts, mixed-rate CSV refusal).
- [x] **D. Re-indexer**: rebuild the DB from the SDS tree
      (`parse_sds_path` exists) for archives copied from another machine.
      *Done 2026-06-11* (decisions in the log):
  - [x] `storage/reindex.py` (`reindex_session_root`, Qt-free — progress
        and cancel are plain callables, rule 2): walks
        `<session_root>/<device>/<SDS…>` via `parse_sds_path` (device
        segment read from the path above `<year>`, per the skill); spans
        from obspy headonly reads, bytes from `stat` (rule 9); `files`
        rows upserted in place via the new `ArchiveDao.replace_file`
        (overwrites `t_start` — disk truth, unlike the live writer's
        `record_file`); rows whose path no longer exists are pruned;
        per-stream `total_bytes` recomputed as SUM(files.bytes); device
        dirs map onto existing device rows by sanitized raw name (never
        a duplicate). Dirty/foreign files (non-SDS names, unreadable
        MiniSEED, header NSLC ≠ path) are skipped PER FILE, counted,
        never fatal. Idempotent; cancellation leaves a safe partial
        index (re-run converges).
  - [x] schema v5: `sessions.reindexed` flags THE synthesized session
        row (at most one per DB, upserted in place) written only when
        the DB holds no real session rows: span = data extent, project
        name = directory name, membership = device dirs found.
        `list_sessions` reads the column behind a `pragma table_info`
        guard so read-only browses of pre-v5 DBs keep working
        unmigrated (rule 8).
  - [x] `core/archive_reindex_worker.py`: serial-queue worker on the
        M3-C export skeleton (same shutdown-only stop + queued clear
        semantics, same auditor reasoning); progress beats throttled to
        10/s; per-file cooperative stop poll (rule 7). Skill §7 pinned:
        start→stop→start, shutdown-during-busy bounded.
  - [x] Archive tab: "Re-index…" beside Refresh → directory picker
        restricted to direct children of the base root (where discovery
        looks); progress + honest completion report in the browser
        status (kept through the automatic refresh via a one-shot
        notice — the refresh used to erase it, caught by the e2e test);
        "re-indexed" status chip + tooltip on synthesized rows.
  - [x] active-session guard in the main window (rule 4 keeps it out of
        the widget/loader): a target whose `archive.db` resolves to
        `engine.archive_db_path()` is refused loudly — the engine is
        writing that DB. Other roots stay re-indexable mid-session;
        cross-process safety is the app-lifetime QLockFile on the base
        root (M2-C).
  - [x] acceptance pinned end-to-end:
        `test_copied_archive_reindex_then_browse_and_load` (copied SDS
        tree, no DB → real worker → browser lists the re-indexed row →
        waveforms load, engine fully idle) + storage suite (stale-DB
        counts corrected from disk truth incl. foreign-machine paths
        pruned, raw-device-name mapping, idempotency, cancel/converge,
        dirty-files-never-fatal) + worker lifecycle suite.
  - [x] review findings fixed, all regression-tested (majors in the
        decision log): prune by run membership (code-reviewer M1);
        session-start guard against in-flight re-index roots
        (code-reviewer M2 TOCTOU, via `SessionToolbar.set_session_start_guard`);
        late `finished` after closeEvent no longer resurrects the
        just-joined browser thread (qt-auditor BLOCKER — handlers gate
        on `_archive_bridge_severed`); `should_stop` polled in the scan
        and prune phases too (both reviewers, rule 7); device-name
        collisions in foreign DBs resolved deterministically (lowest id,
        warned); device rows never fabricate host/port from the
        re-indexing machine; the completion notice survives an active
        filter; pre-v5 read-only `list_sessions` pinned; non-canonical
        dir names warned at synthesis.
- [x] **E. Hand-offs**: Archive → HVSR keeps working with the session-rooted
      reader.
      *Done 2026-06-11.* The seam itself landed in M3-A
      (`test_hvsr_handoff_carries_session_root` pins root selection keyed
      on device AND interval ±1 s, fallback on any re-target; the slice
      read stays the documented one-shot INLINE read on the calling
      thread — verified, unchanged). Closing the box:
  - [x] end-to-end acceptance `test_closed_session_hvsr_end_to_end`
        (tests/gui/test_archive_hvsr_e2e.py): browse a CLOSED session →
        hand off → click "Run on archive" → a REAL
        `HvsrEngine.start_archive_measurement` (no monkeypatch) recovers
        the injected f0 from the session-rooted archive with the engine
        fully idle throughout (rule 13).
  - [x] gap found+fixed by that test: with the engine idle the HVSR
        widget's combos (populated from LIVE buffers) were empty, so the
        prefill could not select the handed-off station and "Run on
        archive" silently no-opped. `prefill_archive` now remembers the
        handed-off group and `_refresh_devices` merges it into the live
        groups (live entries win), so an archive-only station stays
        selectable across later live refreshes.
  - [x] code-reviewer findings on that fix, both regression-tested: an
        archive-only (merged) station must never enable a LIVE start —
        no buffers means the measurement would wait forever with no
        diagnostic — so Start is gated on live-buffer membership with an
        honest tooltip; and the merged entry is tagged "(archive)" in
        both combos so the persistent hand-off row is distinguishable
        from genuinely live devices.

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
| 2026-06-10 | M1-A: the firmware JSON field names are **defined by the test fake** (`tests/core/echos_fake.py`), derived from the skill; models use `extra="ignore"` | The skill pins endpoints/semantics but not exact JSON bodies, and the real devices must not be probed casually (lockout). Verify field names against real firmware before M1-C relies on them — both `echos_api.py` and the fake carry this caveat in their docstrings. |
| 2026-06-10 | M1-A: a device-reported restart **failure returns** a terminal `RestartStatus(state="failed")` instead of raising | It is domain state the dialog must render step-by-step, not a transport error; the closed `EchosErrorKind` set stays transport-only. |
| 2026-06-11 | M1-D: `emit_hn1` lives on the **SeedLink tab**, not Acquisition as sketched | It is a `/api/seedlink/config` field; tabs group by write endpoint so one Apply = exactly one confirmed read-modify-write (and only the SeedLink Apply triggers the hot-reload restart). |
| 2026-06-11 | M1-D accepted **against the fake firmware**; the real-device wire-contract check is re-scoped to an explicit **M1 closure gate** | The milestone's acceptance line is literally "against a fake firmware server"; the field-name pin against echos.local/pihw.local needs the user (lockout risk — never probe unsolicited) and now has its own unchecked item under M1. |
| 2026-06-11 | M1-D: pytest-qt's `waitSignal` cleanup benignly emits "Timers cannot be stopped from another thread" on cross-thread signals | Do NOT chase this warning text in tests/logs; the real hazard (worker QTimer active at GC) is pinned by `test_shutdown_stops_worker_timer_via_release_barrier` instead. |
| 2026-06-11 | M1 closure: **Network tab is read-only**; the client has NO `set_network_config` | The firmware's POST schema for `/api/network/config` is unverified and the real read shape (known_networks list + AP + NTP) differs wildly from the skill sketch; a guessed write can take a device off the LAN with button-B AP mode as the only recovery. Pin the schema from firmware sources before M6's wizard needs it. |
| 2026-06-11 | M1 closure: POSTable models use `extra="allow"` and round-trip unmodelled fields; read models stay `extra="ignore"` | The real `/api/config` carries ~10 fields beyond OSR/gains (trigger_mode, schedule, seed_metadata…); a full-body read-modify-write that dropped them would silently reset device behaviour. |
| 2026-06-11 | M1 closure: `RestartStatus` terminal heuristic is `state=="done"` OR (`"idle"` + non-empty `applied`); POST 200 (vs 202) = applied-without-restart | The real idle shape is `{"state":"idle","applied":{}}` and the in-progress shape is write-gated; the device note says auth_required hot-applies without restart. Provisional until the first real authenticated apply is observed. |
| 2026-06-11 | M2-A: global `engine.start()` **kept** as "monitor all configured devices" (tests/headless only; GUI never calls it) | ~25 test call sites use it as the boot convenience; its new semantics (no writers ever) keep the rule-13 invariant intact while avoiding a whole-suite rewrite. |
| 2026-06-11 | M2-A: `archive.enabled` no longer creates writers — **only `start_recording` does**; the rest of `archive.*` (root, encoding, fsync, queue) still parameterises the writer | Rule 13: recording is a user action, not a config side effect. `enabled` is now vestigial; M2-B/C decide whether it becomes "default record set" in the new-session dialog or is dropped (schema v4 moment). |
| 2026-06-11 | M2-A: hot-reload `added` bucket registers the device **IDLE** (no autostart); `restart` bucket only recycles devices the user has running (Recording survives with a fresh writer); `reconnect_device` is a no-op on idle devices | Rule 13 applies to runtime adds too. `test_add_device_via_store_starts_worker` consciously rewritten to `…_registers_idle_until_user_starts`. Idle devices still get `_reinstall_chain` so preserved router chains track config. |
| 2026-06-11 | M2-A: detection DAO creation moved from global `start()` to per-device `_start_device` (first detection-capable device to start) | Keeps open question 3's current answer (Monitoring persists detections, rule 8) under the per-device API; on launch with everything idle there is NO archive.db and no recent-detections prefill — M2-B's sessions index restores history. |
| 2026-06-11 | M2-A reviews: `_teardown_archive_writer` drains the device's archive inbox to the writer (FIFO before the blocking `close_all`) and logs residual drops | qt-concurrency-auditor F1 / code-reviewer major 1: the per-device paths (downgrade, `stop(name)`, hot-reload restart) bypassed `stop()`'s global drain and silently lost up to one flush-tick of recorded tail. Regression test `test_downgrade_flushes_inflight_archive_inbox`. Also from review: `_started=False` precedes the IDLE emits (reentrancy), `_on_config_changed` guards on `_started` (stale queued diff), name validation precedes infra boot, Archive-tab empty state points at Recording instead of the vestigial `archive.enabled`. |
| 2026-06-11 | M2-B: **one `archive.db` per session root** (`<base>/<project>/archive.db`); the base-root DB survives as the sessionless monitoring index (detections while no session) | Skill `miniseed-sds` mandates per-session-root DBs; the monitoring index keeps open question 3's interim answer working between sessions. M3-A's session browser scans project dirs' DBs (or the base index) rather than one global DB. |
| 2026-06-11 | M2-B: `start_recording` **requires** an active session and joins it; detections during a session land in the session DB | Rule 14: sessions are the archive unit — no session, no archive writes. The engine swaps its single DAO between contexts (close-then-open, never two live); membership rows are written before the state is announced (rule 8). |
| 2026-06-11 | M2-B: project-name injectivity is checked against **disk** (raw name read from the existing project dir's own archive.db); unverifiable dirs (no DB) are allowed with a loud log | Unlike device names (collision domain = one config, checked at load), project names accumulate over time; the project's DB is the only durable home of the raw name. Files win over the index (rule 8) so an unprovable collision must not block recording. |
| 2026-06-11 | M2-B: New-session dialog deferred to **M2-C** | It is pure UI and shares the toolbar's wiring; B's engine API (`start_session(project, devices)`) is exactly the dialog's contract. |
| 2026-06-11 | M2-B: per-device `archive.root_dir` overrides keep working (`<override>/<project>/<device>/…`) but the project injectivity guard runs only against the **app base root** | The session's `archive.db` (the only durable home of the raw name) lives at the app-base session root; an override dir has no DB to check against, so a guard there could only ever warn-unverified. Multi-root sessions are an edge config; revisit if the field uses them. |
| 2026-06-11 | M2-C: toolbar **Stop = global `engine.stop()`** (session closes + every device Idle), not per-device stops or end_session-only | One bounded parallelised call (rule 7), one unmistakable end state (rule 13). Per-device control stays available via future panel actions; the engine API already supports it. |
| 2026-06-11 | M2-C: crash-recovery sweep runs **synchronously in `MainWindow.__init__`** before the engine exists, gated by a **QLockFile** on the base root | Rule 13 guarantees nothing in THIS process touches the DBs at launch; the lock closes the cross-process hole (a second instance must not dirty-close a session another instance is recording — qt-concurrency-auditor F4). A crashed holder's lock is stale (dead pid) and reclaimed, so the sweep still runs after real crashes. Pre-event-loop bootstrap I/O, per-DB elapsed-logged, OSError-contained. |
| 2026-06-11 | M2-C reviews: tests redirect `platformdirs.user_data_dir` into the per-test tmp (autouse conftest fixture + pin) | Code-reviewer BLOCKER: default-config MainWindow tests ran the launch sweep against the user's REAL `~/.local/share/echosmonitor/archive` (the M0-C QSettings bug class — no damage occurred only because the dir didn't exist yet). Also from review: panel badge slot no longer resurrects removed-device rows (ghost class); toolbar bytes baselines snapshot on first sight in `_refresh` (no lifetime-counter flash); engine `stop()` flips `_started` before its event barrier so a pre-posted config diff can't spawn an orphaned worker mid-teardown (regression-tested). |
| 2026-06-11 | M2-B reviews: session transitions are guarded (`_session_transition` flag + `ExcludeUserInputEvents` on every absorb barrier; config diffs re-queue past the swap); `start_session` absorbs queued DSP-thread detections BEFORE the DAO swap; a failed swap restores the sessionless detection index; `started_at` fetched by row id; base index sweeps crash-dirty rows on open | qt-concurrency-auditor F1/F2/F4/F5 + code-reviewer majors 1–2, minors 4–5 on the M2-B diff: `processEvents()` inside the swap could dispatch a reentrant `start_session`/click and route the old session's queued `flushedFile`/detection events into the NEW project's DB; `list_sessions(limit=1)` provenance could be fooled by a crash-dirty future-dated row. DAO lifetime is now documented as per-context on `archive_dao()`; consumer re-resolution is the contract (stale-reference rewiring lands with M2-C/M3-A). |

| 2026-06-11 | M3-A: browsing opens every DB **read-only** (`mode=ro` + `query_only`, no migration) | A browse must never rewrite an archive as a side effect (rule 8; the `stored_project_name` precedent). The launch sweep already migrates every base-root DB, so the browser never needs write access; a v-old foreign DB that fails the ro query is skipped per-DB, not migrated. |
| 2026-06-11 | M3-A: loaders take **no constructor DAO** — requests carry `db_path`, the worker opens it read-only and closes per request | The M2-B DAO is per-session-context; a captured reference goes stale on the first swap and a worker-thread connection on it leaks (`close()` is per-thread). Per-request open/close on the worker thread kills both, and the index stays a pure accelerator (missing/corrupt → canonical-scan fallback). Same reasoning removed the engine-DAO hand-off into the HVSR thread (`_run_hvsr_archive` is index-less now). |
| 2026-06-11 | M3-A: browser truth = `SessionEntry` (session_root + db_path), **including for the open session**; per-device `archive.root_dir` override archives are not browsable | Discovery can only see base-rooted project dirs (the decision-logged M2-B injectivity scope); routing the open session through the engine instead would make the same window behave differently mid-recording vs after Stop. Revisit with the override-root edge config if the field uses it. |
| 2026-06-11 | M3-A: detection-table history prefill across session DBs stays OPEN (not part of A) | A's acceptance is the waveform browser; the detections table still fills from live sessions only. The browser's discovery layer is the natural substrate when M3 revisits it. |
| 2026-06-11 | M3-A: per-session station trees show only `session_devices` members; a membership-less row (base monitoring index) falls back to ALL devices in that DB | Membership is the rule-14 record of who recorded; the monitoring index has no membership by design and hiding everything would make it look broken. Coverage stays the honest signal either way. |

| 2026-06-11 | M3-B: the hidden trace view (Stacked vs Overlaid) is **never statically X-linked**; the layout toggle copies the range once and re-targets the spectrogram's link to the visible view | pyqtgraph maps linked ranges through each view's pixel geometry; a HIDDEN view's degenerate geometry distorts the range it pushes back (~3 % drift per load, found by `test_x_range_fits_loaded_window`). Pixel-alignment between *visible* overlapping views is intended behavior — the spectrogram-link test asserts span-relative follow, not exact equality. |
| 2026-06-11 | M3-B: gappy components stay in counts on a unit switch (no per-segment decon), but the display says so per component | An FFT response removal smears NaNs across the window and `miniseed-sds` mandates rejecting gapped windows for science; per-segment decon would be new science behavior, out of polish scope. The honesty layer (per-component labels, "(counts — gaps)", overlay "mixed units") removes the silent-mix lie instead. Revisit per-segment decon only with a real field need. |

| 2026-06-11 | M3-C: the export worker is a **serial queue**, not latest-wins; its stop flag is shutdown-only and re-armed ONLY via the queued clear | An export is an explicit "save this file" — a second request cancelling the first is data loss, the inverse of the read loaders' supersede semantics. Without a token supersede, the loaders' synchronous stop-clear idiom is unsafe (it could resurrect an export queued behind a shutdown on the restart path — auditor finding); queue FIFO drains stale requests against the still-set flag before the queued clear lands. The request seam is deliberately uncapped (rule 5 deviation): drop-oldest is wrong for saves, and each request costs one application-modal dialog — click-bounded. |
| 2026-06-11 | M3-C: exports re-read the interval from the archive (split stream → MiniSEED; shared grid → CSV), never the on-screen arrays; mixed-rate groups refuse the CSV grid | The MiniSEED files are the source of truth (rule 8) — re-reading preserves dtype/encoding bit-identically and exports work without rendering; the display pipeline is float64 with NaN gap-breaks, a render not an archive format. A CSV is one grid: components at different rates get a clear refusal, not resampling. |

| 2026-06-11 | M3-E: archive-only hand-off stations are merged into the HVSR widget's combos (tagged "(archive)") but can NEVER start a LIVE measurement | With the engine idle a closed session's station has no live buffers: unmergeable = "Run on archive" silently no-ops (the gap the e2e test exposed); but a merged station passing the live-start gate would make `start_measurement` wait forever for windows that cannot arrive — the same silent-no-op bug on the other path. Live Start is gated on live-buffer membership with an honest tooltip. |
| 2026-06-11 | M3-D: re-indexed sessions get a SYNTHESIZED row (`sessions.reindexed`, schema v5) — span = data extent, name = directory name, membership = device dirs; at most one per DB, never written when real session rows exist | Sessions cannot be reconstructed from the tree, and `sessions.project_name` was the raw name's only durable home — a missing DB loses it irrecoverably; the dir name is the honest fallback and the flag keeps the synthesis visible (browser chip + tooltip) instead of masquerading as a real record. Real rows are the durable session history and must never be shadowed or duplicated. |
| 2026-06-11 | M3-D: `ArchiveDao.replace_file` (a second files upsert that overwrites `t_start`) exists ONLY for the re-indexer; the live writer keeps `record_file`'s t_start-preserving semantics | The live writer's first-write `t_start` is correct for a file it is appending to; the re-indexer's job is the opposite — make the row mirror the file as it exists NOW (rules 8/9). One method serving both would silently pick a side. Per-stream `total_bytes` is recomputed from the corrected rows (packet counters are left alone — packet history is unreconstructable). |
| 2026-06-11 | M3-D: stale `files` rows are pruned by **run membership** (any row this run did not upsert), NOT by disk existence | Code-reviewer major: `files.path` is absolute, so an existence check keeps rows pointing into a same-machine duplicate's ORIGINAL tree (the most common "copied archive") — indexing files outside the session root (rule 14) and folding foreign bytes into `total_bytes` (rule 9) — and keeps stale rows for files this run refused as dirty. A completed run's candidate set is exhaustive for the root, so non-membership is the exact prune predicate; cancelled runs skip the prune entirely. |
| 2026-06-11 | M3-D: the session toolbar consults a main-window **start guard** — a project whose root has a re-index in flight cannot start recording (the inverse of the active-session guard) | Code-reviewer major (TOCTOU): the request-time guard stops re-indexing the active session, but nothing stopped a session from STARTING into a root mid-re-index — the engine's DAO and the re-index DAO would write one archive.db concurrently (rule 8; `refresh_stream_byte_totals` clobbering live `record_packet`, the synthesized-row check racing `start_session`). MainWindow owns both sides: it records the in-flight root and vetoes via `SessionToolbar.set_session_start_guard` with a user-facing reason; cleared on done/failed. Direct engine calls (tests/headless) bypass it knowingly — the UI is the only concurrent path. |
| 2026-06-11 | M3-D: `list_sessions` reads `reindexed` behind a `pragma table_info` guard instead of requiring schema v5 | The M3-A browser opens every DB read-only WITHOUT migration (rule 8); an unconditional `SELECT reindexed` would break browsing every existing pre-v5 archive. The pragma costs one statement per (bounded) listing call. |
| 2026-06-11 | M3-D: re-index targets are restricted to DIRECT CHILDREN of the base archive root; the active session's DB is refused at the main-window seam | Discovery only scans base-rooted project dirs (M3-A decision) — re-indexing anywhere else would "succeed" into invisibility; the picker therefore enforces the same scope. The ACTIVE session's `archive.db` is held open and written by the engine (rule 8) — re-indexing it would race the live writer; the guard compares against `engine.archive_db_path()` in the main window (rule 4: the widget and the loader never see the engine). Cross-process: the app-lifetime QLockFile (M2-C) already keeps other instances from recording under this root. |
| 2026-06-11 | M3-D: the re-index worker reuses the M3-C serial-queue semantics (shutdown-only stop, queued clear re-arm); completion reports survive the auto-refresh via a one-shot notice | A re-index is an explicit action on a directory — supersede semantics would cancel work the user asked for; the stop/clear reasoning is identical to the export auditor finding. The tab's refresh used to overwrite the completion report within one event-loop turn (caught by the e2e test): the notice is consumed by exactly one `_populate_sessions` pass. |

## Open questions (resolve before the milestone that needs them)

1. ~~M0-A: migrate old QSettings or reset?~~ **Resolved 2026-06-10: reset,
   log once** (see decision log — QSettings holds only window/layout state).
2. M1: does the deployed firmware support CORS for a desktop client? N/A —
   we call from Python, not a browser; confirm no Origin checks server-side.
3. M2: should Monitoring without Recording still write the metadata DB
   (detections)? Proposed: yes — detections are cheap and useful; waveforms no.
   *M2-A interim:* current behaviour kept (a monitoring device with STA/LTA
   creates the DAO and persists detections); final call belongs to M2-B's
   session model.
4. M4: tile stack choice (offline requirement? QtWebEngine weight?).
5. M5: common-window vs per-device windows for array HVSR (see M5-A).
6. M7: signing — is a Windows cert / Apple Developer account available, or
   do we ship unsigned with documented bypass instructions?
