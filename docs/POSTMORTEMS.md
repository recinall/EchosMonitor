# Postmortems

> Carried over verbatim from the original seedlink-dashboard project
> (M0-C, 2026-06-10). Entries referencing the AI subsystem (agents,
> AIEngine, persist-on-detection) describe code that was removed in M0-B
> (CLAUDE.md rule 12); their threading/lifecycle lessons still bind.
> Module paths in old entries use the pre-rename `seedlink_dashboard`
> package name.

A living log of bugs, races, and incidents that consumed real debugging
effort. Each entry exists so the next person who hits a similar
symptom can find the precedent quickly — and so the lessons survive
beyond the commit message that fixed them.

## How to write a postmortem

Add a new section under "Entries" in reverse-chronological order
(newest at the top). Use the four-line format:

- **Symptom** — what an operator (or test runner, or CI log) actually
  saw. Be concrete: error string, exit code, time-to-failure, frequency.
- **Root cause** — the underlying invariant that was being violated.
  Resist the urge to redescribe the symptom in different words; if you
  cannot articulate the broken invariant in one sentence, you have not
  found the root cause yet.
- **Fix** — the change that restored the invariant. Reference the
  commit hash and the file(s) touched. If the fix has belt-and-braces
  layers, list them in order.
- **Lesson learned** — the rule the codebase should now follow so this
  class of bug stays dead. Phrase it as a positive invariant ("X must
  hold before Y"), not a "don't do Z" — easier to enforce in review.

Write postmortems for any of these:

- Race conditions, segfaults, or non-deterministic test failures.
- Production-affecting bugs that took more than ~1 hour to diagnose.
- Subtle protocol or library quirks (especially obspy / Qt / asyncio)
  whose behaviour surprised us and is likely to surprise the next reader.
- "Why is this code shaped like that?" mysteries that the answer is
  load-bearing for. (Better here than as a 30-line code comment.)

Skip:

- Routine bugs whose fix is obvious from the diff.
- Style / naming churn.
- One-line typo fixes.

Cross-link relevant code via `path:line` and the postmortem entry's
heading slug from comments that reference an invariant defended here.

## Entries

### 2026-06-18 — Stall watchdog cried wolf: an in-process GIL freeze read as a "silent" device

- **Symptom** — v0.1.2 logged `seedlink_stream_stalled` (`silent_s` ~5.2–5.8,
  `threshold_s` 5.0) "regularly", reported first as happening mainly during
  HVSR analysis, then also without it. No data was actually lost; the live
  stream recovered on its own each time (`seedlink_stream_resumed`).
- **Root cause** — the watchdog stamped each stream's last-arrival time in
  `_on_packet` and scanned for silence in `_scan_stalls`, BOTH on the
  GUI/engine thread. A multi-second GIL-holding compute — the hvsrpy/numba
  HVSR re-compute is the textbook case, but any heavy in-process work
  (deconvolution, a GC pause) does it — freezes the engine thread, so packet
  processing AND the scan stall together. The device kept sending and the OS
  socket buffer held the data (obspy drained it with no loss once scheduled),
  but the last-packet timestamps went stale, so the next scan computed a >5 s
  gap and flagged a stall that never happened. Exactly the CLAUDE.md rule 10
  landmine ("GIL-holding work starves the data path"), seen for the first time
  only now because the v0.1.0/v0.1.1 binaries shipped an empty obspy plugin
  registry (see 2026-06-17) and delivered zero packets — v0.1.2 is the first
  build where SeedLink data and the watchdog ran together. The ~237 s period
  between warnings was a red herring: it was just the HVSR re-compute cadence.
- **Fix** — `core/streaming_engine.py::_scan_stalls` now reads its OWN
  scheduling delay (`now - prev_scan`). If a scan ran more than
  `_STALL_SCAN_STARVED_S` (2.5 s) past its ~1 Hz cadence, the engine thread was
  frozen, not the network — so it rebases every stream's liveness clock to now,
  logs `stall_scan_starved`, and forgives the round instead of blaming the
  device. A genuine network silence leaves the loop ticking on time and is
  still flagged. The `prev_scan == 0.0` never-scanned sentinel skips both the
  throttle and the guard (first scan / tests). Regression tests:
  `test_stall_watchdog_forgives_process_wide_starvation` (frozen scan → no
  flag, liveness rebased) and `test_stall_watchdog_flags_silence_when_scan_loop_is_healthy`
  (on-cadence scan + real silence → still flagged).
- **Lesson learned** — a watchdog that lives on the same thread it monitors
  cannot tell "the world stopped" from "I stopped looking". Any
  same-thread liveness check must cross-check its own scheduling delay before
  trusting an elapsed-time measurement. The deeper cost — the live plot and
  data path genuinely stutter while hvsrpy holds the GIL — was unaddressed by
  the watchdog fix; the real fix is to run the GIL-bound hvsrpy compute in a
  subprocess. **Done 2026-06-18** (`core/hvsr_compute.py`): both HVSR engines
  now run `accumulator.compute()` in a persistent spawn child via a
  `SubprocessHvsrComputeClient`, so the worker thread only blocks on a pipe
  `poll` (GIL released) while numba runs out-of-process — the GUI render and
  the SeedLink worker keep scheduling. A bonus property: the compute is now
  genuinely interruptible (a cancel `terminate()`s the child), where the
  in-process numba could only be abandoned mid-JIT. The frozen-bundle spawn
  path is gated by `--check` (a real subprocess compute round-trip, the same
  CI-catches-it discipline as the obspy-metadata smoke).

### 2026-06-17 — Packaged obspy had an empty plugin registry: no copy_metadata("obspy")

- **Symptom** — in the released v0.1.0/v0.1.1 desktop binaries (but never in a
  dev `uv run` checkout): `archive_reader_read_failed error='Format "MSEED" is
  not supported. Supported types: '` (the supported-types list **empty**),
  `echos_stationxml_unparseable`/`position_stationxml_unparseable
  error_type=TypeError`, and live recording stuck at **0 packets** with the
  stall watchdog firing on `expected_interval_s=0.0` despite "1 station(s)
  accepted". Archive windows loaded empty.
- **Root cause** — obspy discovers EVERY reader/writer (MiniSEED, StationXML, …)
  through its distribution **entry points** (`importlib.metadata`). The
  PyInstaller spec collected obspy's data files and submodules but NOT its
  dist-info, so the frozen app's plugin registry was empty. With no MiniSEED
  plugin, `obspy.read(format="MSEED")` (archive), `read_inventory(
  format="STATIONXML")` (positions + device dialog) AND the SeedLink client's
  decoding of incoming MiniSEED records all failed — the last silently, so the
  worker connected, subscribed, and received zero parseable packets.
- **Fix** — `datas += copy_metadata("obspy")` in `packaging/echosmonitor.spec`
  (commit 1c7cc50). `collect_data_files`/`collect_submodules` do not bring entry
  points; only `copy_metadata` does. Belt-and-braces: the `--check` smoke in
  `__main__._obspy_io_self_check` now round-trips MiniSEED + StationXML through
  the plugin registry, so a freeze with empty obspy metadata fails CI's release
  build (per-OS) instead of the field. Shipped as v0.1.2.
- **Lesson learned** — any dependency whose features resolve through
  `importlib.metadata` entry points (obspy, keyring backends, pluggy-style
  systems) needs `copy_metadata(<dist>)` in the freeze, not just its modules
  and data. And a packaged smoke must EXERCISE the bundle's real IO paths
  (read/write a file in each format the app uses), not merely import-and-
  construct — `--check` building the main window proved nothing about obspy's
  registry, which is why this shipped twice.

### 2026-06-06 — Deconvolution amplified sub-corner noise: broadband pre_filt on a geophone

- **Symptom** — The physical-units VELOCITY trace for a 4.5 Hz velocimeter
  (EG-4.5-II) showed a dominant slow low-frequency lobe absent from the COUNTS
  trace, as if the counts were treated as ACCELERATION and integrated once. It
  *looked* like a wrong response / one extra integration.
- **Root cause** — The response was **correct** (StationXML: input M/S,
  2 zeros at origin, poles → |pole|/2π ≈ 4.5 Hz; ObsPy did exactly one
  division by the velocity response — VEL/counts is flat above f0). The bug
  was `core/response.py::default_pre_filt`: it hard-coded the LOW
  stabilisation corners at 0.005/0.01 Hz — a broadband-seismometer value
  ~2.7 decades below this geophone's corner. `remove_response` then faithfully
  inverted the geophone's ω² roll-off across the entire sub-corner band,
  amplifying noise/drift by up to ~(4.5/0.01)² ≈ 2×10⁵ into the LF lobe. So
  the symptom was *faithful 1/ω² inversion of the roll-off across a band that
  should have been excluded*, not over-integration. Device-independent (both
  devices shared the response and the broadband default).
- **Fix** — Derive the low pre_filt corners from the instrument's own corner
  frequency (`f0 = smallest non-zero |PAZ pole| / 2π`): `low_stop = f0/2`,
  `low_pass = f0`; keep the fs-based anti-alias high corners. Falls back to the
  broadband default (logged) when no PAZ pole exists. Optional per-device
  `response_metadata.pre_filt` override wins when set. Pure-module change;
  counts byte-faithful (rule 8), `streaming_engine.py` byte-identical, live
  path untouched. Real-data evidence: VEL LF/HF energy ratio 1.79 → ~0.
- **Lesson learned** — A deconvolution stabilisation band is an INSTRUMENT
  property, not a global constant: a "looks over-integrated" LF excess can be
  a *correct* response inverted across a band the sensor never resolved. Tie
  the low corner to the response's corner frequency. The regression test
  asserts the user's observable — VEL ∝ counts (flat |VEL|/|counts|) across the
  passband and LF/HF comparable to counts — and is proven to fail on the old
  broadband pre_filt (forced fallback → LF/HF explodes).

### 2026-06-05 — Two devices with identical SEED station codes shared one SDS file

- **Symptom** — Two configured devices (`Echos` and `Echos_WK`), both emitting
  `XX.ECHOS.00.HHZ/HHN/HHE` with `archive.root_dir=null`, wrote into the **same**
  physical MiniSEED files. The metadata index reported a single merged
  extent/coverage for the pair instead of one per device. Surfaced while
  diagnosing an H2 disconnect: enabling archiving turned on the gap detector,
  whose per-stream view made the collapsed attribution visible. (The disconnect
  itself was an unrelated device-side backward-clock issue.)
- **Root cause** — `storage/sds.py::sds_path` keyed the path only on
  `(year, net, sta, cha, loc)` and ignored the device. Two devices with the
  same NSLC therefore resolved to byte-identical paths; two `MseedWriter`s
  appended interleaved records to one file (a corruption hazard under POSIX
  append), and the DAO `files` table's `UNIQUE(path)` constraint folded both
  devices' rows into whichever inserted first — so extent/coverage queries,
  though correctly keyed on a per-device `stream_id`, read a row that had been
  overwritten by the other device.
- **Fix** — Namespace the SDS tree by device, one level above a full standard
  SDS tree: `archive_root/<device>/YEAR/NET/STA/CHAN.D/...`. The `<device>`
  segment is derived inside the components that already know the device — the
  writer (`mseed_writer.py::_write_one`) and the reader's SDS-scan fallback
  (`archive_reader.py`) — via two new pure helpers in `storage/sds.py`
  (`sanitize_device_name`, `device_sds_root`); `sds_path` itself stays
  device-agnostic and byte-identical. Because `sanitize_device_name` is not
  injective, a `RootConfig` validator rejects any config whose device names
  collapse to the same segment (a segment clash is unsafe — it re-shares a
  tree — unlike a bare NSLC clash, which the per-device paths make safe, so
  that only warns). `core/streaming_engine.py`'s archive setup, signal wiring,
  DAO-at-base (one shared `archive.db`), and the live ingestion path were left
  untouched (the only engine edit is two informational NSLC-collision log
  lines). No DB schema change and no migration: the schema was already
  per-device (`devices.name` UNIQUE → `device_id` FK → `streams`
  UNIQUE(device_id,net,sta,loc,cha)), so distinct paths alone restore correct
  attribution; the old archive is disposable and is deleted, not migrated.
- **Lesson learned** — A physical archive path must be a total function of
  *every* dimension that can independently own the same logical stream — the
  device is such a dimension, not just the SEED NSLC. Any path or uniqueness
  key over seismic data must include the device when two devices can emit
  identical NSLCs (CLAUDE.md rule 9: a file's device attribution comes from the
  writing device, never inferred from NSLC). Tests for this class must seed
  through the REAL writer (hand-built fixtures hid the original collapse) and
  assert the observable — two distinct files, two distinct extents — not the
  mechanism.

### 2026-06-05 — Archive Replay, redone: heavy archive read off the GUI thread

- **Symptom** — The first "Archive Replay" attempt was reverted: opening an
  archived detection pegged the GUI thread for seconds, the SeedLink server saw
  the client go inactive, and it dropped the live session (the M8.1 starvation
  mechanism via a new path). Its isolation test asserted only that the replay
  shared no ring buffers with the live engine — and passed while the app was
  visibly broken.
- **Root cause** — The archive read + waveform array prep ran synchronously on
  the GUI thread; holding the GIL there starved the SeedLink worker even though
  the archive data shared no buffers with live acquisition. *Isolation from
  buffers is not isolation from the GUI thread.* The reverted test asserted the
  wrong invariant (unshared buffers) instead of the one that actually matters
  (the live drain keeps advancing). The pre-existing single-component inline
  `_read_archive_window` (B2) carried the same anti-pattern with a comment
  claiming "rule 11 does not apply" — that judgement was the bug.
- **Fix** — This change reintroduces the feature as a SMALL static slice (no
  player): `core/archive_detail_loader.py` adds a parentless
  `_ArchiveDetailWorker` on a dedicated `archive-detail-loader` QThread,
  mirroring `HvsrEngine`/`DeconvolutionWorker` exactly (QueuedConnection both
  ways, never raises across the boundary, latest-wins token + cooperative
  `_stop`). It reads the detection's Z/N/E components and builds NaN-gapped
  arrays ON ITS THREAD; the GUI thread only `setData`s on the result. The
  synchronous `_read_archive_window` is deleted. `streaming_engine.py` is
  byte-identical; the live path is untouched.
- **Lesson learned** — Any heavy data prep triggered by a user action (archive
  read, array build, deconvolution) must run on a dedicated worker thread
  before it can touch the GUI; the GUI thread may only render the finished
  arrays. The test that guards this must assert the live drain
  (`_latest_raw_endtime` / processed count) KEEPS ADVANCING while the load runs
  — not merely that buffers are unshared. (CLAUDE.md rule 11, rule 10.)

### 2026-06-02 — A QueuedConnection stop flag cannot interrupt a busy worker (M10 fit)

- **Symptom** — the M10 fit-then-infer interruptibility test passed, but a
  disengage issued *during* a learning agent's `fit` did not actually stop
  the fit: a 200-step × 20 ms fake fit ran all 200 steps to completion
  (`ai_fit_progress fraction=1.0`, `ai_fit_done elapsed_ms≈4097`) before
  `disengage` returned. The test only went green because the whole 4 s fit
  fit inside the 8 s `_THREAD_JOIN_MS` join bound — it asserted the bound,
  not the interruption. Caught in code review of Stage B.
- **Root cause** — the cooperative stop flag (`_AiWorker._stop`) was set
  *only* through the `_stopRequested` Signal wired `QueuedConnection`. A
  queued slot is dispatched by the worker thread's **event loop**, which is
  blocked for the entire duration of the long `fit` call — so
  `request_stop` could not run, and `FitContext.should_stop` read `False`
  for the whole fit. Invariant violated: rule 7 ("every wait must respond
  to `stop()` within one polling period"). A QueuedConnection can never
  preempt a thread that is busy *not* spinning its event loop.
- **Fix** — commit (Stage B), `core/ai_engine.py`: `disengage` now sets
  `self._worker._stop = True` **synchronously** (a GIL-atomic cross-thread
  bool write) *before* the queued `_stopRequested.emit()`, so a fit in
  progress observes the flag on its next `should_stop` poll. The queued
  signal stays as a belt-and-suspenders for the idle-worker case. The test
  (`tests/ai/test_fit_lifecycle.py`) was rewritten to assert the *science*
  invariant (rule 10): after a mid-fit disengage the fit loop ran fewer
  than its total steps and never computed the baseline
  (`fit_steps_done < fit_steps`, `_mean is None`) — not merely that the
  join returned under bound.
- **Lesson learned** — a cancellation flag that must interrupt a
  long-running off-thread call must cross the thread boundary by a
  mechanism that does **not** depend on that thread's event loop being
  free: set a shared atomic flag (or `threading.Event` / `QThread.
  requestInterruption`) directly, never only via a QueuedConnection slot.
  And an interruptibility test must assert the work stopped early (a count
  below the total, state left uncomputed), not just that teardown returned
  under a timeout — the timeout is necessary but not sufficient (rule 10).

### 2026-06-02 — The M9 AIAgent abstraction was inference-only; M10 grew a fit lifecycle

- **Symptom** — not a bug: a recorded design evolution, so the next reader
  knows why `AIAgent` has an optional fourth phase. M9 shipped a pluggable
  agent abstraction with a three-method lifecycle (`warm_up` / `infer` /
  `release`) and a docstring claiming "a phase picker, a future event
  classifier and a future anomaly detector all implement the same
  three-method lifecycle." M10 set out to add a classifier and an anomaly
  detector — both of which must *learn* a baseline from the user's own
  channel first — and found the lifecycle had no place to do so.
- **Root cause** — the M9 abstraction modelled only *pretrained,
  inference-only* agents: `warm_up` takes no data and loads a fixed model,
  `infer` consumes one short window, and there is no baseline context, no
  learned-state concept, and no fit phase in `AIEngine`. A learning agent
  does not fit the three-method shape — it needs an optional `fit(baseline)
  → state` phase plus state persistence so it does not re-learn on every
  engage. The docstring over-generalised: the three-method lifecycle was
  correct for what M9 *built*, but claimed coverage of agent types it had
  not built.
- **Fix** — Stage B (commit), the *minimal* principled extension, all with
  inert defaults so inference-only agents are byte-for-byte unaffected:
  `AIAgent.requires_fit` (default `False`), `fit(FitContext) → FitResult`
  (default raises), `serialize_state`/`load_state` (default `None`/no-op);
  a new `FitContext`/`FitResult`; a torch-free `ai/state_store.py` that
  persists learned state under `<data_dir>/models/` (NOT the SDS archive —
  learned state is *derived*, not science data); and an `AIEngine` `FITTING`
  phase (off-thread, interruptible, observable) that fits once and resumes
  persisted state thereafter. The two SeisBench agents (`requires_fit`
  False) never enter it.
- **Lesson learned** — was this a foreseeable gap or acceptable YAGNI? Both,
  honestly: the docstring should have scoped its claim to "pretrained
  inference agents" (a one-line foreseeable correction), but *shipping* the
  fit machinery in M9 would have been speculative dead code with no learning
  agent to exercise it. The defensible rule: an abstraction may legitimately
  grow when a genuinely new shape of client arrives (a learning agent vs a
  pretrained one) — provided the growth is minimal, has inert defaults that
  leave existing clients untouched, and is documented as deliberate (here +
  `docs/reports/M10.md` H2) rather than smuggled in as a band-aid. Claims in
  a base-class docstring about "all future subclasses" are the smell to
  watch for.

### 2026-06-01 — Flaky test resurfaced: stop-one-device (same message, new cause)

- **Symptom** —
  `tests/core/test_streaming_engine_multi.py::test_stop_one_device_keeps_other_streaming_then_restart`
  went flaky again at ~10 % over a 50-iter loop (9/50 under CPU contention;
  1/10 clean), failing on the **exact same** assertion message as the
  M4-tail flake fixed in `5148c59`:
  `assert from_a_after_stop == 0` → `"dev-a should be stopped but still
  emitted N packets"` (N=1). Captured 3× in
  `docs/diagnostics/flake-resurface-asserts.txt`.
- **Root cause** — *Not* the M4-tail cause. That fix (drop the per-device
  `_StreamCoalescer` + `_key_to_pair` at `_stop_device`) is fully intact —
  proven by its own in-test regression guard `assert not stale`, which
  **passes on every failing run**. The new cause is a timing window opened
  by an unrelated, correct change: the M8.1 render-decoupling
  (`b45a627`, rule 11) flipped `coalescer.flushed -> traceReady` from
  `DirectConnection` to `QueuedConnection`. The engine lives on the GUI
  thread, so the last `_flush_all` before stop calls `coalescer.flush()`,
  which `emit`s `flushed` and **posts a `QMetaCallEvent` to the engine's
  own event queue**, then returns. `_stop_device` then disconnects the
  coalescer signal and `deleteLater()`s the coalescer — but that cannot
  purge an already-posted meta-call event whose *receiver* is the engine
  (owner of `traceReady`), not the coalescer being torn down. The test
  did `spy.coalesced.clear()` and *then* `qtbot.wait(800)`, so the one
  trailing render frame landed **inside** the counted window, after the
  clear. (`QThread.wait()` inside `_stop_device` joins the worker without
  pumping the GUI loop, so the stale event survives the join too.)
  Proven in isolation by `docs/diagnostics/flake-resurface-mechanism-proof.py`:
  `QueuedConnection` → stale frame delivered after teardown; the same
  harness with `DirectConnection` (pre-M8.1) → no leak. Pinned to the
  exact commit by `git log -L` on the connection line.
- **Fix** — `<this commit>`, **test-side only — there is no production
  bug.** A single trailing render frame after stop is correct best-effort
  behaviour under rule 11; `traceReady` is the deferred render path, not a
  science signal. The test now asserts the **science** invariant instead:
  `engine._latest_raw_endtime[key]`, written *only* in `_on_packet` (the
  worker→engine ingestion hand-off), so it advances iff the device truly
  ingested data. After `_stop_device("dev-a")` the test (1) pumps the loop
  ~200 ms to fold any genuinely in-flight pre-stop `_on_packet` into the
  baseline (the worker thread is already joined → no new packets arrive),
  (2) snapshots both devices' ingestion endtimes, (3) waits 800 ms, then
  asserts dev-a's endtime is **frozen** (`==`) and dev-b's **advanced**
  (`>`). The positive restart check stays on the render-path spy — a
  wait-until-true is immune to `QueuedConnection` deferral (it only delays
  delivery) — but now detects resumption as a count increase past a
  restart baseline (the spy is no longer `clear()`-ed). The M4-tail
  `assert not stale` guard is kept live. Verification:
  `docs/diagnostics/flake-resurface-fixed.txt` — **60/60 PASS** (was ~10 %
  FAIL). Full analysis: `docs/diagnostics/flake-resurface-findings.md`.
- **Lesson learned** —
  (1) **Same assertion message ≠ same root cause.** Before re-applying a
  prior fix, verify it is still intact; here the M4-tail regression guard
  *still passing* was the decisive discriminator between old-monster and
  new-monster. A fix earned from one race can be wrongly "re-applied" to a
  different race wearing the same failure string.
  (2) **A correct fix in one layer can open a timing window observable by
  a test that spied the wrong layer.** The render-decoupling was right;
  the test was observing the best-effort *render* path (`traceReady`)
  while asserting a *science*-grade invariant on it. Rule 10 again: assert
  the science invariant (real ingestion), never the best-effort mechanism
  (a deferred render emission). When a signal is wired `QueuedConnection`,
  any *negative* assertion downstream of it ("X did NOT happen") must
  account for an event already posted before the teardown that disconnects
  it — disconnect/`deleteLater` of the *sender* does not purge a queued
  meta-call whose *receiver* is a different, surviving object.
  (3) **Keep a prior fix's regression guard live.** The `assert not stale`
  line cost nothing to retain and instantly localised this as a *new*
  cause rather than a regression of the old one — that one passing
  assertion saved the entire "did we lose the M4 fix?" investigation
  branch.

### 2026-06-01 — Render coupling starved acquisition at high sample rates

- **Symptom** — with the Echos device raised to 500 Hz × 3 channels
  (configurable up to 4 kSPS), the app dropped samples catastrophically
  and the SeedLink server eventually closed the connection. A
  `--log-level DEBUG` run showed small `dsp_chain_drop` (47, 39, 33) at
  t+5 s, then a `ring_buffer_drop` explosion (14480, 15824) at t+23 s,
  then `socket read error [Errno 9]` and a reconnect cycle at t+37 s. At
  40–100 Hz (IRIS/ANMO) the problem was invisible. The filtered (lower)
  plot in stacked mode also rendered empty.
- **Root cause** — a best-effort consumer (the trace render) was wired
  with a `DirectConnection` *into* the engine's flush tick:
  `coalescer.flushed -> traceReady` (`streaming_engine.py`) re-emitted on
  the GUI thread synchronously inside `_flush_all`, and the widget
  `setData` ran inline. The `StreamingEngine` lives on the GUI thread, so
  `_flush_all` also drains the per-stream DSP queue (toward detection)
  and the archive inbox (toward storage). The render cost scaled with
  `window_seconds × fs` because nothing decimated for display (60k points
  @ 500 Hz, 240k @ 4 kSPS, plus an O(N) `np.roll` + `np.arange` per
  push). At high fs the render exceeded the tick budget, saturated the
  GUI thread, delayed the science drains (DSP/archive bounded queues
  overflowed) and starved the worker thread of CPU until the server
  dropped the slow client. One best-effort consumer's latency propagated
  back through the flush timer into ingestion, detection, storage, and
  the TCP socket.
- **Fix** — decouple the render from the data path (commit on `main`):
  (1) `coalescer.flushed -> traceReady` and the `main_window`
  `traceReady`/`processedTraceReady` widget connections are now
  `QueuedConnection`, so render runs out-of-band on a later event-loop
  turn, never inside `_flush_all`; (2) `_flush_all` runs the DSP-queue
  snapshot and `_drain_archive` FIRST, before the coalescer flush that
  re-emits `traceReady`; (3) `TracePlot` does min/max (peak) display
  decimation to a `pixels × 2` / `window × max_display_rate_hz` point
  budget (default `ui.max_display_rate_hz = 250`), keeping the full-rate
  buffer intact so DSP/detection/storage are unaffected; (4) the DSP
  queue bound is now fs-aware (seconds-of-headroom up to a sample
  ceiling); (5) the ring-buffer allocation logs its memory cost;
  (6) the display-history ring's steady rolling is reclassified from a
  `ring_buffer_drop` WARNING storm to a one-time `ring_buffer_saturated`
  INFO + DEBUG `ring_buffer_overwrite` — a full-ring evict is the 60 s
  snapshot window rolling, never science loss (the trace plot is fed by
  the coalescer; the ring only serves `read_recent` snapshots), so it is
  not rule-5 backpressure. Regression guards:
  `tests/core/test_high_rate_load.py` (a deterministic gate asserting
  `_flush_all` is not gated by render latency, plus a `perf` stress test
  and a filtered-path-not-empty test) and
  `tests/gui/test_trace_plot_decimation.py` (bounded point count +
  transient preserved).
- **Real-device confirmation** — a ~6 min run against the Echos at
  500 Hz × 3 channels (4096-sample ≈ 8 s packets, with a buffered
  backlog dumped on connect) showed **zero `dsp_chain_drop`, zero
  `archive_backpressure`, and no `[Errno 9]` disconnect**, where the
  pre-fix build dropped DSP samples during the connect burst and
  disconnected around t+37 s. That connect-time backlog burst (the
  `ring_buffer_drop` explosion of ≈ 1.5×10⁴ samples near t+25 s) is the
  real failure window: it briefly floods the GUI thread, and pre-fix the
  coupled render stalled the flush/drain tick precisely there. The live
  plot's refresh cadence tracks the device's packet cadence (~8 s for
  4096-sample packets), not `refresh_hz` — expected, not jank.
- **Empty filtered plot — two separate widget-side causes (NOT the
  coupling).** Real-device testing showed the lower (filtered) plot blank.
  This was wrongly attributed to render starvation at first; the actual
  causes were independent: (a) the stacked raw+filtered plot was created
  for *any* non-empty chain, including a detector-only `sta_lta` chain
  whose "processed" output is just the input passed through — there is no
  filtered waveform to show, so the lower pane was empty by construction.
  `LiveStack` now uses stacked mode only when the chain has a
  waveform-producing stage (`_DETECTOR_STAGE_TYPES` gate). (b) The lower
  plot is X-linked to the raw plot, whose axis is anchored to wall-clock,
  but `TracePlot._latest_processed_t` started at the 1970 epoch and only
  accumulated, so a *filter* chain's curve rendered ~56 years left of the
  view — off-screen. It is now slaved to `_latest_raw_t`. Guards:
  `tests/gui/test_live_stack.py` (detector-only → single, detrend →
  stacked) and `tests/gui/test_trace_plot_decimation.py` (processed X
  overlaps raw X). Lesson: an engine-side "data is emitted" test does not
  prove a *widget* renders it — for a linked-axis plot, assert the
  rendered curve falls inside the visible/linked range.
- **Lesson learned** — a best-effort consumer (display) must be isolated
  from the data path: it reaches widgets out-of-band and may decimate or
  drop frames, but ingestion, DSP, detection and storage run to
  completion regardless of render speed (CLAUDE.md rule 11). A throughput
  proxy must measure consumer LATENCY under load, not just call count:
  the M7 test counted `setData` CALLS but never its latency, so it missed
  that a slow `setData` blocked the flush/drain tick. Cross-thread (and
  even same-thread-into-a-hot-tick) signal hops into a render must be
  `QueuedConnection`.

### 2026-06-01 — PySide6 marks both `QSortFilterProxyModel` filter-invalidators deprecated (M8)

- **Symptom** — the M8 detection-table filter tests passed but emitted
  `DeprecationWarning: Function 'QSortFilterProxyModel.invalidateFilter()'
  is marked as deprecated` for every filter change. Switching to the
  documented replacement `invalidateRowsFilter()` produced the *same*
  warning for that method too — a dead end.
- **Root cause** — a PySide6 binding wart: both `invalidateFilter()` and
  `invalidateRowsFilter()` carry a deprecation marker in the generated
  bindings even though Qt itself does not deprecate them. The
  non-deprecated public slot is `invalidate()` (re-runs sort *and*
  filter).
- **Fix** — `DetectionFilterProxy` calls `self.invalidate()` on every
  predicate change (commit on the M8 stage-B branch,
  `gui/widgets/detection_table.py`). Slightly heavier than a rows-only
  invalidation but correct and warning-free; the table is small enough
  that re-sorting on a filter change is imperceptible.
- **Lesson learned** — when a PySide6 method is flagged deprecated,
  verify the *suggested* replacement is actually clean before adopting
  it (run the test under `-W error::DeprecationWarning`); prefer the
  plain `invalidate()` for proxy refilters.

### 2026-05-31 — Degenerate spectrogram passed shape-only tests

- **Symptom** — Since M6 stage 1, the spectrogram rendered as a solid
  uniform green rectangle on every device and every sample rate
  (reproduced identically on IRIS IU.ANMO.00.BHZ 40 Hz and Echos
  500 Hz). The trace plots and the PSD widget were correct; only the
  spectrogram image was degenerate. The X axis showed "Columns (older
  → newer)" with a plain integer scale instead of wall-clock time.
- **Root cause** — A data-domain ↔ display-transform mismatch in
  `gui/widgets/spectrogram_view.py`. The default Z-score colour mode
  normalised **per frequency bin over time** (a streaming Welford
  accumulator), so for any stationary input every bin sat at its own
  temporal mean → z ≈ 0 → the middle of the colormap (viridis mid =
  green), and sustained spectral features (the microseism band) were
  invisible *by construction* because they ARE the temporal mean. The
  Linear mode compounded it with a fixed `levels=(0, 1)` against linear
  power values spanning `1e-3…1e10`, clamping every pixel to the top of
  the LUT. The transform output and the display levels never agreed.
- **Fix** — Commit `<pending>`, `gui/widgets/spectrogram_view.py` (with
  `core/spectrogram_router.py`, `gui/widgets/spectrogram_dock.py`,
  `gui/widgets/live_stack.py`). Z-score is now a **per-column** z-score
  over the frequency axis computed on log power, so each column is
  normalised against its own spectral mean/std and a sustained band
  reads as a consistent positive deviation (the microseism band on
  ANMO is now a bright horizontal band). dB and Linear levels are
  auto-scaled from a robust percentile (2–98 / 5–99) of the live
  buffer rather than fixed. The warm-up region is filled to the
  display floor so a fresh view reads as dark/empty, not green. The
  dock view also gained a wall-clock UTC `DateAxisItem`, fed per-column
  `t_end` timestamps now stamped by the router.
- **Lesson learned** — A rendering test for a visual widget must assert
  on the **information content** of what is displayed (variance of the
  buffer AND the count of distinct colour-mapped values), not only its
  structural properties (shape, latest-column position). The original
  suite asserted shape and ordering, both of which pass on a uniform
  image; `test_image_has_variance` now fails on the old transform and
  passes on the new one. Pin the producer's output domain too
  (`test_output_domain_is_linear_power`) so a silent change to the
  power/dB/normalised contract breaks a test rather than the picture.

### 2026-05-10 — Cross-session durability index lied about disk truth

- **Symptom** — `tests/storage/test_pipeline_e2e.py::test_restart_resumption_reuses_existing_db`
  flaked at ~30–50 % under full-suite load on `main` (commit `4d9bcf8`)
  with `AssertionError: 512 >= 5120`. Two engine sessions wrote to
  the same SDS path; the test asserted the DB's `files.bytes` row
  stayed monotonic across the restart (`f2["bytes"] >= f1["bytes"]`).
  It did not — but the disk file content was correct across both
  sessions. The MSEED archive was always in good shape; the SQLite
  metadata index reported 10× less data than disk had.
- **Root cause** — `storage/dao.py:record_file` UPSERTs with
  replace-bytes semantics (`bytes=excluded.bytes`); intentional, and
  codified by `tests/storage/test_dao.py::test_record_file_upserts_by_path`.
  But `core/streaming_engine.py::_on_archive_flushed_file` passed the
  per-fsync `bytes_added` delta into that slot. The DB row therefore
  tracked "bytes added in the LAST fsync of the most recent session",
  not the cumulative file size. Across two sessions the UPSERT
  replaced session 1's last delta with session 2's last delta;
  `f2 >= f1` held only when timing made session 2's last fsync
  window happen to include more bytes than session 1's. `O_APPEND`
  + `close_all`'s final fsync kept write-survives-stop intact —
  only the index was wrong.
- **Fix** — commit `<hash-pending>`. `MseedWriter._fsync_one`
  captures `os.fstat(fd).st_size` after `os.fsync(fd)` returns and
  emits it as a new 7th field on `flushedFile`. The engine pipes
  `file_size` into `record_file` (replace semantics now match
  "current durable file size") while still passing `bytes_added`
  into `record_packet` (additive `streams.total_bytes`
  accumulator across UPSERT-preserved stream rows).
  `tests/storage/test_cross_session_durability.py` locks the
  contract with 12 sub-tests that drive writer + DAO directly,
  bypassing all Qt timing — ten parametrised cross-session
  iterations per outer pytest run, deterministic.
- **Lesson learned** — When a DAO method's UPSERT semantics are
  "replace by primary key", the value passed in MUST be the
  post-replace target state, not a delta. For derived values that
  have a filesystem analogue (file size, file existence, file
  mtime), the source of truth belongs to the filesystem; the index
  reads it via `fstat` at the moment durability is established.
  Engines that keep running counters in memory and pipe deltas
  through indexes invite this exact class of bug. New invariant:
  every DAO field whose name implies a totalising quantity
  (`bytes`, `count`, `total_*`) MUST be sourced cumulatively at the
  call site, never as a per-window delta. Verified in code review
  by name-shape matching: a slot's parameter named `bytes_added`
  cannot land in a column named `bytes` whose UPSERT replaces.

### 2026-05-10 — M5 closure: persistence boundary held (no incident)

- **Symptom** — Stage A and Stage B of M5 (local persistence) shipped
  with no surprises. Worth recording specifically because the
  abstractions could plausibly have leaked: the storage QThread runs
  parallel to the existing DSP router, the writer's `os.write`
  atomicity assumption is platform-specific, the encoding fallback
  triggers at write time (not config time), and the DB-after-fsync
  ordering creates a 5 s lag between disk and metadata index that
  could have surprised operators reading `streams.last_packet_at` for
  liveness monitoring.
- **Root cause** — N/A; the design held under test.
- **Fix** — N/A.
- **Lesson learned** — Three rules took the load:
  (1) **Rule 8 (persistence boundary)** kept storage code entirely in
  `storage/`, with the engine doing only signal wiring. Crash safety,
  gap detection, and DB writes are all internal to the storage
  subsystem and were independently testable because they're either
  pure (`gap_detector`, `sds`) or near-pure (`mseed_writer` only
  touches files; `dao` only touches SQLite).
  (2) **Rule 7 (wait observability)** prevented the slow-write +
  pause-path code from ever silently hanging on a misbehaving
  filesystem — every wait the writer takes is bounded and emits a
  structured log line, surfaced into `DeviceStatus.archive_last_error`.
  (3) **Memory feedback "Stub-based GUI tests miss `@Slot` ≠
  cross-thread dispatch bugs"** drove the threaded test pair
  (`test_mseed_writer_threaded.py` + the engine archive integration
  test on a real `_archive_thread`) so the writer's queued-signal
  contract was exercised on a real thread, not a stub. None of the
  M3p1/M4 cross-thread flakes regressed under the 30-iter loop. New
  invariant for future milestones: a closure entry is worth writing
  even when nothing surprised you — it's the proof the abstractions
  earn their keep.

### 2026-05-10 — Non-standard SeedLink-like devices (Echos) (M5 prep)

- **Symptom** — Third-party "Echos"-class field stations and similar
  hardware speak something that resembles SeedLink on the wire but
  diverges in framing, authentication handshake, or record size.
  ObsPy's `EasySeedLinkClient` rejects them at protocol level; the
  dashboard surfaces them as `protocol_rejected` with a hint that
  may suggest the user has a configuration error to fix, when in
  fact none exists.
- **Root cause** — Not a bug in the dashboard. The reference
  SeedLink client implements the standard wire protocol; non-
  conformant devices cannot negotiate. Operators reading the
  rejection flag may assume misconfiguration and chase a fix that
  does not exist.
- **Fix** — None applied; preserved as documentation.
  `protocol_rejected` is the correct classification (the M4 closure
  postmortem below). Native support for non-standard SeedLink
  dialects requires a driver abstraction (per-device transport
  plugin with custom framing/auth hooks) and is deferred. Operators
  targeting Echos-class devices should use the vendor's own
  software until that abstraction lands.
- **Lesson learned** — `protocol_rejected` is not always a
  configuration error. New invariant: when a future support thread
  surfaces a `protocol_rejected` device that "should work", first
  verify the device speaks standard SeedLink before chasing config
  or network bugs. The dashboard's classification is correct; the
  device may simply be out of scope.

### 2026-05-10 — Silent SeedLink protocol rejection (M4 closure)

- **Symptom** — A device pointed at a reachable SeedLink server with a
  selector the server does not serve (e.g. `echos.local:18000` with a
  stale `NET=ZZZ STA=NOPE`) sat in `WAITING_RETRY` showing
  `last fail: unknown` indefinitely. The only "real" diagnostic was a
  pair of obspy stderr lines per attempt:
  `response: station not accepted, skipping` then
  `negotiation with remote SeedLink failed: 'no stations accepted'`.
  Operators reading the structured log saw nothing actionable; the
  Devices dock tooltip suggested `nc -vz <host> <port>` which *succeeded*,
  reinforcing the false impression that "the network is fine — the
  app must be broken".
- **Root cause** — `obspy.clients.seedlink.client.seedlinkconnection`
  raises `SeedLinkException("no stations accepted")` at line 1543 (obspy
  1.4) inside `SeedLinkConnection.collect()`, then **catches and
  swallows it** in the same function at line ~848: the `except` branch
  logs the failure, calls `self.disconnect()` (our wrapped override
  flips `terminate_flag`), and re-enters the SL_DOWN reconnect loop.
  The exception never propagates to our `client.run()` caller. Our
  worker therefore funnelled the session into the generic
  `Exception → "unknown"` branch — except that branch wasn't even hit
  in the common case, because `collect()` returns *normally* once
  `terminate_flag` flips, so the worker simply emitted RECONNECTING
  and re-attempted with no failure classification at all.
- **Fix** — commit `<hash-pending>`. Three layers:
  (1) Added `_StationRejectionFilter` (a `logging.Filter` installed on
  the `obspy.clients.seedlink` logger for the lifetime of each session
  in `_run_one_session`). The filter watches for the markers
  `"station not accepted"` and `"no stations accepted"` and exposes a
  pair of flags that the worker reads after `client.run()` returns.
  (2) Extended `FailureKind` with `protocol_rejected` (and a reserved
  `protocol_unsupported`) and added `last_failure_detail: dict | None`
  to both `WorkerDiagnostics` and `DeviceStatus` so the GUI can render
  a structured "rejected: N selectors" line and a Stations-browser
  cross-reference instead of a generic retry message. The filter
  branch and the defense-in-depth `except Exception` branch both call
  `_record_protocol_rejection`, ensuring a future obspy version that
  *does* propagate the exception still classifies correctly.
  (3) DevicePanel (`gui/widgets/device_panel.py`) special-cases
  `protocol_rejected`: Diagnostics column shows
  `"rejected: N selectors · next: Xs"`, tooltip pivots from the
  `nc -vz` reachability hint to the workflow hint
  ("open the Stations browser, pick this device, hit Refresh,
  and subscribe to a station that exists on this server"), and the
  WAITING_RETRY badge gets a `(!)` suffix — at-a-glance signal that
  the retry loop is futile. Existing exponential backoff applies
  unchanged: a misconfigured selector must not hammer the server.
  Tests under `tests/core/test_protocol_rejection.py` (worker
  integration with a `reject_all_stations` fake server mode) and
  `tests/gui/test_device_panel_protocol.py` (panel rendering) lock
  the contract.
- **Lesson learned** — CLAUDE.md rule 7 ("wait observability") extends
  to ANY structured failure that a third-party library swallows
  internally. From our codebase's perspective, a library that catches
  its own exception and only logs it is just as silent as a wait that
  emits no log lines. Where a library is loud only on stderr, we
  install an in-process capture (filter / monkeypatch / state poll)
  and re-emit the failure in our own structured channel, classified
  into our own closed `FailureKind` set. New invariant: every external
  failure mode that a user can hit through configuration MUST surface
  as a distinct `FailureKind`, not as `"unknown"`.

### 2026-05-10 — Flaky multi-device tests resolved (M3p1 → M4 closure)

- **Symptom** — Two integration tests, carried over from M3p1 and
  marked but not fixed during M4, flaked at ~5 % over 50-iteration
  loops on `main`:
  * `tests/core/test_streaming_engine.py::test_engine_restart_with_different_chain_does_not_leak_old_stages`
    failed with `"router still holds a chain after stop()"`.
  * `tests/core/test_streaming_engine_multi.py::test_stop_one_device_keeps_other_streaming_then_restart`
    failed with `"dev-a should be stopped but still emitted N packets"`.
  The flakes blocked confidence in starting M5 (storage) — CI signal
  trustworthiness depends on a green suite under repeat-loop pressure.
- **Root cause** —
  *Race A — chain-leak*: `engine.stop()` emitted `_clearChainsRequested`
  via a `QueuedConnection` to the router thread, then *immediately*
  called `_dsp_thread.quit()`. `QThread.quit()` calls
  `QAbstractEventDispatcher::interrupt()` synchronously on the caller's
  thread; the interrupt could land before the queued `clear_chains`
  slot ran, so the router thread exited with stale chain entries
  intact. The next start/stop cycle then asserted on a non-empty
  `_chains` dict and failed.
  *Race B — coalescer leak*: `_stop_device` stopped the worker but
  left the per-stream `_StreamCoalescer` registered in
  `self._coalescers`. The engine's `_flush_all` `QTimer` kept ticking
  on the GUI thread regardless; for one or two ticks after stop, it
  flushed whatever the coalescer had buffered before the worker died,
  emitting "stopped device emitted a packet" to subscribers. The test
  observed `from_a_after_stop > 0` and failed.
- **Fix** — commit `<hash-pending>`. Race A: replaced the queued emit
  with `QMetaObject.invokeMethod(self._dsp_router, "clear_chains",
  Qt.ConnectionType.BlockingQueuedConnection)`. The blocking invoke
  guarantees the slot runs to completion on the router thread before
  the call returns; only then does `_dsp_thread.quit()` fire. The
  `_clearChainsRequested` Signal stays defined for any future async
  caller, but `stop()` no longer relies on its FIFO ordering. Race B:
  `_stop_device` now drops every `_coalescers` (and matching
  `_key_to_pair`) entry whose composite key starts with
  `f"{name}{DEVICE_KEY_SEP}"`, disconnects each coalescer's signal,
  and calls `deleteLater()`. Ring buffers, `_chain_installed`, and
  DSP chains are preserved (so `_start_device_by_name` resumes
  plotting into the same widget without re-emitting `newStreamSeen`).
  `_on_packet` was refactored so the coalescer is created on the
  first packet AFTER restart even though the buffer already exists —
  the per-stream-state-survives-restart contract now applies only to
  the buffer, not to the coalescer. A regression assertion in
  `test_stop_one_device_keeps_other_streaming_then_restart` checks
  `[k for k in engine._coalescers if k.startswith(f"dev-a{...}")]
  == []` directly so a re-introduction of the leak fails fast on
  the production-state invariant, not just on the symptom.
  Pre-fix baseline: `docs/diagnostics/flaky-tests-before.txt` (50
  iters, 48 PASS / 2 FAIL). Post-fix verification:
  `docs/diagnostics/flaky-tests-after.txt` (50/50 PASS).
- **Lesson learned** — Two separate invariants:
  (1) **`QThread.quit()` is not a queue barrier.** Anything that must
  run on the worker thread before the thread exits must use
  `BlockingQueuedConnection` (or an explicit completion event the
  caller waits on). A queued `emit()` followed by `quit()` is racy
  by design and the kind of bug a 50-iter loop will surface
  intermittently. New invariant: if a stop sequence depends on
  cross-thread state being cleared, the cleanup MUST be synchronous
  from the caller's perspective.
  (2) **Per-device teardown must drop every per-device piece of state
  that an unrelated timer can touch.** A worker-stop that leaves a
  GUI-thread timer free to keep emitting that worker's data is a
  half-stop, not a stop. Any per-device dict / cache / queue inside
  the engine MUST be inventoried at `_stop_device` time and either
  drained or removed; otherwise the device is "stopped" only in
  appearance.

### 2026-05-09 — Silent CONNECTING under SYN blackhole (M3p1 → M3p2 gap)

- **Symptom** — A device pointed at a host that drops SYN packets
  (e.g. `10.255.255.1:18000`) sat in CONNECTING for ~135 s on a stock
  Linux box (measured: 135.46 s with `tcp_syn_retries=6`) before
  finally transitioning to RECONNECTING. No log line was emitted for
  the duration of the silence. Operators reported the dashboard
  appearing frozen; "the app is broken" filed against perfectly valid
  configurations on networks that block outbound SeedLink ports.
- **Root cause** — `EasySeedLinkClient.connect()` performs a blocking
  TCP handshake using the OS default `tcp_syn_retries` envelope
  (~127 s on a typical Linux configuration, observed up to 135 s).
  The worker emitted `CONNECTING` once and then handed off to obspy's
  blocking call with no timeout we controlled, so the worker thread
  silently consumed the entire OS budget before any retry path engaged.
  Per-attempt logging did not exist, so even the structured-log
  channel was silent during the hang.
- **Fix** — commit `<hash-pending>`. Three layers, in order:
  (1) Added a `socket.create_connection` preflight bounded by
  `ReconnectConfig.connect_timeout_s` (default 10.0 s) before obspy's
  connect; failures are classified into `timeout | refused | dns |
  unknown`. (2) Added `ConnState.WAITING_RETRY` distinct from
  `CONNECTING` so the UI can show "in backoff sleep" vs. "actively
  trying". (3) Surfaced attempt counter / last failure kind / next
  retry seconds in `DeviceStatus` and the new DevicePanel Diagnostics
  column, with a tooltip giving an `nc -vz` reproduction hint.
  Per-attempt INFO logging (`seedlink_connect_attempting`,
  `seedlink_connect_established`), WARNING per failure
  (`seedlink_connect_failed`), and a one-shot ERROR escalation
  (`seedlink_connect_failing_repeatedly`) at 5 consecutive failures.
  Stop-during-WAITING_RETRY remained interruptible (the existing
  `_sleep_interruptible` polls `self._stop` every 50 ms); a new
  regression test under `tests/core/test_seedlink_worker_timeout.py`
  asserts ≤ 0.5 s shutdown wallclock during backoff sleep.
- **Lesson learned** — Any blocking system call from a third-party
  library MUST be wrapped in a timeout we control, OR be invoked only
  after a preflight that we control. Default OS timeouts are never
  acceptable for user-facing operations. New invariant: every entry
  point to a third-party blocking call in this codebase has a
  documented worst-case wall time bound, or fails review. The bound
  must also be configurable (so future operators can tune for their
  network reality without forking) and surfaced via structured
  logging on every iteration so silence is itself a bug.

### 2026-05-08 — Worker shutdown segfault under tight pytest loops (M3 prep)

- **Symptom** — `for i in $(seq 1 50); do uv run pytest -q; done` aborted
  with a Python faulthandler "Aborted" trace in 30–50% of runs. The
  same suite under `pytest -v` (slower per-test cadence) passed
  consistently. Two distinct races contributed; both sat in the
  shutdown path between `SeedLinkWorker.run()` and
  `StreamingEngine.stop()`.
- **Root cause** —
  *Race A — connect-vs-stop in `_run_one_session`*: the worker assigned
  `self._client = client` *after* `client.connect()`. A `stop()` firing
  in that window observed `self._client = None`, skipped the socket
  close, and the worker then entered ObsPy's blocking `recv` with no
  way to wake. The QThread Python wrapper was later GC'd while the
  underlying thread was still running, and Qt aborted the process.
  *Race B — late queued signal landing on a torn-down bridge*:
  `worker.stop()` previously returned without waiting for `run()`, so a
  `stateChanged` queued emission could dispatch onto an already-released
  `_DeviceBridge` after the engine cleared its bridge dict.
- **Fix** — commit `981e1a8`. Race A: `_run_one_session` now exposes
  `self._client` *before* `client.run()` and bails immediately if
  `_stop` was set during `connect()`; `stop()` polls `self._client`
  and re-applies `terminate_flag` + socket shutdown each pass until
  `_run_done` fires (cap 2 s). Race B: `SeedLinkWorker.run()` sets a
  `_run_done` `threading.Event` in its `finally` block; `stop()` blocks
  on that event before returning. `StreamingEngine.stop()` also
  explicitly disconnects every named bridge signal before clearing the
  bridge dict so a stray cross-thread emission cannot reach a
  soon-to-be-released bridge during garbage collection. Belt-and-braces:
  the asyncio fake server now populates and awaits its handler-task set,
  eliminating a Python 3.11 vs 3.12 divergence in `Server.wait_closed()`
  semantics.
- **Lesson learned** — every QThread-hosted worker MUST guarantee that
  `stop()` returns only after `run()` has fully unwound, AND the engine
  MUST disconnect bridge signals before releasing the bridge object.
  Encoded as the worker-shutdown contract in
  `core/seedlink_worker.py` (`_run_done` event + polled socket close)
  and `core/streaming_engine.py::StreamingEngine.stop` (per-signal
  disconnect before dict clear). Future workers in this project follow
  the same pattern; tests under `tests/core/test_seedlink_worker.py`
  enforce the budget (`stop()` ≤ 1 s on a connected worker, no
  WARNING+ records during teardown).
