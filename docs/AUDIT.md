# AUDIT.md — kickoff baseline & gap analysis (2026-06-10)

Read-only audit of the inherited codebase against ROADMAP.md. No `src/` or
`tests/` files were modified. All line numbers refer to commit `a1b0f6a`.

---

## 1. Baseline gate results

| Step | Command | Result |
|------|---------|--------|
| Install | `uv sync` | OK (Python 3.12 venv) — **but does NOT install dev tools**: ruff/mypy/pytest live in the optional `dev` extra, so `uv run ruff` fails with "Failed to spawn". Gate was run after `uv sync --extra dev`. |
| Lint | `uv run ruff check src tests` | **FAIL** — `E902 No such file or directory: tests` (the directory does not exist). `uv run ruff check src` alone: **clean**. |
| Types | `uv run mypy src` | **FAIL** — 3 errors, all `unused-ignore` on `QMetaObject.invokeMethod(...)  # type: ignore[call-overload]` at `core/streaming_engine.py:739, 1940, 2000` (PySide6 6.11.1 stubs no longer need the ignore). 82 files checked otherwise clean under `--strict`. |
| Tests | `uv run pytest -q` | **No tests exist.** `testpaths = ["tests"]` finds nothing; 0 collected. |

Git state: single commit `a1b0f6a "Init"` (source only). Untracked:
`.claude/`, `.gitignore`, `CLAUDE.md`, `ROADMAP.md`. No `.pyc` tracked.

### Baseline deviations from what CLAUDE.md/ROADMAP assume

1. **There is no test suite.** CLAUDE.md's gate, the `perf`/`ai` markers,
   "prune AI tests" (M0-C), and the `test-guardian` agent all presuppose
   tests that were not carried over from the original project. M0-C must be
   re-scoped from "prune AI tests" to "create the `tests/` scaffold".
2. **`docs/POSTMORTEMS.md` does not exist** (no `docs/` at all). Code
   comments cite specific entries ("POSTMORTEMS 2026-05-09b",
   "2026-05-10 Flaky multi-device tests resolved", e.g.
   `core/streaming_engine.py:1998`) that point nowhere. **Blocker for the
   agents** (CLAUDE.md rule 10 and the `qt-concurrency-auditor` rely on it):
   either carry the file over from the original repo or start a fresh one
   seeded from the four landmines already summarized in CLAUDE.md rule 10.
3. `pyproject.toml:4` description still reads "…with AI" — touched by both
   M0-A and M0-B.
4. Stale `__pycache__` files exist for deleted modules (`core/replay`,
   `gui/widgets/archive_browser`, `gui/widgets/replay_view`) — harmless,
   untracked, ignore.

---

## 2. Per-milestone gap analysis

### M0-B — AI removal map (complete)

**Files to DELETE entirely**

- `src/seedlink_dashboard/ai/` (whole package: `__init__.py`, `base.py`,
  `domain.py`, `state_store.py`, `agents/` with 4 agent modules)
- `core/ai_engine.py`
- `gui/widgets/ai_panel.py`
- `gui/dialogs/engage_agent_dialog.py`
- `storage/event_persister.py`

**Files to EDIT**

| File | Lines | What |
|------|-------|------|
| `gui/main_window.py` | 43, 61 | AI imports |
| | 259–264 | `AIEngine` construction |
| | 482–484, 525 | `AiPanel` construction + handlers + tab add |
| | 494 | `archive_tab.aiRequested.connect(self._handoff_archive_to_ai)` |
| | 1237–1238 | `aiAnnotation` → detection table/markers wiring |
| | 1653–… | `_handoff_archive_to_ai` (whole method; `setCurrentWidget(self._ai_panel)` at 1662 is inside it) |
| | 1831–1988 | `_open_engage_agent_dialog` + `_open_archive_agent_dialog` (lazy AI imports inside) |
| | 2151 | `self._ai_engine.shutdown()` |
| `gui/widgets/archive_tab.py` | 166 | `aiRequested = Signal(str, object, float, float)` |
| | 314–315, 326, 336, 432, 629, 646 | `_ai_button` ("Run AI agent on this window") + enable/emit wiring. **Caution**: none of these identifiers match the acceptance grep — they must be removed deliberately, or a live button would call a deleted method while the grep still passes. |
| `core/streaming_engine.py` | 72 | `EventPersister` import |
| | 522–526, 738–752 | `_event_persister` field + shutdown handling (one of the 3 mypy `unused-ignore` sites dies here) |
| | 969–1009 | `record_ai_detection` |
| | 1011–1046 | `attach_event_persister` + `event_persister` property |
| `config/schema.py` | 276–307 | `PersistOnDetectionConfig` (**missed by roadmap** — separate class from `AiConfig`) |
| | 309–323, 384 | `AiConfig` + `ai:` field on `RootConfig` |
| `config/__init__.py` | 7, 27 | `AiConfig` re-export (**missed by roadmap**) |
| `storage/dao.py` | 50, 505–608 | `Event` dataclass, `record_event` / `events_for_detection` / `recent_events` / `_row_to_event` — only callers are AI; remove (rule 12 "remove the mechanism too"). `Event` does not match the acceptance grep — remove deliberately. |
| `storage/db.py` | 31, 141–178, 180, 239–252 | keep `SCHEMA_VERSION = 3` and the v2→v3 `events` migration **as a no-op stub** so old DBs' version history stays linear (per roadmap); drop the `_EVENTS_DDL` body **and** its concatenation into `_CREATE_SCHEMA_SQL` at line 180 (otherwise fresh installs still create `events`) |
| `config/default.yaml` + `src/.../config/default.yaml` | ~50–69 | `ai:` section incl. `persist_on_detection` (**two copies** — the packaged one at `src/.../config/default.yaml` is the one actually loaded via `loader.py:38`; the repo-root copy is a reference duplicate) |
| `pyproject.toml` | 4, 39, 40, 87–90, 119, 120–123 | description, `ai` extra, `remote` extra, seisbench/torch mypy override, `addopts` `not ai`, `ai` marker (at 122) |

**What survives AI removal (verified)**

- STA/LTA detection path: `streaming_engine.py` ~1800–1835 writes via
  `dao.record_detection` and emits `detectionRecorded` — fully independent
  of `record_ai_detection`.
- `detection_table.py` / `detection_detail.py`: generic over the
  `detections` table `kind` field; no AI-specific logic; keep unchanged
  (the `sta_lta_only` filter becomes vestigial but harmless).
- `detections` DDL: stays (STA/LTA uses it).

**Collateral risk the roadmap missed**: the config schema is
`extra="forbid"` (`config/schema.py:26`), so an existing user
`config.yaml` containing an `ai:` section will fail validation after the
field is removed. Decide: strip unknown top-level `ai` key in the loader
with a one-time warning, or document the breaking change (proposed:
loader strips + warns once — cheap, honest, keeps "config has one writer").

### M0-A — rename map (everything beyond imports)

| Surface | Site | Current value |
|---------|------|---------------|
| QSettings org/app | `gui/main_window.py:86–87`, `gui/widgets/station_browser.py:92–93` | `"SeedLinkDashboard"` ×2 (stores window geometry, dock layout, column state only) |
| platformdirs data dir | `core/streaming_engine.py:1689` (`_resolve_db_root`), `:1900` (`_resolve_archive_root`), `core/ai_engine.py:593` (dies in M0-B) | `user_data_dir("seedlink_dashboard", "SeedTiLa")/archive` — **default archive + DB location** |
| platformdirs config dir | `config/loader.py:27,55` | `user_config_dir("seedlink-dashboard")/config.yaml` |
| Distribution name | `pyproject.toml:2` + version lookup `src/seedlink_dashboard/__init__.py:8` (`version("seedlink-dashboard")`) and late import `core/streaming_engine.py:1869` | `seedlink-dashboard` |
| Entry point | `pyproject.toml:52` | `seedlink-dashboard = "seedlink_dashboard.__main__:main"` |
| Packaging | `pyproject.toml:59` packages, `:62` force-include of `config/default.yaml` | `src/seedlink_dashboard` paths |
| importlib.resources anchor | `config/loader.py:38` | `resources.files("seedlink_dashboard.config")` |
| CLI prog name | `__main__.py:34` | `prog="seedlink-dashboard"` |
| structlog binding | `utils/logging.py:99` | `app="seedlink_dashboard"` |
| UI strings | `main_window.py:246,1043,1112–1113`; `first_run_wizard.py:185,190,564`; `hvsr_report.py:329` (PDF footer) | "SeedLink Dashboard" |
| Exception base | `core/exceptions.py:11` + subclasses 15/27/31/42 | `SeedLinkDashboardError` |
| Docstrings/comments | `__init__.py:1` ("with AI augmentation"), `loader.py:6`, `schema.py:34` | path docs |
| Package dir itself | `src/seedlink_dashboard/` → `src/echosmonitor/` | all imports |

**Storage-path consequence**: after rename, old data at
`~/.local/share/SeedTiLa/seedlink_dashboard/archive/` (SDS + `events.db`)
and old QSettings/config become invisible. Since this is a product refactor
with (presumably) no deployed user base and M2 re-roots archives by project
anyway, the proposal is **no migration: fresh paths, reset QSettings, log
once** (QSettings holds only window geometry/layout). Recorded in the
ROADMAP decision log; reverse if real archives exist in the field.

### M1 — Echos device management

- `DeviceConfig` (`config/schema.py:264–274`) has: name, host, port=18000,
  reconnect, selectors, dsp_chain, archive, response_metadata. **Nothing
  Echos**: no HTTP port, no credentials reference, no position override, no
  poll interval — the `echos:` sub-model is green-field as planned.
- `sanitize_device_name` (`storage/sds.py:43–88`) + the injectivity guard
  (`config/schema.py:388–416`, `_devices_map_to_distinct_archive_dirs`)
  exist and are the exact pattern rule 14 wants reused for project names.
- The status-poller template exists: `core/info_worker.py` (dedicated
  QThread, queued request slots, no internal queue, `stop()` with
  cancellation token, 2 s join timeout at `main_window.py:120`) — model
  `core/echos_status.py` on it as the roadmap says.

### M2 — session control

- **Autostart is one site**: `gui/main_window.py:432–434` —
  `if self._config.devices: self._engine.start()` (event
  `streaming_engine_autostart`). Removing it is trivial; the real M2-A work
  is that the engine lifecycle is **global** (`start()`/`stop()` for all
  devices, `streaming_engine.py:574–622`); per-device
  `start_monitoring/start_recording/stop` is a genuine engine-API change.
  Per-device machinery already exists internally (`_start_device`
  `:1051–1118`, per-device threads/bridges/status), so the split has a seam.
- Archive writers are created at `_start_device` time when
  `dev_cfg.archive.enabled` (`:1116–1117`, `_setup_archive_writer`
  `:1902–1950`) — i.e. **config-driven, not user-state-driven**; Recording
  must become the trigger.
- **Archive-root resolution sites** (kickoff said "at least three"):
  1. `streaming_engine._resolve_archive_root` (`:1888–1900`) — per-device
     `archive.root_dir` → `app.archive_root` → platformdirs;
  2. `streaming_engine._resolve_db_root` (`:1680–1689`) — same chain minus
     per-device, used for the detection-only DAO;
  3. `core/ai_engine.py:593` — independent platformdirs copy (deleted in
     M0-B).
  Plus the config-level knobs (`schema.py:30–37` app default null,
  `schema.py:109` per-device override) and the reader path, which receives
  the root as a snapshot string (`core/archive_detail_loader.py:322`) and
  can go stale across hot-reload. M2-B should funnel all of these through one
  session-rooted resolver.
- **A `sessions` table already exists** (`storage/db.py:52–58`:
  started_at, ended_at, host, version, config_hash). M2-B *extends* it
  (project_name + per-device membership, schema v4 migration) instead of
  creating it — roadmap corrected.

### M3 — Archive tab: "not completely functional", made precise

**Already implemented and working** (more than the roadmap implies):
device/station selection, `archive_extent`/`archive_coverage` DAO methods
(`storage/dao.py:653–674, 676–717`) **already wired** to a `CoverageStrip`
widget (`archive_tab.py:111–158`); static 3C window view + spectrogram via
off-thread `ArchiveWindowLoader`; unit switching
Counts/Vel/Acc/Disp through the deconvolution worker (`archive_tab.py:793–808`);
measurement cursors with Δt/frequency readout (`:713–743`); honest empty
states.

**Actually missing** (the real M3 work): session-centric browsing (no
session concept in the UI at all); per-interval MiniSEED/CSV export (only
HVSR CSV/JSON exports exist, `storage/hvsr_report.py:110–164`); PNG export;
re-indexer (`parse_sds_path` exists at `storage/sds.py:130–168`, explicitly
documented "for future re-indexers", zero callers); the AI button on the
tab (dies in M0-B). M3-B ("fix rough edges") is mostly *verification*
work, not construction.

### M4 — Map / positions

Green-field as assumed: no lat/lon/elev anywhere (not in `DeviceConfig`,
not in DB). `core/response.py` reads StationXML for **response only** —
the inventory-loading code is the natural seam for `core/positions.py` to
reuse, plus the M1 client's `GET /api/stationxml`.

### M5 — multi-device HVSR: concrete blockers

1. `HvsrEngine` holds exactly one `_Measurement`
   (`core/hvsr_engine.py:219`); start/stop are singleton.
2. `HvsrAccumulator.__init__` takes a single `device: str`
   (`core/hvsr.py:255`) — per-device accumulators are fine, so the array
   layer is "N accumulators + orchestration", as the `hvsr-array` skill
   prescribes; no physics re-implementation needed.
3. Widget binds one device/station (`hvsr_widget.py:156`), though
   `three_component_groups()` (`:113–139`) is already multi-device-aware.
4. `responses_identical` (`core/hvsr.py:718–745`) generalizes per-device.
No blocker beyond planned scope; positions dependency on M1/M4 confirmed.

### M7 — packaging-hostile spots

- `importlib.resources.files("seedlink_dashboard.config")` for the bundled
  `default.yaml` (`config/loader.py:38`) — needs the data file declared to
  the bundler (force-include exists for wheels; PyInstaller spec must add it).
- `importlib.metadata.version("seedlink-dashboard")` at import time
  (`__init__.py:8`) — fails inside bundles without dist metadata; needs a
  guarded fallback.
- matplotlib `Agg` forced lazily inside `write_hvsr_pdf`
  (`hvsr_report.py:228–232`) — good for headless, but matplotlib + hvsrpy +
  the IPython pin (`pyproject.toml:28–35`) inflate the bundle; hidden
  imports for IPython likely needed.
- qasync event-loop bootstrap (`__main__.py:73–74`) — standard, but pins
  the PySide6 plugin set.
- AI lazy imports (`ai/agents/__init__.py:45–66`) disappear in M0-B —
  after that, no *additional* dynamic-import sites remain beyond those
  listed above (`importlib.resources` in the loader, `importlib.metadata`
  at `__init__.py:5–8` and the late import at `streaming_engine.py:1869`).
- obspy reads only user-supplied files at runtime; but obspy itself ships
  data files PyInstaller often misses (known hidden-import work, as the
  roadmap anticipated).

---

## 3. Top 10 risks, ranked

1. **No test suite at all.** Every refactor (M0 rename included) lands
   blind. *Mitigation*: M0-C builds the `tests/` scaffold + smoke tests
   (import app, instantiate config, headless `QT_QPA_PLATFORM=offscreen`
   construct/teardown of MainWindow) before any behavioral milestone; gate
   enforced from then on.
2. **`docs/POSTMORTEMS.md` missing** while code and agents reference it.
   *Mitigation*: carry over from the source repo if available; else create
   seeded from CLAUDE.md rule 10's four landmines, before any thread work.
3. **Engine lifecycle split (M2-A)** touches `streaming_engine.py` (2000+
   lines, thread-heavy, the postmortem hotspot). *Mitigation*:
   `qt-worker-threading` skill + `qt-concurrency-auditor` on every diff;
   land split (per-device start/stop) separately from the Recording/
   Monitoring distinction.
4. **`extra="forbid"` config breakage** on AI removal for any existing
   user config. *Mitigation*: loader strips the `ai:` key with a one-time
   warning (decided in ROADMAP decision log).
5. **Rename silently re-points storage paths** (SeedTiLa → EchosMonitor).
   *Mitigation*: explicit decision (no migration, documented); if field
   archives exist, revisit before shipping M0.
6. **Hot-reload restart flow (M1/rule 15)** — 202 + `restart-status` poll +
   SeedLink reconnect is the most failure-prone new code. *Mitigation*:
   build against an `httpx.MockTransport` fake firmware with the simulated
   7-step restart from day one (roadmap acceptance already requires it).
7. **Credential storage** (keyring with file fallback) is
   platform-divergent and interacts with M7 bundling. *Mitigation*: isolate
   behind one interface; test fallback path in CI on all 3 OS (M7-F).
8. **Archive-root divergence during M2 transition** (three resolution
   sites + stale reader snapshots). *Mitigation*: introduce the single
   session-rooted resolver first, then migrate writers/readers onto it in
   the same milestone.
9. **mypy baseline is red** (3 unused-ignores). Trivial, but a red gate
   normalizes ignoring the gate. *Mitigation*: fix in the first commit
   (one dies with AI removal; the other two are one-line deletions).
10. **hvsrpy/IPython/matplotlib transitive weight** threatens M7 bundle
    size and hidden-import correctness. *Mitigation*: early local
    PyInstaller spike (M7-B already plans it); keep hvsrpy usage confined
    to `core/hvsr.py` (already true).

**Milestone order**: no change needed. M0→M1→M2 dependencies hold. Two
insertions: (a) M0-C now *creates* the test scaffold rather than pruning;
(b) POSTMORTEMS.md carry-over/creation becomes an explicit M0 task since
agents depend on it.

---

## 4. Proposed first change (one reviewable commit)

**M0-B first — AI removal — then M0-A rename as the second commit.**

Why B before A:

1. **Removal shrinks the rename.** Deleting `ai/`, `ai_engine.py`,
   `ai_panel.py`, `engage_agent_dialog.py`, `event_persister.py` removes
   one of the three platformdirs sites (`ai_engine.py:593`), two lazy
   import sites, the seisbench/torch mypy override, and the `ai` extra —
   all of which the rename would otherwise have to edit first and delete
   later.
2. **Deletion is mechanically verifiable without tests** (we have none):
   the acceptance is a grep
   (`grep -ri "seisbench\|torch\|phasenet\|ai_engine\|AiConfig" src tests`
   → empty) plus `ruff` + `mypy --strict` + an offscreen app-launch smoke.
   The rename, by contrast, benefits from having even minimal import-level
   tests in place; those arrive in M0-C right after.
3. **The rename is a single atomic sweep** (`git mv` + import rewrite +
   the 13-surface checklist in §2). Doing it on a smaller, AI-free tree is
   strictly less churn and a cleaner diff to review.

**Exact scope of commit 1 (`m0: remove ai subsystem (rule 12)`):**

- Delete the 5 files/1 package listed in §2 M0-B.
- Edit: `main_window.py` (incl. `aiRequested` connect at 494 and
  `_handoff_archive_to_ai`), `gui/widgets/archive_tab.py` (`aiRequested`
  signal + `_ai_button` wiring — not caught by the acceptance grep),
  `core/streaming_engine.py`, `config/schema.py`, `config/__init__.py`,
  `storage/dao.py` (`Event` + events methods, lines 50, 505–608),
  `storage/db.py` (no-op v2→v3 stub, drop `_EVENTS_DDL` body + the line-180
  concatenation, `SCHEMA_VERSION` stays 3), both `default.yaml` copies,
  `pyproject.toml` (extras `ai`+`remote`, marker, addopts, mypy override,
  description), loader strip of legacy `ai:` key with one-time warning.
- Fix the 2 surviving `unused-ignore` mypy errors
  (`streaming_engine.py:1940, 2000`) so the gate is green from commit 1.
- Verify: acceptance grep empty — run it on `src` only at this commit
  (`tests/` does not exist until M0-C); `ruff check src` clean;
  `uv run mypy src` clean; `uv sync` (no torch) + offscreen launch smoke
  by hand (no test suite yet).

M0-A (rename) is the next commit, then M0-C (tests scaffold + gate wiring +
POSTMORTEMS.md seed).
