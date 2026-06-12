# CLAUDE.md — EchosMonitor

EchosMonitor is a desktop monitoring/recording/analysis application for **Echos**
devices running the **`firmware_seedlink`** variant (ESP32-S3 node, 3 velocimeter
channels + optional HN1, SeedLink v3 server on TCP **18000**, HTTP REST API with
Basic Auth). It is a domain-specific refactor of a generic SeedLink dashboard.

**This file is the contract.** Read it before any change. ROADMAP.md holds the
milestone plan and current state; docs/POSTMORTEMS.md records failures we must
not repeat. Update both as you work.

---

## What EchosMonitor is (and is not)

- IS: an Echos-specific client — device fleet management (client **and** server
  side configuration via the Echos REST API), user-controlled acquisition
  sessions, project-named SDS archives, archive browsing by session, HVSR
  (single and multi-device/array), a device map.
- IS NOT: an AI app. **Rule 12 below — there is no AI subsystem.** Any code,
  config, dependency, test or UI referencing agents/seisbench/torch/phasenet
  must not exist in this repo.
- IS NOT: a generic SeedLink browser. Public servers (IRIS/GEOFON) may still be
  used in tests/fakes, but the product targets Echos nodes.

## Tech stack (locked)

- Python ≥ 3.11, `uv` for env + run. PySide6 + qasync (GUI/event loop),
  pyqtgraph (ALL live plotting — matplotlib only for offline PDF reports),
  obspy (SeedLink client, MiniSEED, response handling), numpy/scipy (DSP),
  hvsrpy (HVSR physics — never re-implement), pydantic v2 (config schema),
  structlog (logging), platformdirs, SQLite (metadata index), `httpx` (NEW —
  Echos REST client; async, timeout-bounded).
- Map tab: prefer a pyqtgraph/QtWebEngine-free solution if possible (e.g.
  offline tile rendering or a simple lat/lon scatter on pyqtgraph); if web
  tiles are required, QtWebEngine + Leaflet is acceptable but must be an
  isolated, optional widget (document the decision in ROADMAP).
- Lint/type/test gate: `ruff` + `mypy --strict` + `pytest` (pytest-qt,
  pytest-asyncio). `perf`-marked tests stay out of the default gate.

## Commands

```bash
uv sync                      # install (NO 'ai' extra — it must not exist)
uv run pytest                # default gate (excludes -m perf)
uv run ruff check src tests
uv run mypy src
uv run echosmonitor
```

---

## Architecture rules (numbered — code comments reference these numbers)

The inherited codebase references rules by number in docstrings (5, 7, 8, 9, 11
especially). **Do not renumber.** New Echos rules continue from 12.

1. **GUI thread is sacred.** No blocking I/O, no heavy compute, no file access
   on the GUI thread. Heavy work lives on dedicated QThread workers
   (parentless QObject + `moveToThread`, QueuedConnection both ways).
2. **Pure modules stay pure.** `dsp/`, `core/models.py`, `core/hvsr.py`,
   `storage/sds.py` contain no Qt, no I/O, no global state. Networking lives
   only in `core/seedlink_worker.py`, `core/info*.py`, `core/echos_api.py`
   and `core/map_tiles.py` (M6.5-D basemap fetcher; worker thread only).
   `__main__.py` only bootstraps.
3. **Config has one writer.** All runtime config mutations go through
   `ConfigStore` (validate → rotate backups → atomic write → emit
   `configChanged`). The engine hot-reloads via the diff path; never restart
   the whole engine for a config tweak that the diff can express.
4. **`core/` never imports `gui/`.** Cross-layer communication is Qt signals
   carrying frozen dataclasses / primitives. Type-erased `Signal(object)`
   payloads get `isinstance` guards on receipt.
5. **Bounded queues, drop-oldest, logged.** Every producer/consumer seam has an
   explicit bound; overflow drops oldest and logs at most once per 5 s per
   stream (`_DROP_LOG_INTERVAL_S` pattern).
6. **Structured logging only.** `structlog`, event-style names
   (`session_started`, `echos_config_push_failed`), key-value context. Never
   `print`, never f-string log messages.
7. **Every wait is bounded, observable, interruptible.** Start/done/elapsed
   logs around anything slow; cooperative `_stop` flags polled ≤100 ms;
   thread joins always with a timeout + warn log; latest-wins tokens for
   supersedable work.
8. **Persistence boundary.** All file/DB writes live in `storage/` and run on
   the storage QThread. The MiniSEED file is the source of truth; the SQLite
   DB is an index over it. DB rows are written only AFTER fsync
   (DB-after-fsync). Anything announced via signal is durable first
   (persisted-before-announced). Atomic writes: temp file in same dir →
   fsync → `os.replace`. Sanctioned exceptions, each on its own thread
   and following the same atomic recipe: ConfigStore (rule 3), the
   keyring fallback file (rule 15), and the `core/map_tiles.py` basemap
   cache (M6.5-D; an accelerator, never truth — evicted on decode
   failure).
9. **DAO field provenance.** Counters come from `COUNT(*)`/actual on-disk
   values at the call site, never from in-memory accumulators passed along.
10. **Tests + postmortems.** Every bug fix lands with a regression test and,
    if it cost >1 hour, a docs/POSTMORTEMS.md entry. Known landmines already
    recorded there: Qt queued slot cannot dispatch into a thread parked in
    `queue.get()`; `QThread.quit()` racing queued slots needs
    BlockingQueuedConnection barriers; worker `stop()` must outlive `run()`;
    GIL-holding reads on the GUI thread starve the SeedLink worker.
11. **Best-effort consumers never back-pressure science.** The data path is
    acquisition → DSP/detection → storage. Render, PSD, spectrogram, HVSR are
    pull-based best-effort consumers: they read ring-buffer snapshots
    (`read_recent`), skip/drop under load, and can never gate the flush tick.
    Display is peak-decimated above `ui.max_display_rate_hz`; science keeps
    full rate.

### Echos-specific rules (new)

12. **NO AI.** Delete and keep deleted: `ai/`, `core/ai_engine.py`,
    `gui/widgets/ai_panel.py`, engage/archive agent dialogs, `AiConfig`,
    `persist_on_detection`, the `ai` optional extra and `ai` pytest marker,
    every `seisbench`/`torch` mention. `detections` schema stays (STA/LTA
    uses it) but `record_ai_detection` and the events persister policy go.
    If a removal orphans a useful mechanism (e.g. `EventPersister`), keep the
    mechanism only if a non-AI feature uses it; otherwise remove it too.
13. **Nothing starts without the user.** The engine never autostarts on app
    launch. Acquisition has three explicit user states per device:
    **Idle → Monitoring** (live view, no disk writes) **→ Recording**
    (monitoring + SDS archive writes). Stop is always immediate (bounded by
    rule 7). The UI must make the current state unmistakable.
14. **Sessions are the archive unit.** A recording session has a user-chosen
    project name; the archive root for that session is
    `<archive_root>/<sanitized_project_name>/` (then the per-device SDS tree
    below it, unchanged). The metadata DB records sessions with project name
    + time span; the Archive tab lists/filters sessions by name and date.
    `sanitize_device_name`-style sanitisation applies to project names with
    the same injectivity guard.
15. **The Echos REST API is the single device-config truth for server-side
    settings.** Server-side config (OSR, gains, SeedLink port/ring/auth,
    StationXML profile, network) is read from and written to the device via
    `core/echos_api.py` — never duplicated into the YAML beyond what the
    client needs to connect (host, port, credentials reference, position
    override). Writes to `/api/seedlink/config` are hot-reload (202 + poll
    `restart-status`); the client must poll, surface progress, and reconnect
    SeedLink after the in-place restart. Credentials: HTTP Basic over plain
    HTTP on LAN — store the password in the OS keyring if available, else
    NVS-style local file with a loud warning; NEVER in the YAML, NEVER in
    logs. Respect the device lockout (429 + Retry-After): back off, never
    hammer.
16. **Device position is metadata, not guesswork.** Lat/lon/elev come from the
    device's StationXML (`GET /api/stationxml`) when available, with an
    optional per-device manual override in config. The Map tab and the
    multi-device HVSR consume one shared `DevicePosition` resolver.

## Module layout (target)

```
src/echosmonitor/
├── __main__.py            # bootstrap only
├── config/                # pydantic schema + loader (+ session config)
├── core/
│   ├── streaming_engine.py, seedlink_worker.py, ring_buffer.py, dsp_router.py
│   ├── echos_api.py       # NEW: typed async REST client for the Echos firmware
│   ├── echos_status.py    # NEW: poller (status/clients/ring usage) on worker thread
│   ├── session.py         # NEW: session state machine (Idle/Monitoring/Recording)
│   ├── positions.py       # NEW: DevicePosition resolver (StationXML + override)
│   ├── hvsr.py, hvsr_engine.py, hvsr_array.py   # hvsr_array NEW (M5)
│   └── ...
├── dsp/                   # unchanged, pure
├── storage/               # SDS writer/reader, DAO, sessions index, reports
├── gui/                   # main window, Map tab NEW, Session toolbar NEW
└── utils/
```

## Workflow with Claude Code

- Work milestone-by-milestone from ROADMAP.md; one stage = one PR-sized change.
- Before coding a stage: restate its acceptance criteria, list files touched,
  run the relevant skill (`.claude/skills/`) — especially `echos-rest-api`
  before touching device communication and `qt-worker-threading` before
  touching any thread.
- After coding: run the full gate, have the `code-reviewer` agent review the
  diff, the `qt-concurrency-auditor` review anything that touched
  threads/signals, then update ROADMAP.md (check the boxes, note decisions).
- Never delete a failing test to make the gate pass; fix or consciously
  rewrite it with justification in the commit message.
- Commits: imperative, scoped (`m1: echos rest client — config read path`).
