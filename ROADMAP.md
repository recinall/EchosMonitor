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

- [x] **A. Position resolver** `core/positions.py`: StationXML lat/lon/elev
      via the M1 client, manual override wins, cached, refreshed on demand
      (rule 16). Done 2026-06-12.
  - [x] source priority **override > StationXML > live GNSS** (decision
        log) — pinned against the real devices first (read-only GETs,
        user-authorized): the firmware embeds a 6-decimal *snapshot* of
        the GNSS fix into StationXML, `/api/status` is the live fix.
        Lat/lon 0/0 ("null island") = no-fix placeholder, treated as
        absent. Every `ResolvedPosition` is tagged with its source.
  - [x] `PositionResolver` facade (ArchiveDetailLoader owner canon) +
        `_PositionWorker` (EchosStatusWorker canon: queued resolve slot,
        `asyncio.run`, plain-method `stop()` with task-cancel nudge).
        Public credential-less GETs only — can never trip the lockout.
        ONE instance to be owned by MainWindow; Map tab (B) and M5 HVSR
        both consume it. MainWindow wiring lands with B (first consumer).
  - [x] failure vocabulary = `EchosErrorKind` + `"unavailable"` (device
        answered but has no position anywhere / no source at all);
        a failed refresh keeps the last known cached position.
  - [x] reviewer findings (2 majors, 2 minors) fixed with behavioral
        regression tests verified to fail pre-fix: `configure`/`refresh`
        supersede in-flight sweeps (generation written even on empty
        dispatch; rapid refreshes coalesce to one sweep); override path
        pre-emit stop check; `shutdown()` is terminal (dispatch refused,
        thread never restarted into a stopped worker).
- [x] **B. Map widget**: Done 2026-06-12. **Decision (open question 4):
      pyqtgraph scatter in a local east/north metre frame — NO web tiles,
      NO QtWebEngine** (decision log). Markers: device name label, state
      colour (Idle grey / Monitoring green / Recording red — the Devices
      dock hexes — plus amber "trouble" when a non-idle device's socket
      is not CONNECTED), hover tip with lat/lon/elev/source, click →
      `DevicePanel.select_device`. Inter-device distance table
      (haversine, sorted; M5's readout), unpositioned devices listed with
      their failure kind. `MapWidget` is a pure consumer (signals in/out,
      holds no resolver/engine reference); MainWindow owns the ONE
      resolver, pushes `PositionQuery`s on every configChanged, and
      shuts the resolver down in closeEvent (terminal + idempotent).
      Pure `haversine_m` / `local_east_north` (antimeridian-normalised)
      live in `core/positions.py` for M5 reuse. Reviewer minors fixed:
      unchanged-state early-out (no plot churn on flapping retries),
      dead assert dropped, ghost-device `select_device` no-op pinned.
- [x] **C. HVSR/M5 hooks**: Done 2026-06-12. `core/positions.py`:
      frozen `StationGeometry` (devices, positions, lexicographic-pair
      `distances_m`, order-free `.distance()` that raises `KeyError` for
      non-members) + pure `distance_matrix()` / `station_geometry()`;
      `PositionResolver.geometry(devices=None)` snapshots the cache
      (never aliases it) with optional selection — names without a
      position are excluded and discoverable via the `.devices` diff
      (M5 must render "no position" from that, not guess). The Map tab's
      distance table now consumes the same shape, so it and the future
      M5 report cannot disagree.

**M4 CLOSED 2026-06-12** — resolver (A), map tab (B), geometry hooks (C);
gate green at every stage, both reviewers passed each diff.

## M5 — Multi-device HVSR

Goal: run HVSR over N Echos devices simultaneously, position-aware.

Honest scope (skill: `hvsr-array`): synchronous per-station HVSR — each
device gets its own accumulator/curve (hvsrpy owns the physics, never
re-implemented); the array layer adds comparison and spatial context. True
array methods (SPAC/FK) are explicitly OUT of scope unless re-planned.

- [x] **A. `core/hvsr_array.py`**: Done 2026-06-12. `HvsrArrayEngine` =
      N `HvsrAccumulator`s + orchestration on the exact `HvsrEngine`
      skeleton (one worker thread, pending≤1 with skip+throttled log,
      bounded join). Windowing: **per-device independent** (open question 5
      resolved — see decision log); the shared disjoint-window capture was
      extracted to module-level `capture_disjoint_window()` in
      `core/hvsr_engine.py` and is called once per device per tick, each
      station carrying its own `last_window_end` cursor. One compute
      request per cycle runs the eligible (≥3 windows live, ≥1 forced)
      devices' snapshots SERIALLY on the worker, stop-flag checked between
      devices; a per-device compute failure lands in
      `ArrayHvsrResult.errors` and never blocks the other devices.
      `ArrayHvsrResult` is frozen and self-contained per cycle (devices,
      results, errors, geometry snapshot, settings, provenance);
      `unpositioned()` exposes the rule-16 "no position" diff.
      `responses_identical` runs per device; same-fs across devices NOT
      required (per-accumulator internal check only). Tests:
      `tests/core/test_hvsr_array.py` (independent windows, bounded
      in-flight, failure isolation, bounded stop during busy cycle,
      start→stop→start, per-device override, geometry/unpositioned).
      Audit confirmed: `HvsrEngine` holds exactly one `_Measurement`;
      `HvsrAccumulator` is cleanly per-device; the widget's
      `three_component_groups()` is already multi-device-aware.
      Review fixes (both reviewers, mutation-verified failing pre-fix):
      the stop test asserts on recorded compute invocations (devB never
      runs after a stop during devA's compute) instead of elapsed time;
      groups must be exactly Z/N/E (an extra component would make the 3C
      capture silently never-ready); `ArrayHvsrResult` maps are
      `Mapping`-typed so consumers can't mutate a frozen cycle. Auditor
      F1 (HIGH, pre-existing): `HvsrEngine.start_archive_measurement`
      restarted the thread WITHOUT the queued `clear_stop`, so a stale
      queued `request_stop` surviving `quit()` (the recorded postmortem
      race) could silently drop the one-shot archive compute — fixed with
      the FIFO stale-stop→clear→compute ordering + deterministic
      regression test (`test_archive_start_survives_stale_queued_stop`).
- [x] **B. UI**: Done 2026-06-12. NEW `gui/widgets/hvsr_array_widget.py`
      as its own "HVSR Array" central tab (the single-station tab stays
      untouched): checkable (device, station) rows (duplicate-device
      selection refused — the array is keyed by device, rule 16), ONE
      shared settings row (skill `hvsr-array`), N mean-curve overlay one
      colour per device with optional faint per-window curves (drawn as
      ONE NaN-separated item per device — auditor F2: per-window items
      would stall the GUI thread on long runs), per-device table (f0±σ,
      T0, A0, windows, SESAME rel/clar with criterion tooltips, response
      verdict verbatim, error). A0 annotated response-sensitive in three
      places; "no position" note from the geometry diff. Map: blue→red
      log-f0 ramp overlay (`set_f0_overlay`) — only devices with an
      honest f0 are recoloured, others keep state colour; Clear-f₀
      button; overlay persists across stop, cleared on the next array
      start; an all-error cycle clears it (no stale colours over fresh
      errors). MainWindow owns `HvsrArrayEngine`, injects
      `PositionResolver.geometry` (rule 16), shuts the array engine down
      before the streaming engine. Hardening from review (auditor
      F3/F4): `_ArrayWorker` latest-wins `_active_id` token kills stale
      queued cycles instantly; `arrayWindowCounts` now carries the
      measurement id. Known accepted costs (auditor F1/F5, both
      precedent-class): `responses_identical` runs ×N on the GUI thread
      at start (cheap when no response metadata is configured — the
      Echos norm); an interactive stop mid-compute can block up to the
      8 s bounded join. Tests: `tests/gui/test_hvsr_array_widget.py`,
      f0-overlay cases in `test_map_widget.py`, MainWindow route via the
      REAL engine signals, smoke tab list updated consciously.
- [x] **C. Report**: Done 2026-06-12 (landed with B — the export glue
      lives in B's new widget). `storage/hvsr_report.py` gains
      `ArrayReportContext`, `array_result_to_dict` /
      `export_hvsr_array_json` (schema `echosmonitor.hvsr-array/1`; each
      station embeds its FULL single-station `echosmonitor.hvsr/1`
      structure, so the array file is a superset), pure
      `array_comparison_lines` and `write_hvsr_array_pdf`: ONE comparison
      page (N mean-curve overlay — never a cross-station average — + the
      f0/SESAME/response table + GEOMETRY block: positions with source,
      sorted inter-station distances, unpositioned diff, A0
      response-sensitivity note) then the existing per-station renderers
      reused per valid station. Stations without a valid result appear on
      the comparison page only. Widget: Save report…/Export JSON… gated
      on ≥1 valid station. Tests:
      `tests/storage/test_hvsr_array_report.py`.
- [x] **D. Archive mode**: Done 2026-06-12.
      `HvsrArrayEngine.start_archive_measurement(devices, t0, t1,
      settings, geometry, readers)`: per-device
      `slice_archive_windows` (windows stay per-device independent — a
      gappy device just contributes fewer; no-window devices stay
      selected but never enter the compute), archive-provenance
      accumulators, ONE forced off-thread cycle ending IDLE (one-shot),
      live timer never started; returns `""` when no device has a
      gap-free 3C window. Slicing is the documented one-shot INLINE read
      on the calling thread (M3-E precedent). Widget: archive from/to
      row + "Run on archive" driving a host-injected handler (the widget
      stays free of ArchiveReader construction); `_live_running` split
      from `_measurement_id` so an archive run is never a LIVE
      measurement (decision-log constraint upheld). Root seam (rule 14):
      `MainWindow._run_hvsr_array_archive` — a session selected in the
      Archive tab roots EVERY checked device at that SESSION root (one
      shared reader; the pull-based counterpart of the single-station
      hand-off ctx), else per-device `engine.archive_root`. Tests: core
      one-shot/per-device-independent/no-windows; widget handler
      contract + no-data message; MainWindow root resolution both ways.
      Review round (code-reviewer APPROVE, auditor PASS w/ follow-ups):
      auditor F2 FIXED (mutation-verified) — a stop whose bounded join
      times out leaves the thread finishing an uninterruptible compute
      with `quit()` pending; exec() then exits discarding queued events
      (the postmortem race) and a new measurement dispatched into it
      hung forever. `_boot_worker` now detects the poisoned thread,
      severs + abandons the (worker, thread) pair and rebuilds fresh;
      `shutdown()` drains abandoned threads bounded
      (`test_restart_after_join_timeout_rebuilds_worker`). Also from
      review: slicing loop got an elapsed/per-device-counts log (rule
      7); the engine's pre-return counts emit (dead toward its only
      consumer) replaced by widget-side seeding from
      `active_measurement()`; archive report GEOMETRY block carries the
      "positions resolved at RUN time, not recording time" honesty note
      (no archived position source exists); reentrant `_on_stopped`
      inside the archive click pinned by test. **Follow-ups (recorded,
      not blocking)**: (1) move the ×N inline archive slicing onto a
      one-shot worker (auditor F1 — the M3-E ×1 precedent's budget
      changed; the elapsed log is the interim observability); (2)
      `HvsrEngine` shares the pre-rebuild join-timeout skeleton flaw —
      port the poisoned-thread rebuild; (3) surface WHICH root was
      searched in the widget's no-data message (a stale Archive-tab
      session selection reads as "no data" with no hint).

**M5 CLOSED 2026-06-12** — array engine (A), UI + map f0 overlay (B),
multi-station report (C), archive mode (D); gate at close: 1059 passed /
5 perf-deselected, ruff + mypy --strict clean. Open question 5 resolved
(per-device independent windows). Follow-ups carried into M6: array
archive slicing onto a worker (auditor F1), HvsrEngine poisoned-thread
rebuild port, no-data root surfacing.

## M6 — Hardening & polish

- [x] **0. M5 follow-ups** *(done 2026-06-12)*:
      (1) array archive slicing moved onto the array worker (auditor F1):
      `start_archive_measurement` returns the id immediately and dispatches
      ONE slice+compute cycle; the worker owns the stations' accumulators
      until its result lands (engine-side `n_windows_total` backs all UI
      counts; `set_window_override` and `_request_recompute` are
      pending-gated), and `slice_archive_windows` grew a cooperative
      `should_stop` (polled per component read / window step — rule 7).
      (2) HvsrEngine got the poisoned-thread rebuild port (auditor F2) PLUS
      the latest-wins `_active_id` token, and BOTH engines' shutdown now
      retains (never drops) abandoned pairs whose bounded join timed out —
      dropping the last reference to a running QThread aborts the process
      (the mutation run crashed the interpreter, proving the test).
      (3) the array no-data outcome is now the async signal
      `arrayArchiveNoData(id, searched_roots)` and the widget message names
      WHICH archive root(s) were searched; a cycle where every slice READ
      failed announces the per-device errors instead of "no data"; the
      handler's `""` return is reserved for the no-reader degenerate case.
      Gate at close: 1071 passed / 5 perf-deselected, ruff + mypy --strict
      clean; all new regression tests mutation-verified.
      Known LOW gaps (auditor, deliberate): a second manual override issued
      while a post-slice recompute is still pending is dropped with a warn
      log (a `slice_inflight` flag distinct from `pending` would make the
      ownership guard exact); a whole-cycle worker failure surfaces only in
      the log + `ArrayHvsrSummary.last_error`, not in the per-device rows.
- [x] First-run wizard rewritten for Echos (discover device on
      `192.168.4.1` AP / mDNS `*.local`, set admin password, add device).
      *Done 2026-06-12:* full rewrite of `first_run_wizard.py` (the
      GFZ/IRIS public-server wizard is gone). Welcome (scan / AP-mode at
      `192.168.4.1` with serial-password instructions / skip) → Find
      (embedded M6-2 mDNS scan + manual host "Check device" via the new
      `EchosDiscoveryWorker.probe_host` — same typed public gate, but a
      MANUAL failure surfaces its kind; rows dedupe on normalized
      {hostname,address}+port; configured hosts marked, not blocked) →
      Details (de-collided name + OPTIONAL admin password). Finish
      writes the DeviceConfig via ConfigStore (mDNS hostname host,
      PROBED SeedLink port, StationXML-exact selectors — the discovery
      probe now fetches `/api/stationxml` best-effort into
      `DiscoveredEchos.channels`) and stores the password via
      `EchosDeviceWorker.storeCredential` on the wizard's worker thread
      (15 s bounded; timeout/early-close accept with a warn — the device
      write is already durable). The wizard performs NO device writes:
      changing the password ON the device stays in the device dialog
      (that POST is still unexercised on real firmware — M1 closure).
      ONE lazily-started thread hosts both workers (an undriven wizard
      owns no running thread — fixed a real destroyed-while-running
      crash); audit PASS, all 8 findings fixed; obsolete InfoWorker
      integration test consciously rewritten for the same contract
      class (queued off-GUI dispatch) on the new worker.
- [x] mDNS discovery of Echos nodes on the LAN (optional, zeroconf).
      *Done 2026-06-12:* advert contract PINNED LIVE against fw 1aa72cbe —
      the firmware advertises `ADS131M04-WebServer._http._tcp.local.` with
      TXT `board=ESP32-S3` (pihw.local advertises NO http service: a
      Pi-hosted node is added manually; discovery is a convenience, never
      the only path). `core/discovery.py` `EchosDiscoveryWorker` (status-
      poller canon: queued one-shot `discover`, plain-method stop with
      threadsafe asyncio cancel): browse `_http._tcp` 4 s → advert cap 64
      + dedup → name-hint-first resolves under a 10 s aggregate budget →
      loose prefilter (`is_echos_candidate`) → CONFIRM via typed public
      `GET /api/status` + `GET /api/seedlink/config` (credential-less —
      lockout unreachable); confirmed nodes STREAM out as they land.
      `DiscoveryDialog` (worker per dialog, latch-guarded teardown,
      disconnect-after-join, join-timeout pairs retained in module-level
      `_ABANDONED`) + DevicePanel "Discover…" toolbar action; "Add
      device…" hands `DeviceDialog.add` an exact prefill (mDNS hostname,
      PROBED SeedLink port, advertised REST port); configured hosts
      (case/dot-normalized) are marked and locked. zeroconf is a regular
      dependency, lazily imported (stripped install →
      `discoveryFailed("unavailable")`). 17 tests (worker canon incl.
      stop-mid-scan + streaming + off-GUI-thread; dialog incl. prefill,
      double-teardown latch), key gates mutation-verified.
- [x] Device clock/GNSS health surfaced (PPS lock from status poller).
      *Done 2026-06-12:* `ClockHealth` closed verdict (PPS > GNSS > NTP >
      HOLDOVER > UNSYNCED) derived on `EchosDeviceSnapshot` from the
      `/api/status` sync BOOLEANS only (`time_sync_type` is a free-form
      firmware composite — display-only); snapshot carries
      `time_synchronized`/`ntp_synchronized`/`time_sync_type`/
      `pps_offset_us` with pessimistic defaults. DevicePanel Echos column
      shows the token (`clk PPS` … `clk hold (!)` / `clk none (!)` as
      attention states); tooltip carries the honest accuracy sentence +
      firmware sync string + PPS offset when locked. HOLDOVER exists
      because `time_synchronized` alone (clock set once, all live sources
      lost, crystal drifting) must never read as "NTP, network accuracy"
      (reviewer MAJOR on the first cut). Tests at models/poller/panel
      layers, verdict ladder mutation-verified.
- [x] Settings dialog (archive root, theme, display caps).
      *Done 2026-06-12:* File → Settings… (`gui/dialogs/settings_dialog.py`)
      edits `app.archive_root` (path picker; empty = the platformdirs
      default, shown verbatim as the placeholder via the ONE shared
      resolver) + `ui` theme/refresh/window/max-plots/max-display-rate,
      through the new `ConfigStore.update_settings` (same rule-3
      validate→rotate→atomic-write→emit pipeline; `_commit_candidate`
      refactor shared with the device mutations; devices untouched → the
      engine diff is a no-op, verified per configChanged consumer).
      `ui.theme` is finally CONSUMED: `gui/theme.py` applies pyqtgraph
      background/foreground at bootstrap (plots only — widget chrome
      stays on the system palette); all fields honestly labeled
      "next launch". `ui.recent_detections_limit` deliberately NOT
      exposed (reviewer: nothing consumes it since the rule-13 autostart
      removal — a dead setting in this dialog would lie; it's the knob
      for the open M3 cross-session detection prefill).
- [x] Docs: user manual for the field workflow (deploy → configure →
      record → HVSR → report).
      *Done 2026-06-12:* `docs/MANUAL.md` — grounded in the actual UI
      labels (wizard pages, Devices-dock actions, session-toolbar
      states, tab names), the clock-health token table, the rule-13/14
      state model, and a troubleshooting section (mDNS, rename-orphaned
      credential, 429 lockout, holdover, lost password).

**M6 code complete 2026-06-12** (all five items + the M5 follow-ups).
Gate at close: 1106 passed / 5 perf-deselected, ruff + mypy --strict
clean. Remaining before M6 CLOSURE (user-gated, do not do unsolicited):
(1) real-device validation of the wizard/discovery against echos.local
(read-only: mDNS advert + public probe — the advert contract was
already pinned live 2026-06-12) and, separately, the first REAL
authenticated write (password/credential flow) which remains
unexercised on hardware; (2) the legacy config migration question —
the user's real device config still lives at the old
`~/.config/seedlink-dashboard` path and EchosMonitor sees 0 devices
until it is copied/migrated (ask the user; the wizard/Settings work is
the natural moment).

## M6.5 — Field-test hardening (from the first real recording, 2026-06-12)

The user's field run (real echos.local, fw 1.4.2, 500 Hz × 3 ch) proved
the M6 deliverables end to end on hardware: wizard (mDNS discovery →
typed probe → add device "echos" with StationXML selectors → keyring
credential store), Monitor, Record (project `Test_1`), Archive
browse/waveform load and Map click all worked. The recording also
surfaced real problems — this milestone fixes them.

- [x] **A. Archive sample loss under burst (CRITICAL — recorded data
      was dropped).** FIXED 2026-06-12: the engine-side archive inbox
      is gone — recorded packets post straight to the storage thread
      from ``_on_packet`` (no drop point on the science sink; the old
      deque was drained only by the flush tick, which the replay burst
      starved — the field archive shows a contiguous 32.8 s hole of
      LIVE data per channel). Rule-5 observability is an in-flight
      gauge (sent − writer-acked, terminal-signal invariant pinned in
      MseedWriter) warn-logged + signalled above ``queue_max`` (field
      repurposed as the warn threshold). Regression:
      ``test_replay_burst_loses_no_recorded_samples`` (fake-server
      ``burst_records`` replay, mutation-verified) +
      ``test_archive_inflight_gauge_warns_without_dropping`` +
      ``test_exactly_one_terminal_signal_per_write_including_pause``.
      The storage thread was never the bottleneck at this seam (the
      drain only posted queued events); writer-side profiling moved to
      item C. Log evidence from the run:
      `streaming_engine_archive_backpressure dropped=29` during steady
      recording (17:45:28), then the device disconnected mid-recording
      (17:47:55), the worker reconnected in ~1 s and the device
      replayed its ring backlog (gap_detector_clock_jump back
      9.6–12.2 s; gaps of 1344/1488 samples logged), and the replay
      burst overflowed the engine→storage archive inbox
      (`queue_max=1024`) → `dropped=440`. Drop-oldest discarded
      RECORDED samples. Directions to investigate (consult skills
      `miniseed-sds` + `qt-worker-threading` first): (1) size the
      inbox for replay bursts (device ring seconds × fs × channels —
      the ring is sized in kB via /api/seedlink/config); (2) recording
      correctness > liveness during catch-up: consider back-pressuring
      the SeedLink reader during FETCH replay instead of dropping
      (rule 11 protects the DISPLAY consumers, not the archive path —
      the archive IS the science sink); (3) profile WHY the storage
      thread drains slowly (fsync cadence? STEIM2 encode per tiny
      record? DB work per record?). Regression test must reproduce a
      replay burst against the fake SeedLink server.
- [x] **B. Gap-detector subsample chatter.** FIXED 2026-06-12. Field
      forensics (Test_1 archive): the jitter is DEVICE-WIDE (identical
      offsets on all 3 channels at the same instants), reaches
      ±2.55 samples (±5.1 ms — NOT subsample, which is why the
      existing half-sample tolerance never caught it), and comes in
      gap→overlap pairs netting zero; it fragmented the archive into
      17 on-disk segments per 440 s. Fix:
      `archive.jitter_tolerance_ms` (default 10 ms, absolute time —
      clock wobble is a time property, so the "≤ half a sample"
      suggestion was rejected as both too small and wrongly
      rate-scaled) with the half-sample floor; within tolerance the
      packet is contiguous, its stamp is SNAPPED onto the
      reconstructed grid before writing (engine applies
      `GapDetector.last_snap_s` on the archive branch only), and
      `last_end` follows the grid so zero-mean jitter never
      accumulates; drift crossing the tolerance re-anchors with one
      honest event. On-disk continuity proven by
      `test_rectified_jittered_stream_is_contiguous_on_disk` (raw
      jitter fragments as negative control) and the engine wiring
      test (mutation-verified).
- [x] **C. App efficiency.** DONE 2026-06-12 (synthetic; real-device
      confirmation rides item E). `scripts/profile_500hz.py` drives a
      real engine+writer with field-shaped packets (500 Hz × 3 ch,
      108-sample records, dead-host worker, the high-rate-load
      pattern; headless — no render/PSD consumers attached, so the
      figures are the science path only). Findings on this machine
      (SeedTiLa prod running):
      1 device = 7.0 % of one core, 2 devices = 14.3 %, flush ticks
      sub-ms p99, archive in-flight high-water **1** at both loads —
      zero steady-state backpressure with ample second-device
      headroom; the field's steady drops were tick starvation (fixed
      in A), never storage slowness. Hotspot found + fixed: obspy
      `Stream.write(format="MSEED")` re-resolves its format plugin
      via importlib.metadata per call (~3 ms/packet, 54 % of writer
      CPU) — the writer now binds `_write_mseed` once and skips the
      per-packet `trace.copy()` when no dtype cast is needed
      (1-device CPU 7.0 % → 5.1 %). Per-packet records (≈108 samples
      per 512-byte record, ~4.7× the optimal STEIM2 size) are KEPT:
      coalescing across packets would trade the crash-tight
      write-on-arrival discipline for archive size — revisit only if
      field sessions make size hurt.
- [x] **D. Map satellite layer.** DONE 2026-06-12, the preferred way —
      no QtWebEngine needed. `core/map_tiles.py`: slippy-map tile math
      (pure, unit-tested against known values) + `TileFetcher` worker
      (httpx, 10 s/tile timeout, ≤36 tiles/batch, latest-wins
      generation, disk cache under `user_cache_dir` with the rule-8
      atomic recipe + poisoned-entry eviction). MapWidget gains a
      checkable **Satellite** button: tiles drawn as pyqtgraph
      ImageItems (zValue −10) under the scatter/f0 overlay, placed in
      the local E/N frame via `local_east_north` of the tile corners
      (sub-metre mismatch at array scale); `Fit view` still fits the
      ARRAY (ignoreBounds); Esri attribution rendered whenever the
      basemap is on, doubling as the honest offline note; basemap
      re-requested only when the frame origin/extent actually moves
      (state-only rebuilds never blank it); cached tiles serve
      offline. Worker thread is widget-owned, lazy-started on first
      toggle, joined bounded in `shutdown_basemap` (MainWindow
      closeEvent) with the M6-0 abandoned-retention pattern.
      Visual orientation check against the real site rides item E.
      **Superseded by M6.5-F below**: the original static-per-extent
      basemap did not follow pan/zoom (user: "spostandosi la mappa
      non si aggiorna"); it now does.
- [x] **F. Basemap follows the viewport (pan/zoom).** Added
      2026-06-13 after the field user reported the M6.5-D basemap only
      covered the device's surroundings and never updated on
      pan/scroll. The map now refetches the tiles for the VISIBLE
      region: `sigRangeChanged` → debounced (200 ms) → inverse-project
      the viewport to a lat/lon box (`positions.east_north_to_latlon`,
      round-trip-tested) → zoom from the viewport span → fetch only the
      missing tiles. Tiles persist in an LRU (cap 96, rule 5/8) so a
      pan-back reuses them; finer zoom draws above coarser
      (zValue per zoom) so a zoom transition never flashes blank. The
      single-device degenerate-view rescue (M6.5-E) still fires, but
      only recentres on toggle/origin-change — once the user pans to
      the surroundings an arriving tile never yanks the view back.
      Widget now self-cleans on `closeEvent` (stop debounce + join
      thread). Verified end-to-end headless against real Esri tiles
      for the site (toggle → pan → zoom all render full imagery).
- [x] **F. Wizard wrote into the BUNDLED default.yaml (fixed
      2026-06-12, same day).** The field run exposed it: with no user
      config, `load_config(None)` returned the bundled
      `src/echosmonitor/config/default.yaml` as the resolved path,
      MainWindow handed that to ConfigStore, and the wizard's
      `add_device` atomically REWROTE package data in place (plus a
      `.yaml.1` backup in the source tree — both briefly committed,
      then cleaned). In a packaged install the bundle is read-only →
      every wizard finish would fail. Fix: the loader's fallback now
      returns the USER config path as the resolved (write-target)
      path; the bundle is never a write target. The user's
      wizard-written device config was moved to
      `~/.config/echosmonitor/config.yaml` and the pristine bundle
      restored. Regression test
      `test_fallback_path_is_writable_user_path_not_bundle` pins a
      first write creating the user file with the bundle untouched.
- [x] **E. End-to-end perfection pass.** DONE 2026-06-12 (headless
      scope; two visual checks ride the next GUI session — below).
      `scripts/e2e_field_check.py` recorded the REAL echos.local for
      4 min through the real engine into project `M65_E2E_Claude`
      (kept in the archive as evidence — browsable in the Archive
      tab): 5155 packets, **zero backpressure** (A/C), **zero
      gap/overlap events** (B; the device's clock state at run time:
      GNSS 12 sats, PPS locked, RMC+PPS+NTP), coverage exactly
      1.0000 on all 3 channels, ONE contiguous segment each (the
      original field run had 17). Session `archive.db`: 1 session /
      3 streams / 3 files / **0 gap rows**. `ArchiveReader`
      read-back of a 180 s window: exactly 90001 samples/channel,
      unmasked int32 — gap-free science windows for archive HVSR.
      Map: 30 real Esri World Imagery tiles fetched+decoded+cached
      for the actual site (45.8852 N, 11.0609 E, z18). No device
      writes were made; no rough edges found in the exercised paths.
      USER CHECKLIST for the next GUI session: (1) toggle Satellite
      on the Map tab and confirm imagery orientation/placement
      against the known site — **first attempt FAILED (2026-06-12
      evening, "la tendina map NON visualizza la mappa satellitare")
      and is FIXED**: with one positioned device the auto-ranged
      viewport collapsed to a degenerate ~0 m span, so the data-sized
      tiles were invisible behind a pixel-sized marker; the view now
      snaps to the imagery extent when degenerate/elsewhere (decision
      log), `Fit view` floors each axis at 50 m, and a headless
      render-grab of the real site imagery verified placement,
      tile-seam continuity and north-up orientation (also pinned by a
      render-sampling test); the basemap now follows pan/zoom (M6.5-F,
      2026-06-13) — please re-check scrolling/zooming on screen; (2) one
      GUI-driven monitor→record→browse→HVSR→report pass (those code
      paths are gate-covered and untouched by M6.5, but eyes beat
      tests).

Field-run notes for the M6 closure items: the real-device
wizard/discovery validation is DONE (read path + keyring store worked
on hardware); `POST /api/auth/password` (changing the password ON the
device) remains the only unexercised write. The legacy-config
migration question is SUPERSEDED — the user re-added the device via
the wizard; pihw.local can be added manually the same way if wanted.

## M6.6 — Pre-release refinements (planned 2026-06-16; do M6.6 before M7)

Four user-requested refinements from the 2026-06-15 GUI session. This
section is a HANDOFF: each item below carries its root-cause, the exact
files/lines, the acceptance criteria, the rule constraints, and the
decision already taken, so a fresh post-`/clear` session can execute it
stage by stage. Investigated read-only; nothing was implemented in the
planning session. Per CLAUDE.md workflow: run the named skill FIRST for
each stage, write the regression test (mutation-verify it), full gate in
the BACKGROUND (one pytest instance, `timeout 600`), `code-reviewer` on
every diff + `qt-concurrency-auditor` on anything touching workers/
threads/timers, update the boxes + decision log, commit per stage.
Suggested order: A (small, science-critical) → D (self-contained) →
B (depends on nothing) → C (touches the poller + B's fetch path; do
after B so the StationXML pre-acquisition fetch and the poll-backoff are
designed together).

- [x] **A. HVSR assigns N/E by channel code, not alphabetically
      (SCIENCE BUG).** DONE 2026-06-16: `three_component_groups_from_pairs`
      now maps by orientation char (`N`/`1`→N, `E`/`2`→E, `Z`/`3`→Z);
      docstring rewritten; regression tests in `tests/core/test_models.py`
      (alphabet-vs-code mutation-verified). Original report follows. ▸ The
      HVSR tab shows `Z=HHZ N=HHE E=HHN` — North
      and East are SWAPPED, and the swap is in the science path, not
      just the label: every GUI-generated HVSR curve (live AND archive)
      has fed hvsrpy's `ns`/`ew` reversed. Root cause:
      `core/models.py:348-354` `three_component_groups_from_pairs` does
      `horizontals = sorted(n for o, n in orients.items() if o != "Z")`
      then `{"N": horizontals[0], "E": horizontals[1]}` — `sorted()`
      orders the full NSLC strings, and `…HHE` < `…HHN` alphabetically,
      so N gets HHE and E gets HHN. The orientation code is ALREADY
      parsed one block up (`orient = parts[3][2]`, line 345) but then
      thrown away. Fix: map by orientation suffix — `N` or `1` → N,
      `E` or `2` → E (keep `Z`/`3` handling), and when only `1`/`2`
      exist map 1→N, 2→E (SEED convention); the docstring at
      `models.py:333-335` currently codifies the wrong "first/second"
      rule — rewrite it. The fix is ONE function; the consumers
      (`gui/widgets/hvsr_widget.py:507-509` label,
      `core/hvsr_engine.py:216-220` live capture,
      `core/hvsr.py:412-415` hvsrpy feed + `:866-911` archive slice)
      all read the corrected dict automatically. Acceptance: a unit
      test on `three_component_groups_from_pairs` proving HHZ/HHN/HHE
      AND the 1/2/3 convention map by code not alphabet (mutation-
      verify by reintroducing `sorted()`); the HVSR label shows
      `N=HHN E=HHE`. Decision-log that pre-fix GUI HVSR results had
      N/E swapped (horizontal-combination dependent: geometric-mean /
      squared-average are symmetric in N,E so f0 is unaffected, but
      azimuth-dependent combos and any directional reading were wrong).
      Skill: `hvsr-array` (and re-read its component-ordering note).
      No worker/thread surface → no qt-concurrency-auditor needed.

- [x] **B. Auto-read + persist the device StationXML.** DONE 2026-06-16:
      `fetch_stationxml(client)` (never-raises helper) on the status-poller
      worker via a new `fetch_stationxml` slot + `stationXmlReady` signal;
      fetched off-thread when a device enters Monitoring/Recording (de-duped
      per acquisition). Persisted per `(session, device)` in the new
      `session_stationxml` table (schema v6, idempotent migration) via
      `engine.persist_session_stationxml`; read back through
      `archive_reader.read_session_stationxml`. `ResponseProvider` gains a
      lock-guarded blob store + `inventory_from_stationxml_blob`, so
      `remover_for`/`is_configured` resolve a remover from the blob with the
      config-file override still winning (rule 16). Archive analysis preloads
      the selected session's blob into the provider. Full unit + integration
      tests; qt-concurrency-auditor PASS, code-reviewer APPROVE. Original
      plan follows. ▸ The firmware
      serves `GET /api/stationxml` (public, no auth — `echos_api.py:450
      get_stationxml`, already used by the wizard for selectors and by
      `core/positions.py` for coordinates). Goal: fetch it
      automatically BEFORE each acquisition and PERSIST it per session
      so Archive browsing and archive HVSR/deconvolution use the real
      instrument response + coordinates without re-querying the device.
      Today instrument response comes ONLY from a user-supplied local
      file (`device.response_metadata.path` → `core/response.py
      ResponseProvider`); if absent, HVSR/decon silently degrade to
      counts. Plan:
      (1) Pure async helper `fetch_stationxml(client) -> str | None`
          (never raises; logs + None on failure). Lives in `core/`
          (rule 2 networking is sanctioned in `echos_api`/adjacent).
      (2) Fetch OFF the GUI thread (rule 1) when a device goes
          Idle→Monitoring/Recording (rule 13 — user-triggered, never on
          launch). Reuse the status-poller thread or a one-shot task;
          design the trigger TOGETHER with item C's poll-backoff so the
          two REST touch-points share one policy.
      (3) Persist per session+device (rule 14): new
          `session_stationxml(session_id, device_name, xml_blob,
          fetched_at, UNIQUE(session_id,device_name))` table in
          `storage/db.py` (schema bump v5→v6, `CREATE TABLE IF NOT
          EXISTS`, migration is a no-op stub for old DBs per the M0-B
          precedent), DAO write on the storage thread after fsync
          (rule 8), read-back via `storage/archive_reader.py
          read_session_stationxml(...)`.
      (4) `core/response.py`: a `ResponseProvider.from_stationxml_blob`
          path so a `ResponseRemover` can be built from the persisted
          XML (live decon keeps the config-file override as the winner,
          rule 16-style: explicit override > fetched StationXML).
      Acceptance: against the fake firmware, starting a recording
      persists the StationXML blob; the Archive tab / archive HVSR for
      that session resolve a response from the blob with NO live device
      call; fetch failure degrades gracefully (counts, one warn).
      Files: `core/echos_api.py` (helper or reuse), `core/
      streaming_engine.py` (fetch+persist orchestration),
      `storage/db.py` + `storage/dao.py` + `storage/archive_reader.py`,
      `core/response.py`, fake at `tests/core/echos_fake.py` already
      serves a 3-channel StationXML. Skills: `echos-rest-api` (endpoint/
      auth) + `miniseed-sds` (DB-after-fsync, schema) +
      `qt-worker-threading` (off-thread fetch). qt-concurrency-auditor
      REQUIRED (new off-thread fetch + storage write).

- [x] **C. Minimize REST polling while SeedLink streams.** DONE 2026-06-16:
      slow-heartbeat policy (see decision log) — `EchosStatusWorker` gains a
      `set_streaming` slot + a worker-thread streaming set; the tick uses
      `poll_interval_streaming_s` (default 30 s) while a device is in the
      set, and a device leaving the set is made due immediately to resume
      full cadence. MainWindow keys the set off `engine.deviceStateChanged ==
      ConnState.CONNECTED` and pushes only on change. Schema knob
      `echos.poll_interval_streaming_s` (mirrored in both default.yaml).
      Worker timing tests (back-off + resume-on-drop, mutation-verified) +
      MainWindow wiring + schema bounds; qt-concurrency-auditor PASS.
      Original plan follows. ▸ The status
      poller (`core/echos_status.py`, `EchosStatusWorker`, started
      unconditionally in `main_window.py:371-378`, one shared QThread)
      hits THREE public GETs every `echos.poll_interval_s` (default 5 s,
      `schema.py:318`): `/api/status` (clock/GNSS/PPS — the clock-health
      column), `/api/seedlink/status` (clients + ring %),
      `/api/calibrate/status` (calibration state). It polls
      UNCONDITIONALLY — it never checks whether SeedLink is happily
      streaming. Once the SeedLink TCP stream is CONNECTED and packets
      flow, most of this is redundant (the data itself proves the device
      is alive; client-count and idle calibration state are noise).
      Keep what is ORTHOGONAL to data flow: clock discipline
      (PPS/GNSS/NTP — timestamp trust) and ring % (only meaningful near
      exhaustion). Plan a "back off while streaming" policy: the poller
      slows or skips while a device's `ConnState.CONNECTED` AND packets
      arrived recently, and resumes full cadence when the stream stalls/
      drops (which is exactly when REST is useful — to detect a reboot
      vs a network hiccup). Wiring: MainWindow already has
      `engine.deviceStateChanged` and the worker `diagnosticsUpdated`/
      `statsUpdated`; feed a per-device streaming flag (or a
      `last_packet_monotonic`) into `EchosStatusWorker.configure(...)`;
      the worker checks it on its tick (worker-thread-only state, no new
      locks). Consider a slow "heartbeat" cadence while streaming (e.g.
      clock-health every 30-60 s) vs a hard skip — DECIDE and decision-
      log it; expose via a schema knob (e.g.
      `echos.poll_interval_streaming_s` or `skip_poll_while_streaming`).
      Note: public GETs never trip the 429 auth lockout, so this is
      purely about device/LAN load, not safety. Acceptance: with a
      device CONNECTED and streaming (fake SeedLink), the poller issues
      ≤ the heartbeat cadence (assert call count over a window); when the
      stream drops, full-cadence polling resumes; clock-health column
      still updates within the heartbeat. Files: `core/echos_status.py`,
      `gui/main_window.py`, `config/schema.py`, maybe `core/models.py`
      (`last_packet` on diagnostics). Skills: `echos-rest-api` +
      `qt-worker-threading`. qt-concurrency-auditor REQUIRED.

- [x] **D. Implement the Log tab (currently a placeholder).** DONE
      2026-06-16: `utils/logging.py` gains `QtLogSink` (bounded
      `deque(maxlen)` under a lock, `Signal(object)` bridge,
      `install_gui_log_sink`); new `gui/widgets/log_widget.py` (level
      filter, autoscroll, pause, clear, copy, atomic export via new
      `storage/log_export.py`); `__main__` installs the sink and hands it
      to MainWindow; `app.log_max_lines` knob (default 1000). Tests:
      `tests/utils/test_logging.py`, `tests/gui/test_log_widget.py`,
      `tests/storage/test_log_export.py`, schema bounds + a MainWindow
      end-to-end record from a worker thread. qt-concurrency-auditor PASS,
      code-reviewer APPROVE (after routing the export through storage/ +
      QMessageBox on failure). Original plan follows. ▸ The
      bottom dock's Log tab is `_make_placeholder_dock(_DOCK_LOG)` — a
      centered QLabel (`main_window.py:737,785-792`). Logging is
      structlog→stdlib with a SINGLE `StreamHandler(sys.stderr)`
      (`utils/logging.py:81-92`); nothing reaches the GUI and there is
      no file sink or ring buffer. Plan an in-app log viewer:
      (1) A `logging.Handler` subclass wrapping a QObject with a
          `Signal(object)` carrying a frozen record (level, ts, logger,
          event, rendered line). `emit()` is called from ANY thread
          (worker or GUI) — keep a bounded `deque(maxlen=N)` under a
          `threading.Lock` (rule 5: cap + drop-oldest, no unbounded
          growth) and emit the Qt signal QueuedConnection → GUI slot
          (rule 1/4: only the GUI thread touches the widget).
      (2) New `gui/widgets/log_widget.py`: read-only view (QPlainTextEdit
          or a capped model/list), level filter, autoscroll + pause,
          clear, copy/export. Prefill from the ring buffer's snapshot on
          construction (logs emitted before the tab existed).
      (3) Install the handler in `configure_logging` (or right after,
          handing the sink to MainWindow) so worker-thread logs flow in;
          wire `recordReady → log_widget.on_record` QueuedConnection.
      (4) Optional schema: `app.log_max_lines` (default ~1000). The dock
          identity/objectName/QSettings layout are unchanged (just swap
          the placeholder body) so saved layouts keep working.
      Acceptance: a log emitted from a worker thread appears in the Log
      tab on the GUI thread; the buffer caps at `log_max_lines` (drop-
      oldest); level filter works; the smoke test
      (`tests/gui/test_main_window.py:52-71`, asserts the 4 docks incl.
      "Log") still passes and a new test drives a record end-to-end.
      Files: `utils/logging.py`, `gui/widgets/log_widget.py` (new),
      `gui/main_window.py`, `config/schema.py`, `__main__.py` (hand the
      sink to the window), `tests/gui/`. Skill: `qt-worker-threading`
      (the cross-thread sink). qt-concurrency-auditor REQUIRED (a
      Handler emitting a Qt signal from arbitrary threads is exactly its
      territory — the GIL-safe deque + QueuedConnection marshal must be
      audited).

Constraints for the whole milestone: do NOT start M7. Everything that
works must keep working (wizard, discovery, settings, clock-health
column, live tabs, session toolbar, archive browser/exports, HVSR
single+array, map + f0 overlay + satellite). Real device echos.local is
authorized for monitoring/streaming (and the public StationXML GET);
device-config WRITES still need explicit per-request go-ahead; respect
the 429 lockout; the dev box runs a live SeedTiLa prod instance (timing
flakes — rerun standalone before chasing; one pytest instance at a
time). Mutation-verify every regression test by reverse-edit, NEVER
`git checkout` (it wiped uncommitted work twice).

## M7 — Release: Windows / Linux / macOS

Goal: a tagged version produces installable artifacts for the three desktop
platforms from CI, reproducibly.

- [x] **A. Versioning + changelog**: semver from git tag (the package already
      reads `importlib.metadata`); CHANGELOG.md kept per milestone; window
      title + About dialog show the version.
      *Done 2026-06-16* (gate green; code-reviewer APPROVE; no threads → no
      concurrency audit):
  - [x] version is git-tag-driven via **hatch-vcs** (`dynamic = ["version"]`,
        `source = "vcs"`); a build writes the resolved version into a
        gitignored `src/echosmonitor/_version.py`. `__init__._resolve_version()`
        is a frozen-app-safe chain: `importlib.metadata` → generated
        `_version.py` → `"0.0.0+dev"`. Untagged dev checkout → `0.1.devN+g<hash>`.
  - [x] window title now shows `EchosMonitor v<ver>` (About + status bar
        already did, M0); `storage/hvsr_report.py:APP_VERSION` no longer a
        hardcoded `"0.1.0"` literal — it derives from `__version__` (the
        report's existing test already asserted equality with the package
        metadata, so this was latently required).
  - [x] app/window icon: `EchosMonitor.png` moved to
        `src/echosmonitor/resources/` (packaged + force-included in the wheel);
        `gui/resources.app_icon()` loads it via `importlib.resources`
        (bundle/zip-safe, never raises) and `__main__` sets it on the
        QApplication.
  - [x] `CHANGELOG.md` (Keep-a-Changelog), `[0.1.0]` backfilled at milestone
        granularity; `tests/test_versioning.py` pins the fallback chain (3
        branches), the title, and the changelog presence.
  - [x] **tagging `v0.1.0` deferred to the release cut** (C/E): the tooling
        already handles an untagged checkout; the first real artifact build
        tags then.
- [x] **B. Packaging tool decision** (record in the decision log):
      PyInstaller (one-dir) vs Briefcase. Constraints to verify on all 3 OS:
      PySide6 plugin bundling, obspy data files (e.g. seedlink/StationXML
      schemas), scipy/numpy size, hvsrpy + its transitive matplotlib +
      the IPython workaround pin. Produce a working local build first
      (`scripts/build.{sh,ps1}`), with hidden-imports/spec files versioned.
      *Done 2026-06-16* (decision: **PyInstaller one-dir**; gate green;
      code-reviewer APPROVE; built + verified on Linux):
  - [x] versioned `packaging/echosmonitor.spec` (one-dir, windowed) +
        `packaging/entry.py` shim + `scripts/build.{sh,ps1}`; `pyinstaller`
        and `pyinstaller-hooks-contrib` in the build-only `dev` group.
  - [x] constraint findings (all resolved in the spec):
        **obspy** needed three fixes — collect `RELEASE-VERSION` + a runtime
        hook (`packaging/rthooks/pyi_rth_obspy_version.py`) so obspy's
        relative frozen `__file__` doesn't point `OBSPY_ROOT` at the CWD and
        crash at import; and `collect_dynamic_libs("obspy", search_patterns=
        ["*.so",…])` because obspy's C libs are named `mseed.cpython-*.so`
        (the default `lib*.so` pattern collects nothing). **hvsrpy** pulls
        `IPython.display` at import (the pinned-`ipython` reason) → hidden
        import. **keyring** backends are entry-point-loaded → `collect_
        submodules("keyring.backends")`. **PySide6** plugins bundle via the
        contrib hook; QtWebEngine/Qt3D/Multimedia/tkinter excluded (unused).
        Bundle is ~697 MB uncompressed (scipy/numpy/PySide6/obspy/matplotlib)
        — the M7-C installers compress it; trimming is a follow-up.
  - [x] `--check` headless self-check added to `__main__` (construct config +
        main window in the freeze, exit 0 before the event loop/wizard); the
        packaged binary's `--check` runs the full start path
        (`config_loaded` from the bundled `default.yaml`, `streaming_engine_
        idle` per rule 13, `check_ok`). `tests/test_cli_smoke.py` pins
        `--version`/`--check` in a fresh interpreter (XDG-sandboxed) plus a
        skip-unless-present packaged-binary smoke (the M7-E seed).
  - [ ] M7-C carry-forwards (reviewer follow-ups): per-platform icons
        (`.ico`/`.icns` — PNG is Linux-ignored); Windows GUI-subsystem build
        has no stdout, so the packaged smoke uses `--check` exit codes, not
        `--version` stdout; trim bundle size.
- [ ] **C. CI matrix** (GitHub Actions). Split into two PRs (gate first,
      release next) so the cross-OS test surface is de-risked before the
      heavier installer work.
  - [x] **C1. PR gate** (`.github/workflows/ci.yml`): on every PR and push to
        main, run the gate (`ruff check src tests` → `mypy src` → `pytest`) on
        ubuntu/windows/macos. *Done 2026-06-16* (gate green locally — ruff 0,
        mypy 0, 1206 passed; code-reviewer APPROVE after two fixes; no threads
        → no concurrency audit):
    - [x] 3-OS matrix, `fail-fast: false` (per-OS failures surface
          independently), `python-version: ["3.12"]` (extensible axis;
          floor is 3.11), `timeout-minutes: 30`.
    - [x] `actions/checkout@v4` with **`fetch-depth: 0`** — hatch-vcs derives
          `__version__` from full git history (no tags yet → `0.1.devN+g<hash>`);
          a shallow clone breaks `uv sync`'s editable build.
    - [x] `astral-sh/setup-uv@v8` (`enable-cache`, `python-version` → `UV_PYTHON`);
          install via **`uv sync --locked`** against a now-**committed `uv.lock`**
          (un-ignored) for reproducibility (see decision log).
    - [x] Linux installs the PySide6 offscreen Qt libs the GitHub image lacks
          (`libegl1 libgl1 libxkbcommon0 libdbus-1-3 libglib2.0-0`); `conftest.py`
          forces `QT_QPA_PLATFORM=offscreen` so no display is needed.
    - [x] least-privilege `permissions: contents: read`; safe `pull_request`
          trigger (no secret exposure to fork PRs).
    - [x] **cross-platform port** (the first real 3-OS run showed the app had
          never run on Windows/macOS): fixed every exposed bug — see the
          2026-06-16 M7-C1 decision-log entry. **Ubuntu + Windows are required
          and green** (`setup-uv` pinned to a full version `v8.2.0`; `uv`
          pinned to `0.11.21` to match the lockfile).
  - [ ] **C1c. macOS green** (`continue-on-error` for now, NON-blocking): the
        real-socket SeedLink integration tests (~15 files that run a live
        `SeedLinkWorker` QThread against a loopback fake server) hit a
        `Fatal Python error: Aborted` during GC on macOS-arm64 — obspy's
        blocking `receive_data` (C, GIL-released) does not unwind at teardown
        and a later test's GC aborts the interpreter. The QThread/worker
        retention added to `streaming_engine` + the worker-test harness
        (mirroring the HVSR M6-0 precaution) prevents the "QThread destroyed"
        variant but not the GC abort. Needs **real macOS hardware** to
        reproduce/diagnose (a recv-timeout on the obspy socket, or
        `pytest-forked` isolation, are the leads). Until then macOS runs +
        reports but does not block the gate.
  - [x] **C2. Tag release** (`.github/workflows/release.yml`): on tag `v*` (and
        `workflow_dispatch` for dry-run testing) build per-OS artifacts and
        publish to the GitHub Release. *Workflow + packaging authored
        2026-06-16; Linux artifact validated locally end-to-end; Windows/macOS
        validated via `workflow_dispatch` CI:*
    - [x] per-platform icons (`.ico`/`.icns`) generated from the master PNG by
          `packaging/make_icons.py` (committed under `packaging/icons/`); the
          spec selects per `sys.platform`. **macOS BUNDLE** added to the spec so
          the build emits a real `EchosMonitor.app` (the `.dmg` payload).
    - [x] **Linux → AppImage** (`packaging/linux/build_appimage.sh`):
          AppDir + `appimagetool --appimage-extract-and-run` (no FUSE).
          Validated locally: the `.AppImage` runs `--version` + `--check` green.
    - [x] **Windows → Inno Setup installer + portable `.zip`**
          (`packaging/windows/echosmonitor.iss`; `choco install innosetup`).
    - [x] **macOS → `.dmg`** (`packaging/macos/build_dmg.sh`, `hdiutil`,
          .app + `/Applications` symlink). Unsigned (signing is M7-D).
    - [x] M7-E packaged smoke folded in: `scripts/build.{sh,ps1}` run the
          `--check` headless start/quit after every build, so a broken bundle
          fails the release job, not the user.
    - [x] `publish` job (softprops/action-gh-release) runs only on a real `v*`
          tag; `workflow_dispatch` stops after `build` (artifacts downloadable
          from the run) so the pipeline is validated before the first tag.
          Least-privilege: `contents: read` default, `contents: write` only on
          the publish job.
- [ ] **D. Signing** (open question 6): Windows code signing and macOS
      notarization need certificates/Apple account — ship unsigned first
      with documented Gatekeeper/SmartScreen instructions; wire signing as
      optional CI secrets. *Carry-forward (M7-C2 reviewer): thread the
      resolved version into the macOS `.app` `Info.plist`
      (`CFBundleShortVersionString`, currently PyInstaller's `0.0.0` default)
      while here — notarization touches the plist anyway.*
- [x] **E. Packaged smoke test** *(done via M7-C2)*: every release `build`
      job runs the bundle's `--check` (headless config + main-window
      construct + exit-by-code) plus `--version`, embedded in
      `scripts/build.{sh,ps1}`, so a broken bundle fails the release job, not
      the user. Exit-code based so it works on the Windows GUI-subsystem
      build that has no stdout.
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
| 2026-06-16 | M7-C2: `release.yml` triggers on **tag `v*` AND `workflow_dispatch`**; the `publish` (GitHub Release) job is gated on the tag, so a manual dispatch builds + uploads artifacts WITHOUT cutting a release. Per-OS artifacts: Linux AppImage (`appimagetool --appimage-extract-and-run`, no FUSE), Windows Inno Setup installer + portable zip (`choco install innosetup`), macOS `.dmg` (`hdiutil`, via a new spec `BUNDLE` → `EchosMonitor.app`). Icons (`.ico`/`.icns`) are committed, generated from the master PNG by `packaging/make_icons.py`. | The dispatch/tag split lets the whole pipeline be validated on all three runners before the irreversible first tag — the same de-risking the M7-C1 gate-first split bought (and exactly what avoided guessing here: the Linux AppImage was proven locally end-to-end, `--version` + `--check` green, but the Windows/Inno + macOS/dmg paths can only be exercised on their runners). AppImage over a raw tarball gives a single double-clickable file with no FUSE requirement on the build side; Inno over NSIS for a conventional Windows installer UX; `hdiutil` over `create-dmg` to avoid a brew dependency. Committing the derived icons (vs generating in CI) keeps the spec runnable directly (local + CI) with the PNG as the one source of truth and `make_icons.py` as the regenerator. Artifacts are unsigned — signing is M7-D, wired later as optional secrets so an unsigned release ships now. | 
| 2026-06-16 | M7-C1: **macOS is non-blocking (`continue-on-error`)**; Ubuntu + Windows are the required, green matrix. Also pinned `astral-sh/setup-uv@v8.2.0` (no moving `v8` tag is published) and the uv version to `0.11.21` (must match the lockfile generator, else `uv sync --locked` rejects it). | macOS-arm64 hits a `Fatal Python error: Aborted` during GC across the ~15 real-socket SeedLink integration tests: obspy's blocking `receive_data` (C, GIL-released) does not unwind at test teardown, and a later test's garbage-collect aborts the interpreter. It is not a EchosMonitor logic bug (Windows + Linux exercise the same paths green) but an obspy-on-macOS + threads + GC interaction, and it is not reproducible or safely fixable without real macOS hardware — chasing it blind would burn many ~6-min CI cycles with no local repro. Landing Linux + Windows green now ships the real value (the cross-platform port found genuine storage/durability/portability bugs); macOS keeps running + reporting under `continue-on-error` and is tracked as M7-C1c. The defensive QThread/worker retention added to the engine + harness stays (it is correct and pattern-consistent even though it does not resolve the GC abort). | 
| 2026-06-16 | M7-C1: the first real 3-OS CI run surfaced that **the app had never run on Windows/macOS**; fixed every exposed portability bug in-PR rather than deferring (user call). Real `src` bugs: (1) the MiniSEED writer's direct `_write_mseed` binding KeyError'd on Windows because an int32-WIDTH array there carries the `np.intc` scalar class, which is `==` by dtype but absent from obspy's `SAMPLETYPE` map — canonicalise to `np.dtype(np.int32).type` with a zero-copy `.view` only when it differs (Linux hot path untouched); (2) `os.fsync` on a **read-only** fd raises EBADF on Windows — the export + HVSR-report atomic writes reopened the just-written file `"rb"` then fsync'd; reopen `"rb+"`; (3) `QSettings(org, app)` uses the **Windows registry** (NativeFormat), which `setPath` can't redirect (test-isolation hole) and isn't portable — route all settings through `gui/qsettings_util.open_settings` (IniFormat file on every OS) + `setDefaultFormat(IniFormat)` at bootstrap. Test-only platform assumptions (the handoff's anticipated class): SDS path asserts compared `/`-literal strings (Windows renders `\`) → assert on `Path.parts`/`.parents`; dock-minimum asserts were `==` exact px (font-metric-dependent) → `>=` the design floor + a relative central-width check; the SO_LINGER RST in the fake server packed `"ii"` (POSIX `struct linger`) which fails on Windows' `u_short` layout → `"HH"` on win32; the seedlink worker-test harness now retains worker/thread pairs whose bounded join times out (obspy recv slow to unwind on macOS) so a later test's GC can't trigger a "QThread destroyed while running" abort (same precaution as the HVSR engines). | The product targets Windows/macOS desktops (M7); shipping a release whose science-critical storage path crashes on two of three platforms is not acceptable, and the bugs were real (encoding, durability-fsync, settings portability), not cosmetic. Fixing in-PR keeps the gate honestly green on all three OSes rather than merging a known-red tier-2. Storage changes preserve the Linux hot path (M6.5-C zero-copy) and were verified by the existing round-trip tests now running on Windows CI plus two new ones (obspy-key pin + uint round-trip). |
| 2026-06-16 | M7-C: split into **two PRs — PR gate (`ci.yml`) first, tag release (`release.yml`) next**; gate is a 3-OS matrix running the documented `ruff`/`mypy`/`pytest` gate | The cross-OS test surface (paths, keyring backends, offscreen Qt) is the real unknown and is independent of the installer work; landing the gate first turns those failures green before the heavier Inno/AppImage/.dmg build, and keeps each change PR-sized. Two workflow files (not one with tag-conditionals) keep the PR-push and tag-`v*` triggers independent and obvious. The gate is already green locally on Linux (ruff 0, mypy 0, 1206 passed), so `ci.yml` reproduces a known-good command set; the per-OS unknowns (paths, keyring, offscreen Qt) only surface on CI itself. `fetch-depth: 0` is mandatory: hatch-vcs reads full history (no tags yet) and a shallow clone breaks `uv sync`'s editable build. |
| 2026-06-16 | M7-C: **commit `uv.lock`** (un-ignore from the blanket `*.lock`) and install in CI with **`uv sync --locked`** | The M7 milestone header demands reproducible artifacts; a bare `uv sync` re-resolves from the index every run, so an unrelated transitive release (PySide6/numpy/scipy point bump) could turn the gate red on a PR that changed nothing — exactly the unrelated-flake class to avoid. Committing the lockfile pins the exact set across OSes and across time; `--locked` (not `--frozen`) additionally *asserts* the lock matches `pyproject.toml`, so editing a dependency without re-running `uv lock` fails the gate loudly instead of silently drifting. This is the standard application (vs library) posture; CLAUDE.md's bare `uv sync` commands still work locally. |
| 2026-06-16 | M7-B: packaging tool is **PyInstaller one-dir**, not Briefcase; build is a versioned `.spec` + `scripts/build.{sh,ps1}` | PyInstaller is the battle-tested path for a scientific PySide6 + obspy/scipy/hvsrpy stack — the contrib hooks cover PySide6 plugin bundling, and the obspy/keyring/IPython gaps are all expressible in the spec. One-dir (not one-file) gives faster startup, no per-launch temp extraction, and is the exact input the M7-C OS installers wrap (Inno/NSIS, AppImage, .dmg). Briefcase's native-installer story is cleaner but unproven against obspy's data files + ctypes-loaded C libs, which is where the real risk sits. The build surfaced four real bundling fixes, now encoded: obspy's relative frozen `__file__` (OBSPY_ROOT→CWD crash) needs a `inspect.getfile` runtime hook + collected `RELEASE-VERSION`; obspy's C libs (`mseed.cpython-*.so`) need `collect_dynamic_libs(..., search_patterns=["*.so",…])` since the default `lib*.so` matches none; hvsrpy's top-level `IPython.display` import and keyring's entry-point backends need hidden imports. A `--check` headless self-check is the packaged smoke (exit-code based — portable to the Windows GUI build that has no stdout). |
| 2026-06-16 | M7-A: version is **git-tag-driven via hatch-vcs**, not a hand-maintained literal; `__version__` is a 3-step fallback chain (`importlib.metadata` → generated `_version.py` → `"0.0.0+dev"`) | One source of truth (the tag) removes the "bump pyproject AND tag" double-book; hatch-vcs writes `_version.py` at build so a PyInstaller bundle that does not collect dist-info still reports a real version (M7-B reads it in the freeze). The metadata-first order keeps editable `uv sync` and metadata-collecting bundles authoritative; the literal `0.0.0+dev` only ever shows for a raw never-built source checkout. `hvsr_report.APP_VERSION` was a second hardcoded `0.1.0` — folded into `__version__` (its own test already required them equal). First tag `v0.1.0` is deferred to the release cut (M7-C/E), so dev shows `0.1.devN+g<hash>`. |
| 2026-06-16 | M6.6-A: map HVSR horizontals by **orientation code** (`N`/`1`→N, `E`/`2`→E), never by `sorted()` of the NSLC string | The bug (`models.py:350`) sorted full NSLCs so `…HHE` < `…HHN` put East into N — swapping the science inputs to hvsrpy on every GUI HVSR (live + archive), not just the label. The orientation char is already parsed (`parts[3][2]`); use it. f0 survives for symmetric horizontal combos (geom-mean/squared-avg) but directional readings were wrong. |
| 2026-06-16 | M6.6-B: persist the fetched StationXML in a **new `session_stationxml(session_id, device_name, xml_blob, fetched_at)` table** (schema v6), not in config or a sidecar file | Rule 14 scopes it to the session that recorded with it; rule 8 puts the write on the storage thread after fsync; the Archive tab + archive HVSR/decon read it back via `archive_reader` with zero live device calls. Config stays the connect-only truth (rule 15); a user `response_metadata` file still wins as an explicit override. Old DBs migrate via a no-op `CREATE TABLE IF NOT EXISTS` (M0-B precedent). |
| 2026-06-16 | M6.6-C: the "back off REST while SeedLink streams" **cadence (hard-skip vs slow heartbeat) is left to the implementing session** to decide with the `echos-rest-api` skill in hand and decision-log then | The trade is real (clock-health freshness vs device/LAN load) and best judged against the live device; the plan fixes only the policy shape (key off `ConnState.CONNECTED` + recent packets, resume full cadence on stall) and the schema-knob requirement. |
| 2026-06-16 | M6.6-C **resolved: SLOW HEARTBEAT (default 30 s), not hard-skip; full 3-GET snapshot retained at the slower cadence** (schema knob `echos.poll_interval_streaming_s`) | Clock discipline (PPS/GNSS/NTP) is timestamp-trust and stays worth refreshing during a recording, so a heartbeat beats a full skip. Keeping the whole snapshot (status + seedlink + calibrate) at the slow rate — rather than dropping individual GETs — keeps `EchosDeviceSnapshot` coherent (no partial-update model) and is the lower-risk change; the device/LAN win comes from the 6× cadence drop (5 s→30 s). **Streaming is keyed off `ConnState.CONNECTED`** (the SeedLink socket-liveness proxy): on any drop/reconnect the device leaves the set and is made due immediately, so full-cadence polling resumes at once — exactly when REST matters (reboot vs hiccup). A CONNECTED-but-silent socket keeps the slow cadence; acceptable because a real stall transitions ConnState off CONNECTED. |
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

| 2026-06-12 | M4-A: position source priority is **override > StationXML > live GNSS** (`/api/status` `position`), source tagged on every result; lat/lon exactly 0/0 treated as absent | Rule 16 names StationXML canonical and the override as winner; the real-contract check (both devices, read-only) showed the firmware embeds a 6-decimal GNSS *snapshot* into StationXML — so live GNSS is a same-truth fallback for when StationXML is absent/unparseable/coordinate-less (e.g. document generated before first fix). 0/0 is the no-fix placeholder ("null island"), never a real deployment. |
| 2026-06-12 | M4-A: an unreachable/timeout StationXML fetch fails fast WITHOUT trying `/api/status`; protocol-level failures DO fall back | A dead host would only burn a second HTTP timeout on the same socket; an endpoint-specific failure (404, bad body) says nothing about `/api/status`. Pinned by `test_unreachable_device_fails_fast_without_status_fallback`. |
| 2026-06-12 | M4-A: `positionFailed` vocabulary = `EchosErrorKind` + **`"unavailable"`** (no position anywhere / no source at all); a no-REST no-override device is `unavailable`, not an error | "This device has no position" is honest domain state the Map tab must render (grey marker / absent), not a transport failure; widening the closed set beats overloading `protocol`. A failed refresh keeps the last known cached position — stale-but-labeled beats blank. |
| 2026-06-12 | M4-A: `configure` AND `refresh` bump the latest-wins generation (written to the worker even when the dispatch set is empty); `refresh_device` deliberately does not; `shutdown()` is terminal | Both reviewers: an empty/fully-cached configure left the in-flight sweep fetching removed devices (results discarded but network work wasted), and N rapid refreshes queued N un-superseded full sweeps (rule 5 — the only unbounded seam in the file). A single-device refresh bumping the global generation would discard every other device's in-flight result to save one duplicate fetch. Regression tests verified to fail pre-fix. |
| 2026-06-12 | M4-B (open question 4): the Map tab is a **pyqtgraph scatter in a local east/north metre frame** — no tile stack, no QtWebEngine, no new dependency | The fleet is a handful of nodes deployed metres-to-km apart for array work: the user needs *relative geometry* (who is where, how far — exactly what M5 consumes), not basemap context. pyqtgraph is offline-by-construction (field laptops) and is the stack every other tab ships; CLAUDE.md already prefers this. The frame is metres E/N of the positioned-device centroid, aspect-locked 1:1 so the on-screen shape IS the array shape; absolute lat/lon/elev/source live in the marker hover tip. Revisit web tiles only on a real field need, as an isolated optional widget. **Revised by M6.5-D** (the field need arrived — see the 2026-06-12 M6.5-D entry): a satellite RASTER layer now exists, still with no web-tile stack and no QtWebEngine. |
| 2026-06-12 | M4-B: position resolution runs from launch (configure on every configChanged), like the M1-C status poller | Rule 13's "nothing starts without the user" governs *acquisition* (the engine); positions are passive credential-less fleet metadata, the same sanctioned class as the status poller. The Map tab also works before any device is started — which is when the field user is placing instruments. |
| 2026-06-12 | M5-D: the array archive run roots at the Archive tab's **currently selected session** (pull-based, one shared reader for all checked devices); with no selection each device falls back to its live `engine.archive_root` | The single-station hand-off ctx (M3-E) is push-based and keyed to one device + interval; an array run spans N devices, and the selected session's root is the only honest reach into a CLOSED session's per-device SDS trees (rule 14). The user flow is explicit: pick the session in the Archive tab, then run the array over it. |
| 2026-06-12 | M5-A (open question 5): array windowing is **per-device independent** — each device accumulates its own gap-free disjoint windows (own `last_window_end` cursor); the common-window gate (accept only when ALL devices cover the same span) is a deferred optional toggle | Skill `hvsr-array`: independent windows degrade gracefully (a flaky device just contributes fewer windows; it can never collapse the whole array's throughput) and stay honest — curves remain comparable because the interval and the one shared settings panel are identical across stations. Common windows buy stricter comparability at the cost of throughput hostage-taking by the weakest device; revisit on a real field need and surface per-device rejection reasons then. |
| 2026-06-12 | M6-0: the array archive no-data outcome is the ASYNC signal `arrayArchiveNoData(id, searched_roots)` — no `arrayMeasurementStopped` (nothing ran to stop), and the handler's `""` return shrinks to the no-reader degenerate case | Moving the N-device slice onto the worker (auditor F1) makes the outcome inherently async; emitting `stopped` first would clear the widget's id and make it drop the no-data message, emitting it second would clobber the message with "Stopped.". The roots ride the signal because the message must say WHERE it looked (the M5 follow-up: a stale Archive-tab session selection used to read as bare "no data"). |
| 2026-06-12 | M6-0: during an archive cycle the worker OWNS the stations' accumulators; the engine/UI read only engine-side `_Station.n_windows_total`, and `set_window_override` + `_request_recompute` are pending-gated | The accumulators are filled in place on the worker (snapshotting N devices' window lists would double memory for nothing); the ownership convention plus the pending-first gate removes every cross-thread accumulator touch instead of blessing "GIL-safe" racy reads. Known cost: a second override during a post-slice pending recompute is dropped (warn-logged) — `slice_inflight` flag if it ever matters. |
| 2026-06-12 | M6-0: a cycle where EVERY device's slice read raised announces the per-device errors as an empty-results `arrayUpdated`, never `arrayArchiveNoData` | "No archived data in that range" when the truth is N I/O failures sends the user hunting for a time-range mistake that does not exist; the errors ride the result so the table's per-device status column shows the real cause (reviewer finding on the first M6-0 cut). |
| 2026-06-12 | M6-0: both HVSR engines' shutdown RETAINS abandoned worker/thread pairs whose bounded join timed out (drain retries on the next shutdown; no cap, count logged) | Dropping the last Python reference to a running QThread is a hard Qt abort (destroyed-while-running) — the mutation run literally crashed the interpreter. A cap is impossible without dropping a running reference, so the logged `abandoned=` count is the rule-5 observability compromise. |
| 2026-06-12 | M6 clock health: `ClockHealth` derives from the `/api/status` BOOLEANS only; `time_sync_type` is display-only; `time_synchronized` alone = **HOLDOVER** (attention), never NTP | The firmware's sync-type string is an unpinned composite ("RMC+PPS+NTP" on fw 1aa72cbe) — branching on it would break on the next firmware wording. And a clock that was set once but has no live source is a free-running ESP32 crystal (seconds/day drift): reporting it as "NTP, network accuracy" is exactly the false-"synchronized" the model promises never to emit. |
| 2026-06-12 | M6 discovery: mDNS advert is only a PREFILTER (loose substring/prefix on instance `ADS131M04` / TXT `board=ESP32*`); the typed public probe (`/api/status` + `/api/seedlink/config`) is the gate; both pinned LIVE against echos.local (fw 1aa72cbe) | The instance/TXT are firmware constants the next build could reword (and mDNS conflict-renames duplicates "name (2)"), so exact-matching would be brittle; a schema-validated credential-less probe cannot be spoofed by a printer and can never trip the auth lockout (rule 15). pihw.local proved the negative case: no `_http` advert → manual add stays first-class. |
| 2026-06-12 | M6 discovery: `zeroconf` is a REGULAR dependency; the milestone's "optional" is honoured at the FEATURE level (lazy import → `discoveryFailed("unavailable")` on stripped installs) | An optional extra would keep the default `uv sync` gate from ever exercising the discovery tests; zeroconf is pure-python with one tiny dep (ifaddr), so the packaging cost is nil while testability wins. |
| 2026-06-12 | M6 discovery: the add-device prefill uses the mDNS HOSTNAME (`echos.local`), not the resolved IP, as `DeviceConfig.host` | The hostname survives DHCP lease changes — the exact field failure mode of a seismometer that sits on a site LAN for weeks; the probed IP remains visible in the dialog row for debugging. |
| 2026-06-12 | M6 wizard: performs NO device writes — "set admin password" means STORE in the OS keyring (off-thread, bounded 15 s, accept-with-warning on timeout/close); changing the password ON the device stays in the device dialog | `POST /api/auth/password` is still unexercised on real firmware (M1 closure) and needs the CURRENT password anyway; the wizard's job is to leave a working config + credential, not to mutate a factory-fresh device. The device write lands BEFORE the credential wait, so no close path can lose it (persisted-before-announced). |
| 2026-06-12 | M6 wizard: the worker thread starts LAZILY on the first page action; an undriven wizard owns no running thread | A wizard constructed but never driven (Help-menu open + patched/closed exec) never reaches done()/teardown — an eagerly-started thread then gets GC'd while running, a hard Qt abort (crashed the menubar tests for real). Queued events posted before start are delivered when exec() runs, so laziness costs nothing. |
| 2026-06-12 | M6 settings: `ui.theme` scope is the PLOTS (pyqtgraph background/foreground at bootstrap), not the widget chrome; all settings labeled "next launch" | Restyling every Qt widget is a large fragile surface for no field value (the chrome follows the system); pyqtgraph reads its config at item creation so a runtime switch would leave existing plots in the old colors — honest "next launch" beats a half-applied hot toggle. `ui.recent_detections_limit` stays out of the dialog until the M3 prefill that consumes it lands. |
| 2026-06-12 | M6.5-A: the archive seam has **no engine-side queue and no drop point** — `_on_packet` posts each recorded packet straight to the storage thread; rule 5's bound becomes an **in-flight gauge** (sent − writer-acked) warn-logged + `archiveBackpressure`-signalled above `queue_max` (field kept, repurposed as the warn threshold) | The old bounded deque was drained only by the flush tick and the drain emitted per-entry anyway — pure added latency plus a drop hazard; a replay burst starves the tick (Qt posted-event flood) and drop-oldest ate 33 s of LIVE recorded data in the field. The archive is the science sink (ROADMAP A: correctness > liveness; rule 11 protects display, not this path); the physical bound is the device ring + live rate, and sustained writer slowness still trips the writer's own slow-IO pause valve. `DeviceStatus.archive_drops_total` removed (no readers; nothing can drop there now). |
| 2026-06-12 | M6.5-B: gap-detector jitter tolerance is **absolute milliseconds** (`archive.jitter_tolerance_ms`, default 10, half-sample floor), and in-tolerance packets are **snapped onto the reconstructed grid** before archiving (`last_end` follows the grid, not the device stamps) | The field jitter is device clock wobble — a time property (±5.1 ms on echos.local), so a sample-scaled tolerance would be wrong at other rates and "≤ half a sample" (the ROADMAP sketch) was already in place and demonstrably too small. Snapping (not just suppressing the event) is what fixes the REAL damage — 17 on-disk segments/440 s that fragment reads and HVSR windows; `last_end` following the grid is what stops zero-mean jitter from emitting pairs forever. Honest cost, documented in code + schema: a real discontinuity ≤ tolerance is absorbed as ≤ 10 ms persistent timing bias (no `gaps` row) until the next over-tolerance event re-anchors — inside the device's own stamping noise, and irrelevant to single- and multi-station HVSR (no cross-station phase coherence used). Display/DSP keep the raw stamps; only the archive branch mutates, after every other consumer captured its values. |
| 2026-06-12 | M6.5-C: the writer binds obspy's `_write_mseed` **directly** (one import) instead of `Stream.write(format="MSEED")`, and skips the per-packet `trace.copy()` on the no-cast hot path | The plugin dispatch inside `Stream.write` resolves the MSEED entry point via importlib.metadata on EVERY call — ~3 ms + an email-header parse per 108-sample packet, 54 % of the writer's CPU in the M6.5-C profile. `_write_mseed` IS the function the entry point resolves to, it accepts BytesIO, and it never mutates the input trace; the round-trip tests pin the private binding so an obspy upgrade fails the gate loudly. Per-packet records stay (crash-tight write-on-arrival; coalescing's ~4.7× size win is not worth the in-memory crash window until the field says size hurts). |
| 2026-06-12 | M6.5-D (revises M4-B / open question 4): the Map tab gets a **satellite raster layer** — Esri World Imagery XYZ tiles fetched by a `core/map_tiles.py` worker (httpx, ≤36 tiles/batch, 10 s/tile, latest-wins generation, disk-cached) and drawn as pyqtgraph ImageItems under the scatter; **still no QtWebEngine, no slippy-map stack** | The field run produced the real need M4-B deferred to: siting context when placing instruments. A static per-extent raster keeps every M4-B virtue (offline-by-construction once cached, no new GUI stack, relative geometry intact — tiles are placed through the same `local_east_north`, sub-metre mismatch at array zooms) while QtWebEngine+Leaflet remains the documented fallback only if imagery needs ever outgrow this. Source/terms: Esri World Imagery public tile endpoint; attribution ("Esri — Esri, Maxar, Earthstar Geographics, and the GIS User Community") is rendered whenever the basemap is on, and a failed batch shows an honest "imagery unavailable" note instead. Contract amendments: rule 2 networking list + rule 8 sanctioned-cache exception (cache is an accelerator, never truth — fsync'd atomic writes, undecodable entries evicted + refetched). |
| 2026-06-13 | M6.5-F (revises M6.5-D's "static patch per array extent"): the satellite basemap **follows the viewport** — pan/zoom debounce-refetch the visible region's tiles, kept in an LRU (cap 96) with per-zoom zValue layering | M6.5-D fetched one fixed batch around the array; the field user panned/zoomed and nothing updated ("la mappa non si aggiorna"). Following the viewport is what a map is expected to do; it stays within the M4-B/D spirit (no QtWebEngine, no slippy-map *library* — just our own bounded tile fetches over a worker) because each gesture is one debounced ≤36-tile batch, disk-cached, and the item count is LRU-bounded. The inverse projection (`east_north_to_latlon`) is the new pure primitive; the degenerate-view rescue now recentres only on toggle/origin-change so a deliberate pan to the surroundings is never undone. The widget self-cleans on closeEvent (the debounce timer could otherwise spawn a tile thread mid-teardown — caught as a test interpreter-abort). |
| 2026-06-12 | M6.5-E fix: the map viewport **rescues itself onto the basemap extent** when imagery is requested or arrives into a degenerate (≤2 m span — covers both the auto-range-collapsed single-marker view and the never-ranged `[0,1]` default) or non-intersecting view; `Fit view` floors each axis at 50 m independently; a healthy user viewport is never touched | The first real Satellite toggle showed NOTHING: one positioned device is a lone point at the frame origin, pyqtgraph auto-ranges to ~1e-153 m at PAINT time (i.e. AFTER request-time checks — hence the second rescue on tile arrival), the pixel-sized marker still renders but data-sized tiles cannot. Landmine for the next reader: pyqtgraph's auto-range can undo your view fix between your check and the next paint; rescue at the consumer (tile arrival), not only at the producer. Orientation/placement verified by an offscreen render-grab of real site imagery + a north-red/south-blue render-sampling test. |
| 2026-06-12 | M6.5-A: MseedWriter **terminal-signal invariant** — exactly one `writeOk` XOR `writeFailed` per `write_trace`; the paused-path drop emits `writeFailed` (was silent), the pause-TRIP write emits only its `writeOk` (the old `writeFailed("filesystem unresponsive")` on the success path is gone) | The in-flight gauge counts terminal acks against sends; a silent drop inflates the gauge forever (false backpressure), a double terminal injects spurious acks that mask real backpressure exactly when the filesystem struggles (both reviewers flagged the latter). The teardown close barrier logs inflight + elapsed (rule 7) since its wait now scales with the backlog. |

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
4. ~~M4: tile stack choice (offline requirement? QtWebEngine weight?)~~
   **Resolved 2026-06-12: pyqtgraph scatter, local E/N metre frame, no
   tiles, no QtWebEngine** (see decision log).
5. ~~M5: common-window vs per-device windows for array HVSR (see M5-A).~~
   **Resolved 2026-06-12: per-device independent windows** (see decision
   log; the common-window gate is a deferred optional toggle).
6. M7: signing — is a Windows cert / Apple Developer account available, or
   do we ship unsigned with documented bypass instructions?
