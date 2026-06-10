# Manual test procedures

> Carried over from the original seedlink-dashboard project (M0-C,
> 2026-06-10) with the AI sections removed (CLAUDE.md rule 12) and the
> package renamed. Public-server walkthroughs (IRIS/GFZ/INGV) remain
> valid for protocol-level testing; Echos-specific procedures will be
> added per milestone.

Automated tests cover the offline path (fake SeedLink server). The procedures
below verify behaviour against real public servers — required only for
release sign-off and when changing networking code.

## First live connection (M2)

Goal: prove that a single configured device transitions through
`CONNECTING → CONNECTED`, that the central live stack receives a scrolling
trace, and that the device dock shows the per-NSLC row appearing.

### 1. Find your user-config path

The app writes a `config_loaded` log line at startup with the resolved
path. Without launching the app, the path is determined by `platformdirs`:

| OS      | Path                                                   |
| ------- | ------------------------------------------------------ |
| Linux   | `$XDG_CONFIG_HOME/echosmonitor/config.yaml` (default `~/.config/echosmonitor/config.yaml`) |
| macOS   | `~/Library/Application Support/echosmonitor/config.yaml` |
| Windows | `%LOCALAPPDATA%\echosmonitor\config.yaml`        |

Or pass `--config /path/to/your.yaml` to skip the lookup.

### 2. Enable the IRIS smoke device

Paste the following into your user config (everything that's not in
`config/default.yaml` overrides those defaults):

```yaml
devices:
  - name: iris-iu-anmo
    host: rtserve.iris.washington.edu
    port: 18000
    reconnect:
      initial_delay_s: 1.0
      max_delay_s: 60.0
    selectors:
      - { network: IU, station: ANMO, location: "00", channel: BHZ }
```

### 3. Run the app

```bash
uv run python -m echosmonitor
```

Expected timeline (approximate, network-dependent):

1. Window opens within ~1 s.
2. The **Devices** dock shows `iris-iu-anmo` with state `CONNECTING`
   (amber).
3. Within ~5 s the row turns `CONNECTED` (green) and a child row appears:
   `IU.ANMO.00.BHZ`.
4. Within ~30 s the central plot starts scrolling. (IRIS's RT server
   often batches a few minutes of data before flushing the first packet —
   delays up to a couple of minutes are normal at quiet times.)

### 4. Public servers known to work

| Provider | Host                           | Port  | Example selector              |
| -------- | ------------------------------ | ----- | ----------------------------- |
| IRIS DMC | `rtserve.iris.washington.edu`  | 18000 | `IU.ANMO.00.BHZ` (single ch.) |
| INGV     | `discovery.rm.ingv.it`         | 18000 | `IV.*.*.HHZ` (Italy network)  |
| GFZ      | `geofon.gfz-potsdam.de`        | 18000 | `GE.*.*.BHZ`                  |

All three are best-effort, throttled feeds; do not run sustained
production traffic against them.

> **INGV note (2026-05-09).** INGV's `discovery.rm.ingv.it:18000` was the
> bundled second-server example through M3 but became unroutable from EU
> consumer ISPs in May 2026 (TCP SYN drop, classified `timeout` by the
> worker — see POSTMORTEMS 2026-05-09). GFZ replaced it as the bundled
> example. If you want to try INGV anyway, configure `host:
> discovery.rm.ingv.it`, `network: IV`, `station: MILN`, `channel: HHZ` —
> it may still work on academic networks.

### 5. Troubleshooting

**Stuck in `CONNECTING`.** Verify outbound TCP 18000 isn't blocked by a
firewall or corporate proxy: `nc -zv rtserve.iris.washington.edu 18000`
should return `succeeded`.

**Disconnects immediately, error mentions "no data stream selectors
accepted".** The server doesn't carry the channel you asked for. Try a
broader selector (e.g. `IU.*.*.BHZ`) or pick a different station from
the IRIS station list at https://ds.iris.edu/mda/IU/.

**Connects but no plot ever scrolls.** This is most often the server
batching data — wait up to two minutes. If still no data, switch
networks (some ISPs deeply throttle SeedLink). Check the log for
`packetReceived` events; if none, the server isn't sending anything.

**Enable debug logs.** Either:

```bash
uv run python -m echosmonitor --log-level DEBUG
```

or set `app.log_level: DEBUG` in your YAML. Combine with
`--log-json` for machine-parseable output.

**Window state survives restarts** via `QSettings`. To reset layout, on
Linux remove `~/.config/EchosMonitor/EchosMonitor.conf`.

## Reconnect after server drop

Goal: confirm the worker rejoins automatically after a transient
disconnect.

1. Connect against the IRIS device above.
2. Once data is scrolling, drop the network briefly (`sudo nmcli
   networking off; sleep 3; sudo nmcli networking on`).
3. The Devices dock should transition `CONNECTED → RECONNECTING`, and
   within ~30 s return to `CONNECTED`.

## Bandpass on ANMO with STA/LTA tap (M2)

Goal: prove the live DSP chain — detrend → bandpass → STA/LTA — runs
end-to-end on a real public stream. The central widget should show two
stacked plots: the raw trace on top (long-period microseism dominates)
and the bandpassed trace below (centered around zero, mid-frequency
content visible). The detection log should *not* fire on quiet
background.

### 1. Configure a chain on the IRIS device

Replace your IRIS device entry with the following:

```yaml
devices:
  - name: iris-iu-anmo
    host: rtserve.iris.washington.edu
    port: 18000
    reconnect:
      initial_delay_s: 1.0
      max_delay_s: 60.0
    selectors:
      - { network: IU, station: ANMO, location: "00", channel: BHZ }
    dsp_chain:
      - { type: detrend, kind: constant }
      - { type: bandpass, freqmin: 0.5, freqmax: 8.0, corners: 4, zerophase: false }
      - { type: sta_lta, sta: 1.0, lta: 30.0, on_threshold: 3.5, off_threshold: 1.5 }
```

### 2. Run the app

```bash
uv run python -m echosmonitor
```

Expected timeline:

1. Window opens, `iris-iu-anmo` reaches `CONNECTED` within ~5 s.
2. The central widget shows a **stacked** TracePlot for `IU.ANMO.00.BHZ`:
   the upper (gray) curve is the raw counts; the lower (cyan) is the
   filtered output.
3. For the first ~30 s the lower plot may show a transient as the IIR
   filter warms up. After that, the lower trace centers around zero with
   visible mid-frequency content (1–8 Hz band).
4. Pan or zoom on the upper plot — the lower plot must follow exactly.

### 3. Detection sanity check

On quiet background data (typical at ANMO), no `dsp_trigger` events
should fire over a 5-minute observation. If you see frequent triggers,
either the threshold is too low or the device is being hit by glitches —
in the latter case the small drop-status badge in the plot header turns
red.

To force a trigger for a smoke check, lower `on_threshold` to e.g. 2.0
and a few minutes of background activity should produce one or two
events. Restore the default 3.5 once you've confirmed the mechanism.

### 4. Troubleshooting

**Lower plot stays empty.** The chain failed to build (most often
because `freqmax >= fs/2` for the stream's actual rate). Check the log
for `dsp_chain_build_failed`. Note that ANMO's BHZ channel is 20 Hz, so
the example's 8 Hz upper bound is well within Nyquist.

**Drop badge turns red.** The DSP router's bounded queue is overflowing
(likely a slow CPU or another long-running process). The log emits
`dsp_chain_drop` at most every 5 s per stream with a count. Reduce
`ui.refresh_hz` if this persists.

**Y axis on lower plot has unexpected scale.** Toggle the per-plot
`Auto Y` button — pyqtgraph's auto-range can latch onto a transient.

**I only see one plot per stream.** Two common causes:

1. The device's `dsp_chain` is empty or missing in your config. The
   stacked raw + filtered view only appears when at least one stage is
   present. Check the status bar at the bottom of the window — if any
   configured device has no chain, a dim italic note appears: *"·
   N device(s) without DSP chain"*; hover for the device names. Also
   look for `device_no_dsp_chain` in the log at startup.
2. Chain construction failed silently. The engine logs
   `dsp_chain_build_failed` at WARNING when a stage cannot be built
   against the stream's actual sample rate (most often `freqmax >=
   fs/2`). If you see `dsp_chain_build_failed` *without* a follow-up
   indication in the UI, file an issue — that's a bug; the failure
   should be loud.

## Two real servers in parallel (M3 part 1)

Goal: prove that two SeedLink devices configured in the same YAML
stream concurrently — each in its own QThread, each with its own DSP
chain, each rendered in a dedicated device group in the central widget.

### 1. Configure both IRIS and INGV

Replace the `devices:` section of your user config with the following.
Each device declares a different DSP chain so the two filtered traces
look visibly different from each other (the IRIS chain is global-scale,
the INGV chain is regional-scale).

```yaml
devices:
  - name: iris-iu-anmo
    host: rtserve.iris.washington.edu
    port: 18000
    reconnect:
      initial_delay_s: 1.0
      max_delay_s: 60.0
    selectors:
      - { network: IU, station: ANMO, location: "00", channel: BHZ }
    dsp_chain:
      - { type: detrend, kind: constant }
      - { type: bandpass, freqmin: 0.5, freqmax: 8.0, corners: 4, zerophase: false }
      - { type: sta_lta, sta: 1.0, lta: 30.0, on_threshold: 3.5, off_threshold: 1.5 }

  - name: ingv-rt
    host: discovery.rm.ingv.it
    port: 18000
    reconnect:
      initial_delay_s: 1.0
      max_delay_s: 60.0
    selectors:
      - { network: IV, station: MILN, location: "", channel: HHZ }
    dsp_chain:
      - { type: detrend, kind: constant }
      - { type: bandpass, freqmin: 1.0, freqmax: 15.0, corners: 4, zerophase: false }
      - { type: sta_lta, sta: 0.5, lta: 20.0, on_threshold: 3.5, off_threshold: 1.5 }
```

### 2. Run the app

```bash
uv run python -m echosmonitor
```

Expected timeline:

1. Window opens, both `iris-iu-anmo` and `ingv-rt` rows appear in the
   Devices dock with state `CONNECTING` (amber).
2. Within ~30 s both devices reach `CONNECTED` (green). The Devices
   dock's `Stats` column begins ticking up at 1 Hz with packet and byte
   counters.
3. The central widget shows **two device groups** stacked vertically,
   separated by a draggable splitter. Each group has a header strip
   with the device name in **bold**, the connection-state badge, a
   `1/1` visible/total counter, and a stacked TracePlot (raw above,
   filtered below).
4. Both plots scroll independently — the IRIS chain (0.5–8 Hz) shows
   long-period microseism content; the INGV chain (1–15 Hz) shows
   higher-frequency local-seismicity content.
5. No `dsp_chain_build_failed` log lines for either device.

### 3. Independent failure check

Disconnect from the network briefly (`sudo nmcli networking off`).
Both device rows transition to `RECONNECTING` (amber). After a few
seconds, restore the network — both should return to `CONNECTED`
without you needing to restart the app. The two devices reconnect
independently; if only one comes back, that's a bug.

### 4. Same-NSLC scenario (rare, but legal)

The engine namespaces per-stream state by `(device_name, nslc)` so two
servers publishing the same NSLC (for example, two relays of the same
upstream feed) don't collide. There's no public way to provoke this
against real public servers — it's covered by the unit test
`tests/core/test_streaming_engine_multi.py::test_same_nslc_across_two_devices_buffers_are_independent`.
For ad-hoc verification, point two devices at the same selector on the
same upstream and confirm two device groups appear with one stream
each, identically labelled but each with its own scrolling buffer.

**Archive separation (per-device SDS).** With both same-NSLC devices
archiving enabled, the SDS tree is namespaced per device: data lands under
`archive_root/<device>/YEAR/NET/STA/CHAN.D/...` — one complete standard tree
per device. Confirm two sibling directories appear under `archive_root` (one
per device name) and that the Archive tab shows a distinct extent/coverage for
each device, not a single merged span. A device dialog warning banner ("…both
produce `XX.ECHOS.00.HHZ`…") appears when you configure the second device with
the same NSLC; it is informational and does not block saving.

> **Upgrade note:** this layout changed — pre-existing archives written under
> the old non-namespaced layout (`archive_root/YEAR/...`) are **not** migrated
> and will not be found by the Archive tab or readers. Delete the old
> `archive_root` directory and re-record on the new per-device layout.

### 5. Clean shutdown with two devices

Close the main window. The process should exit within ~2 s — the same
budget as a single device, NOT 4 s for two devices. The engine stops
the workers in parallel on helper threads. If shutdown takes more than
~3 s, file a bug; the parallel-stop path has likely regressed.

## Clean shutdown

Goal: closing the window should stop all worker threads inside ~2 s.

1. Connect a device and let data flow.
2. Close the main window.
3. Process should exit (the terminal where you launched it returns to
   the prompt). If it hangs more than ~5 s, file a bug — `engine.stop`
   is supposed to close worker sockets and join threads with a 2 s
   timeout each.

## Recovering from a closed dock

Goal: confirm the three recovery paths after accidentally closing a dock.

### A. View menu

1. Open the app and verify all four docks are visible (Devices,
   Stations, Spectrogram, Log). Live / Detections / PSD / HVSR are
   now central tabs, not docks.
2. **View → uncheck "Spectrogram"** — the Spectrogram dock disappears.
3. **View → check "Spectrogram"** — the dock returns in its previous
   position.
4. Verify the keyboard accelerators: `Alt+1` toggles Devices, `Alt+2`
   Stations, `Alt+3` Spectrogram, `Alt+4` Log. The View menu shows the
   same shortcut next to each entry.

### B. Title-bar context menu

1. Right-click the Devices dock title bar (or any visible dock title
   bar, or the menubar). A popup appears listing every dock — both
   visible and hidden.
2. Toggle "Spectrogram" twice; the dock hides and re-shows.

This recovery path is provided by `QMainWindow.createPopupMenu()` for
free — it stays available as long as at least one dock is visible.
Section C below covers the only state in which it would be unreachable.

### C. View → Reset window layout

1. Float and drag a couple of docks into odd positions, close two
   others.
2. **View → Reset window layout…** A confirmation dialog appears
   ("Reset all window layout to defaults?…"). Click **Yes**.
3. The window resizes to 1600×1000 and all four docks return to the
   default layout (Devices/Stations tabbed on the left, Spectrogram/Log
   tabbed full-width on the bottom; the central tab group fills the
   middle).
4. Open devices and the configuration on disk are unaffected — only
   `geometry` and `windowState` in QSettings are cleared.

### Anti-pattern: closing every dock

Closing every dock is a legal operation, but on the next launch the
app auto-restores the **Devices** dock as a safety fallback (an
otherwise-empty MainWindow has no discoverable recovery path for a
non-technical user). Look for the structured-log line on startup:

```
all_docks_hidden_fallback   message=all docks were hidden in saved state; restored Devices dock as a safety fallback
```

## Diagnosing connectivity (M3p2)

Goal: prove that a SeedLink server which silently drops SYN packets
(corporate firewall, route to nowhere, host down) surfaces in the UI
within ~10 s rather than the OS default ~127 s.

### 1. Add a known-blackhole device

Add the following block to your user config:

```yaml
devices:
  - name: blackhole-test
    host: 10.255.255.1     # RFC1918, typically unrouted
    port: 18000
    selectors:
      - { network: XX, station: TEST, location: "", channel: HHZ }
    reconnect: { initial_delay_s: 1.0, max_delay_s: 60.0, connect_timeout_s: 10.0 }
    dsp_chain: []
```

### 2. Expected UI within ~11 s

The Devices dock row for `blackhole-test` should:

- Briefly show **CONNECTING** (amber).
- Transition to **WAITING_RETRY** (darker amber, distinct from
  CONNECTING) within `connect_timeout_s + ~1 s`.
- Show a Diagnostics column entry like
  `attempt 1 · last fail: timeout · next: 1s`, updating each second.
- Show a tooltip on the row mentioning the failure kind and a copy-
  pasteable `nc -vz` reproduction command.

After a few backoff cycles, the failure kind stays `timeout` and
attempt counter climbs. The terminal log emits one
`seedlink_connect_attempting` (INFO) per attempt and one
`seedlink_connect_failed` (WARNING) per failure with structured
context. After 5 consecutive failures, a single
`seedlink_connect_failing_repeatedly` ERROR appears once.

### 3. Verify the link from your shell

```bash
nc -vz 10.255.255.1 18000        # should hang ~10s then "timed out"
python -c "import socket; socket.create_connection(('10.255.255.1', 18000), timeout=2)"
```

Both should fail with a timeout. If `nc` immediately reports
`connection refused` or `host is unreachable` instead, your network
isn't blackholing 10.255.255.1 — pick a different unrouted address
or just trust the WAITING_RETRY transition (the failure kind in the
UI will switch to `refused` / `unknown` accordingly).

### 4. Common cause: corporate firewall

Port 18000 (default SeedLink) is commonly blocked outbound by
enterprise networks. If `nc` from your shell hangs identically against
a real SeedLink server (e.g. `discovery.rm.ingv.it:18000`), check
with your network admin or try from a non-corporate network
(home / mobile hotspot) to confirm. The dashboard surfaces the symptom
within `connect_timeout_s` either way; only the *cure* requires
network-level changes.

## Stations browser (M4 stage A)

Goal: confirm the Stations dock can fetch a server's catalog without
restarting the engine. Read-only in stage A; stage B wires the
"Add to device" button.

### 1. Configure GFZ in your user YAML

```yaml
devices:
  - name: gfz-de
    host: geofon.gfz-potsdam.de
    port: 18000
    reconnect: { initial_delay_s: 1.0, max_delay_s: 60.0, connect_timeout_s: 10.0 }
    selectors:
      - { network: GE, station: WLF, location: "", channel: BHZ }
```

### 2. Run the app and switch to the Stations dock

```bash
uv run python -m echosmonitor
```

The Stations dock shares the left tab area with Devices; click its
tab to bring it forward. Confirm the device combo lists `gfz-de`,
the `[Refresh]` button is enabled, and the inline spinner glyph
shows `↻` (idle).

### 3. Refresh and inspect

Click `[Refresh]`. Expected:

- The spinner glyph cycles `⠋⠙⠹⠸…` for 1-3 seconds.
- The left tree populates with `GE` as a top-level node;
  expanding it reveals the stations served by GFZ (typically a
  few dozen).
- Selecting `GE.WLF` populates the right table with that
  station's streams (`BHZ`, `BHN`, `BHE`, plus low-rate channels
  like `LHZ`).
- The bottom toolbar reads "0 streams selected" with a greyed-out
  `[Add to device…]` button (tooltip: "Available in M4 stage B.").
  Stage B wires this button.

### 4. Logs to grep for

```
info_fetch_start        kind=STATIONS host=geofon.gfz-potsdam.de
info_connect_attempting
info_connect_ok         elapsed_ms=...
info_fetch_ok           kind=STATIONS payload_bytes=...
```

The `info_connect_*` pair was added by the M4 stage A code-review
fix; their absence after a Refresh would indicate the watchdog regressed.

### 5. Persistence check

Close and re-open the app. Confirm the Stations dock remembers:

- the last-selected device (combo restored),
- which network rows were expanded,
- the splitter position between the tree and the table.

## Dynamic device management (M4 stage B)

Goal: confirm devices can be added, edited, removed, and reconnected
entirely through the UI — no YAML editing, no app restart.

### 1. Start with an empty config

```bash
rm -f ~/.config/echosmonitor/config.yaml
uv run python -m echosmonitor
```

The Devices dock shows a centered "No devices configured." label
with an `[Add device…]` button beneath it. The status bar carries
the dim italic tip "Tip: open the Devices dock to add your first
server." (Stage C surfaces this).

### 2. Add a device via the toolbar

Click `[+ Add device]`. The modal Add-device dialog opens with an
empty form. Fill:

- Name: `gfz-de`
- Host: `geofon.gfz-potsdam.de`
- Port: 18000 (default)
- Connect timeout: 10.0 s (default)
- Selectors: row `GE`/`WLF`/`""`/`BHZ`

The OK button is greyed until the form is valid. Click OK.

Expected:

- The dialog closes.
- A `gfz-de` row appears in the Devices tree, transitions
  `CONNECTING → CONNECTED` within ~5 s.
- The status-bar tip disappears (devices is now non-empty).
- `~/.config/echosmonitor/config.yaml` exists with the
  device block.

### 3. Browse → subscribe → see a plot

Switch to the Stations dock, click `[Refresh]`, expand `GE`,
select `WLF`, check `IU.WLF.*.BHZ` (single row). The
`[Add to device…]` button enables. Click it; the popup's
"Add to existing device" radio is pre-selected with `gfz-de`.
Click OK.

Expected:

- The popup closes.
- Within ~1 second, a `IU.WLF..BHZ` row appears in the Devices
  dock under `gfz-de`.
- A scrolling TracePlot appears in the Live tab.
- No engine restart, no socket drop on `gfz-de`.

### 4. Edit chain only — verify NO restart

Right-click `gfz-de` → Edit (or use the toolbar). Open the dialog,
note the chain summary. Click OK without changing the chain (the
Edit-chain button is disabled in stage B). Now in your YAML, move
the `freqmin` from 0.5 to 1.0; the engine picks up the change as
chain-only:

Expected:

- The device's connection state STAYS at `CONNECTED` (no
  `CONNECTING` transition).
- The filtered (lower) trace's content shifts to reflect the new
  passband.

(Future M5 will make the chain editable from the dialog itself;
the YAML-edit path is the workaround until then.)

### 5. Remove a device

Right-click `gfz-de` → Remove. The confirm dialog opens with the
disabled "Also delete this device's archived data" checkbox (M6
expectation-set). Click OK.

Expected:

- The device disappears from both the Devices dock and the Live tab
  within ~1 second.
- `~/.config/echosmonitor/config.yaml.1` exists with the
  pre-removal content.
- The empty-state CTA returns.

### 6. Backup rotation

Inspect the config directory:

```bash
ls -la ~/.config/echosmonitor/
```

After several mutations, you should see `config.yaml`,
`config.yaml.1`, `config.yaml.2`, `config.yaml.3`, but NOT
`config.yaml.4` (the oldest is unlinked before each write).

### 7. Reconnect now

Disable your network briefly so `gfz-de` enters `WAITING_RETRY`,
then re-enable it. Right-click `gfz-de` → Reconnect now. The row
should immediately retry without waiting out the backoff window.

## First-run experience (M4 stage C)

Goal: confirm the app guides a fresh user through their first device
without ever touching the YAML.

### 1. Nuke the user config

```bash
rm -rf ~/.config/echosmonitor/
```

### 2. Recommended branch

Run the app:

```bash
uv run python -m echosmonitor
```

The First Run wizard appears modally over a hidden main window.
Page 1 (Welcome) shows the dim italic "Probing recommended
servers…" line for a couple of seconds, then flips to
"Recommended: GFZ Potsdam (responded ✓)" or "IRIS DMC (responded
✓)" depending on which answered first.

Leave the default radio ("Start with a recommended public server"),
click Next → Confirmation page. Verify the summary mentions either
`gfz-de` (host `geofon.gfz-potsdam.de`) or `iris-iu-anmo`
(host `rtserve.iris.washington.edu`). Click Finish.

Expected:

- Main window appears.
- The chosen device transitions `CONNECTING → CONNECTED` within
  ~5 s.
- Within ~30 s, plots scroll in the Live tab.
- `~/.config/echosmonitor/config.yaml` exists with the
  device block.

### 3. Configure-my-own branch

Re-run after `rm -rf ~/.config/echosmonitor/`. Pick
"Configure my own server now", click Next. Fill in your own
SeedLink server. Click `[Test connection]` — the inline label
turns green with `✓ Connected to ... v...` on success or red
with the failure kind on a bad host. Click Next → Confirmation →
Finish.

### 4. Skip branch

Re-run after `rm -rf ~/.config/echosmonitor/`. Pick "Skip —
I'll add a device later", Next → Confirmation (summary reads "No
device will be created."), Finish.

Expected:

- Main window appears.
- DevicePanel shows the empty-state CTA.
- Status bar shows the persistent "Tip: open the Devices dock to
  add your first server." message.

### 5. Wizard MUST NOT re-appear

Re-run the app a second time without nuking the config. The
wizard MUST NOT show — even on the Skip branch (the user file
now exists, even if empty). The conjunction in
:func:`is_first_run` is the thing that prevents this from
becoming a launch-time interruption.

### 6. Logs to grep for

```
config_loaded                            (always)
info_fetch_start  kind=ID                (welcome-page probe)
config_store_committed action=add_device (on Finish)
```

The `--config /path/to/explicit.yaml` CLI flag suppresses the
wizard regardless of the conjunction — useful for tests and
explicit overrides.

## Diagnosing protocol rejection

Goal: when a SeedLink server accepts the TCP handshake but rejects the
selectors we ask for (wrong NET / STA / CHA), the panel must surface
this as a *misconfiguration* — not a generic outage — and point the
operator at the resolution path.

### 1. Add a deliberately-wrong device

Pick any reachable SeedLink server, then add a device whose selector
references a station that does NOT exist on it. Example using IRIS:

```yaml
devices:
  - name: rejected-smoke
    host: rtserve.iris.washington.edu
    port: 18000
    reconnect:
      initial_delay_s: 1.0
      max_delay_s: 30.0
    selectors:
      - { network: ZZ, station: NOPE, location: "", channel: BHZ }
```

You can also drive this through the Devices dock toolbar
(`Add device…` → fill the same wrong selector → `[Test connection]`).
The test-connection probe uses the INFO path, not STATION negotiation,
so it may report `Connected` even though the streaming session will be
rejected — that's expected; the rejection becomes visible once the
engine starts the worker.

**Caveat for IRIS specifically.** Real SeedLink servers vary in how
they handle bad selectors: some send `ERROR\r\n` to STATION (the
path our filter watches for), others accept STATION liberally and
silently route no data, still others fail at a later verb. IRIS in
particular is known to accept STATION/SELECT liberally and only
filter at the data-routing layer; it may NOT emit obspy's
"no stations accepted" marker. If the recipe below does not
reproduce within 30 s against a public server, the server is
silently routing nothing — switch to a local test against
`tests/core/fakes.FakeSeedLinkServer` with
`reject_all_stations=True` (the canonical `ERROR\r\n` path the
integration tests exercise).

### 2. What you should see within ~10–15 s

In the Devices dock:

| Column      | Value                                                |
| ----------- | ---------------------------------------------------- |
| State       | `WAITING_RETRY (!)` (amber, with the bang suffix)    |
| Diagnostics | `rejected: 1 selectors · next: Xs`                   |

Hover the row to see the tooltip:

```
Server rejected the requested stations (1 selector).
Last: server rejected the requested stations.
Try: open the Stations browser, pick this device, hit Refresh,
and subscribe to a station that exists on this server.
```

The `(!)` suffix is the at-a-glance signal that the retry loop is
*futile* — no amount of waiting will help; the configuration needs
human attention.

### 3. Logs to grep for

```
seedlink_protocol_rejected   rejected_selectors=['ZZ.NOPE..BHZ'] rejection_count=1
seedlink_connect_failed      kind=protocol_rejected
seedlink_state               state=WAITING_RETRY  message='retrying in 1.0s'
```

obspy itself emits `obspy.clients.seedlink` ERROR lines too:

```
response: station not accepted, skipping
negotiation with remote SeedLink failed: 'no stations accepted'
```

These are the markers our `_StationRejectionFilter` watches for.

### 4. Resolution path (the workflow the tooltip points at)

1. Open the **Stations** dock (View → Stations, if not already
   visible).
2. Pick **rejected-smoke** in the device combo.
3. Click **Refresh** — the server's catalog populates.
4. Pick a station that actually exists on this server.
5. Click **Subscribe in Live** (or **Add to device…** for a
   permanent edit).

The DevicePanel row should transition through `CONNECTING →
CONNECTED` within a few seconds and the `(!)` suffix should
disappear.

### 5. Backoff sanity check

While the device is in the rejection loop, confirm in the logs that
the worker is honouring the configured backoff (1 s → 2 s → 4 s …):
the `next_retry_in_s` field on each `seedlink_connect_failed` line
should roughly double up to `max_delay_s`. A misconfigured selector
must NOT cause the worker to hammer the server at full speed —
this is the rule-7 invariant for misconfiguration loops.

## Archive smoke test (M5 stage A)

Goal: with archive enabled on one device, prove that MiniSEED files
appear under the configured root in canonical SDS layout, that the
DevicePanel surface reflects the archive footprint live, and that a
clean Ctrl+C leaves the files readable end-to-end via ObsPy.

### 1. Configuration

Drop the following into your user config (`config.yaml`) — pick an
absolute, writable path for `archive_root`:

```yaml
app:
  log_level: INFO
  archive_root: /tmp/echosmonitor-archive   # any writable directory

devices:
  - name: iris-iu-anmo
    host: rtserve.iris.washington.edu
    port: 18000
    selectors:
      - { network: IU, station: ANMO, location: "00", channel: BHZ }
    dsp_chain: []         # raw plot only — keeps the archive smoke test
                          # scoped to storage; M2 covers DSP behaviour.
    archive:
      enabled: true
      encoding: STEIM2    # IRIS BHZ is int32 → STEIM2 stays primary
      record_length: 512
      fsync_interval_s: 5.0
```

### 2. Run for ~5 minutes

```bash
uv run python -m echosmonitor
```

Watch the **Devices** dock:

* The state badge transitions `CONNECTING → CONNECTED`.
* Within ~30 s the `Stats` column gains an inline second segment, e.g.
  `1.5k pkts / 1.2 MB · arch 240 KB · 1 files`.
* The `arch` segment grows monotonically; `files` stays at `1` (single
  NSLC, single UTC day).
* No `(!)` suffix should appear — that would indicate
  `archive_last_error` is set.

Stop the app cleanly with `Ctrl+C` (or **File → Quit**).

### 3. Verify the SDS tree

```bash
tree /tmp/echosmonitor-archive
```

Expected layout (year/doy will reflect the run date):

```
/tmp/echosmonitor-archive
└── 2026
    └── IU
        └── ANMO
            └── BHZ.D
                └── IU.ANMO.00.BHZ.D.2026.130
```

The file size should be a multiple of 512 bytes (the configured
`record_length`).

### 4. Read back via ObsPy

```bash
uv run python -c "from obspy import read; \
  st = read('/tmp/echosmonitor-archive/2026/IU/ANMO/BHZ.D/IU.ANMO.00.BHZ.D.2026.130'); \
  print(st); print('contiguous samples:', sum(tr.stats.npts for tr in st))"
```

Expected output: a single `Stream` containing one or more `Trace`s
that together cover roughly the run window (5 min × 20 Hz ≈ 6000
samples for ANMO BHZ). No "merge gap" warnings should appear if the
SeedLink stream itself was clean.

### 5. Crash-recovery sanity check (optional)

To exercise the per-path tail validator:

1. While the app is stopped, append a few junk bytes to the latest
   archive file (simulate a torn write):

   ```bash
   printf 'XXXXX' >> /tmp/echosmonitor-archive/.../IU.ANMO.00.BHZ.D.2026.130
   ```

2. Restart the app. On the first packet that lands on this path, a
   single INFO log line should appear:

   ```
   mseed_writer_truncated_to_valid_record path=... kept_bytes=... lost_bytes=5
   ```

3. The file is now record-aligned again; new appends land cleanly.

### 6. Restart resumption

Restart the app (same config, no edits). Within ~30 s the DevicePanel
stats line should resume showing the *cumulative* archive footprint
(`arch ... · 1 files` reflects the original size + the new appends).
The single SDS path on disk has grown — no second file appears for the
same UTC day.

## Archive integrity (M5 stage B)

Goal: prove the SQLite metadata index agrees with the on-disk SDS
files across a stop / start cycle, that gaps detected by the live
gap detector are recorded with the correct kind / sample-count, and
that the DB is durable after a clean shutdown.

Prerequisite: complete the **Archive smoke test** above so an SDS tree
and an `archive.db` file exist under your `archive_root`.

### 1. Run two sessions back-to-back (~10 minutes each)

With archive enabled on `iris-iu-anmo`:

```bash
uv run python -m echosmonitor
# Run for ~10 minutes, then Ctrl+C.
uv run python -m echosmonitor
# Run for another ~10 minutes, then Ctrl+C.
```

### 2. Inspect the SQLite schema

```bash
sqlite3 /tmp/echosmonitor-archive/archive.db .schema
```

Expected tables: `_meta`, `sessions`, `devices`, `streams`, `gaps`,
`files`. The schema version should be `1`:

```bash
sqlite3 /tmp/echosmonitor-archive/archive.db \
  "SELECT value FROM _meta WHERE key='schema_version'"
```

### 3. Confirm two sessions, one device, one stream, one file

```bash
sqlite3 /tmp/echosmonitor-archive/archive.db <<EOF
SELECT count(*) AS sessions FROM sessions;
SELECT count(*) AS devices  FROM devices;
SELECT count(*) AS streams  FROM streams;
SELECT count(*) AS files    FROM files;
SELECT count(*) AS gaps     FROM gaps;
EOF
```

Expected:
* `sessions = 2` (one row per run; both have non-null `ended_at`).
* `devices = 1` (UPSERT collapses two runs into one row).
* `streams = 1` (single NSLC across both sessions).
* `files = 1` if both runs landed in the same UTC day, otherwise 2.
* `gaps`: usually 0–2 for a clean network; non-zero means the live
  detector caught discontinuities — that's interesting, not broken.

### 4. Cross-check DB vs disk

```bash
sqlite3 /tmp/echosmonitor-archive/archive.db \
  "SELECT path, bytes FROM files"
```

For each row, compare `bytes` against the on-disk size. The DB is
gated on fsync, so its `bytes` value can lag the on-disk size by up
to one fsync interval; **the DB MUST NEVER claim more bytes than the
file actually has.**

```bash
ls -l "$(sqlite3 /tmp/echosmonitor-archive/archive.db 'SELECT path FROM files LIMIT 1')"
```

Read the file via ObsPy and compare its sample count against
`streams.total_packets * (typical samples per packet)`:

```bash
uv run python -c "
from obspy import read
import sqlite3
conn = sqlite3.connect('/tmp/echosmonitor-archive/archive.db')
row = conn.execute('SELECT path, total_packets FROM files JOIN streams USING(stream_id) LIMIT 1').fetchone()
path, total_packets = row[0], row[1]
print('DB total_packets:', total_packets)
st = read(path)
print('SDS file:', path)
print('SDS samples:', sum(tr.stats.npts for tr in st))
"
```

For ANMO BHZ at ~20 Hz with the fake server packet size (~50 samples
each), the SDS sample count should equal
`total_packets * 50` ± 50 samples (in-flight at sampling time).

### 5. Inspect any gaps the detector caught

```bash
sqlite3 /tmp/echosmonitor-archive/archive.db \
  "SELECT t_start, t_end, samples_missing, kind FROM gaps ORDER BY t_start"
```

`kind` is one of `gap` (positive `samples_missing`), `overlap`
(negative), or `rate_change` (zero — the stream's sample rate
changed). Gaps the live detector caught are durable across restarts
and visible from any read-only connection.

### 6. Clean shutdown invariants

After both runs, every session row must have `ended_at` set. A
non-null `ended_at` means `engine.stop()` ran to completion; if you
see `ended_at IS NULL` for an old session, that engine was killed
mid-run (no clean Ctrl+C).

```bash
sqlite3 /tmp/echosmonitor-archive/archive.db \
  "SELECT id, started_at, ended_at FROM sessions ORDER BY id"
```

## Spectrogram smoke test (M6 stage 1)

Goal: confirm the live spectrogram pane appears under each filtered
TracePlot, the marine microseism band is visible, the per-device
toggle works, and color modes don't crash.

### 1. Use an IRIS station with a default bandpass

A `config.yaml` snippet:

```yaml
devices:
  - name: iris
    host: rtserve.iris.washington.edu
    port: 18000
    selectors:
      - { network: IU, station: ANMO, location: "00", channel: BHZ }
    dsp_chain:
      - { type: detrend, kind: constant }
      - { type: bandpass, freqmin: 0.5, freqmax: 10.0, corners: 4, zerophase: false }
```

### 2. Launch the app

```bash
uv run python -m echosmonitor
```

Within ~5 seconds of CONNECTED:

- A spectrogram pane appears below the filtered TracePlot (the bottom
  half of the raw/filtered stack).
- The `Spectrogram` dock at the bottom of the window contains one tab
  per active stream — open it and the same waterfall is visible at
  full size.
- The IU.ANMO.00.BHZ marine microseism band (0.05–0.5 Hz, visible as a
  sustained horizontal band at low frequency) appears within ~30 s of
  data once the spectrogram has rolled out enough columns.

### 3. Switch color modes

In the spectrogram-pane header, swap the color combo across:

- `z-score` (default — surfaces structure without calibration)
- `dB` (absolute power; -20 to 60 dB display range)
- `linear` (raw power; mostly useful for sanity checking)

Each swap clears the waterfall and rebuilds under the new transform.
No crashes; the X axis stays consistent.

### 4. Per-device spectrogram toggle

On the device group header, the `spec` toolbutton hides/shows every
spectrogram pane belonging to that device in one click. The setting
persists across app restarts (stored under
`QSettings("EchosMonitor")` / `Spectrograms/<device_name>`).

### 5. Chain hot-reload resets the spectrogram

While the app is running, edit the device's `dsp_chain` to remove the
bandpass and save the file (or use the Stations browser's
subscribe-stream flow). The processed-trace plot reverts to raw and
the spectrogram pane resets its frequency axis to the raw sample rate
within ~1 s.

## PSD on quiet station (M6 stage 2)

Goal: prove the PSD widget computes a Welch curve from a live ring
buffer, the NLNM/NHNM reference overlays bracket the trace, and
overlay/auto-refresh / window-length controls behave.

### 1. Use a broadband IRIS channel

YAML snippet:

```yaml
devices:
  - name: iris
    host: rtserve.iris.washington.edu
    port: 18000
    selectors:
      - { network: IU, station: ANMO, location: "00", channel: BHZ }
```

### 2. Launch and open the PSD tab

```bash
uv run python -m echosmonitor
```

Click the **PSD** tab in the central tab group (between **Live** and
**HVSR**).

### 3. Inspect the curve

Once a few packets land:

- Pick `iris / IU.ANMO.00.BHZ` in the `Stream:` combo.
- Set `Window: 5 min`.
- The PSD curve fills out within ~5 s (one auto-refresh tick at
  `max(5 s, 5 min / 4)` = 75 s; press `Refresh` for an immediate
  redraw).
- Marine microseism peaks (~6 s and ~12 s periods, i.e. 0.08 Hz and
  0.17 Hz) should be visible as a hump in the curve.

### 4. Overlay a noisier station

Click `+ overlay` to pin the current stream as an overlay, then pick a
different station in the combo (use the `Devices` dock to add one).
Both curves should render in different colours with a legend on the
right.

### 5. NLNM/NHNM overlay (advanced)

The `NLNM/NHNM` toggle defaults **OFF**. The trace PSD is in dB rel.
counts²/Hz (raw counts straight from the ring buffer, no instrument
response removed) while the Peterson 1993 noise models are in dB rel.
(m/s²)²/Hz (acceleration). Overlaying the two without response
correction is a unit error of typically 80-150 dB; the overlay is
therefore only physically meaningful on channels already converted to
acceleration. The toggle stays in the UI for that case but is off by
default — instrument-response work arrives with M8.

If you opt in: check `NLNM/NHNM`. The grey dashed reference curves
appear at the next auto-refresh / manual refresh. Uncheck to hide.

### 6. Window-length scaling

Switch `Window: 30 s` → `60 s` → `5 min` → `15 min` → `1 h`. Each
change fires a fresh request immediately. With the 1 h window, the
auto-refresh interval is 15 min — pressing `Refresh` is the fastest
way to see an update.

### 7. Auto-refresh disable

Uncheck `auto`. The periodic timer stops; only the `Refresh` button
triggers a new compute. Re-check to resume.

## Tuning a filter live (M6 stage 3)

Goal: prove the interactive DSP chain editor renders a live preview,
validates parameters against Nyquist before save, persists the edited
chain through the existing M4 hot-reload path, and the engine
re-installs the spectrogram on the new ``fs_out``.

### 1. Use the same IRIS device as the spectrogram and PSD tests

```yaml
devices:
  - name: iris
    host: rtserve.iris.washington.edu
    port: 18000
    selectors:
      - { network: IU, station: ANMO, location: "00", channel: BHZ }
    dsp_chain: []
```

### 2. Open the chain editor

```bash
uv run python -m echosmonitor
```

In the `Devices` dock select the `iris` row and click `Edit device...`
(toolbar) — or simply **double-click** the device row. The DeviceDialog
opens; click `Edit chain...`. The chain editor opens with an empty
chain.

### 3. Build a chain incrementally

Click each palette button in turn:

- `Detrend` (kind=linear)
- `Bandpass` (freqmin=1.0, freqmax=20.0, corners=4)
- `Decimation` (factor=2)

After each addition the live preview panel below should re-render
within ~200 ms (the debounce). The top mini-plot shows raw counts,
the bottom shows the post-chain output, and the inline spectrogram
shows the filtered band content.

### 4. Verify Nyquist validation

Set the Bandpass `freqmax` to a value above Nyquist (e.g. 60 Hz on
the IRIS BHZ stream at 40 Hz). The form's freqmax box paints red and
shows a tooltip; the editor's status banner turns red with
`Chain invalid: ...`. OK and Apply both grey out. Reset to 20 Hz —
banner reverts, buttons re-enable, preview redraws.

### 5. Zerophase warning

Tick `Zerophase` on the Bandpass form. A yellow caveat note appears
inline: live streaming forces causal regardless of this checkbox, so
ticking it is documented as "offline review only". Save / apply
still works.

### 6. Apply vs OK

Press **Apply**: the editor stays open, but the changes are mirrored
back into the surrounding DeviceDialog's working state.
Press **OK** on the chain editor: it closes, the DeviceDialog's
chain summary updates. Press **OK** on the DeviceDialog: the new
chain is persisted via `ConfigStore.update_device`.

### 7. Hot-reload visible

Watch the main window's central LiveStack pane: the chain reinstalls
within ~1 s of the DeviceDialog's OK. The TracePlot's processed row
shows the bandpassed signal, the spectrogram pane below it resets to
the new `fs_out` (decimation halved 40 Hz to 20 Hz Nyquist) and
starts filling at the chain's column rate.

### 8. Cancel discards

Re-open the chain editor, change the bandpass, press **Cancel**. The
DeviceDialog's chain summary is unchanged; the saved chain on disk
is unchanged; the live engine is unchanged.

## Layout and navigation (M7 + central-tabs restructure)

Goal: confirm the central-tabs layout, focus mode, dock detach,
the per-device Live tabs, and the keyboard-shortcuts reference all
behave on a real screen. None of these touch networking or DSP — run
against any connected device (the IRIS device from the M2 section is
fine) or even with no devices configured.

The window is organised as **a solid central tab group flanked by four
docks**:

- **Centre (central widget):** a tab group **Detections | Live | PSD |
  HVSR | Archive**. This is the app's core view and is *not* detachable. The
  **Detections** tab is a master-detail split — the detection table on
  the left, the "why did this fire?" detail pane on the right; selecting
  a row populates the detail in the same tab.
- **Left sidebar (docks):** **Devices** + **Stations**, tabbed.
- **Bottom (docks, full width):** **Spectrogram** + **Log** (Log is a
  not-yet-implemented placeholder), tabbed.

> Returning users: a window state saved by an older 9-dock build is
> tolerated — the four surviving docks re-place by name and the stale
> Live/PSD/HVSR/Detections dock entries are ignored. If anything
> looks off after upgrading, **View → Reset window layout…**.

### 1. Default layout breathes

Reset to defaults first so you see the from-scratch layout:

1. **View → Reset window layout…** → **Yes**.
2. The window resizes to **1600×1000**. The central tab group fills the
   middle; the Devices/Stations sidebar sits on the left; Spectrogram +
   Log run full-width along the bottom.
3. Drag the sidebar / bottom splitters — the window stays horizontally
   resizable (the sidebar and the central tabs both resize freely; no
   frozen splitters), and each dock stops at its minimum: Spectrogram
   ≥ 250 px tall, Devices/Stations ≥ 220 px wide. Switch to the PSD tab
   and select a detection on the Detections tab — the central tabs never
   pin a wide minimum (the long-title squeeze/freeze bug stays fixed).

### 2. Focus mode on docks and on the central tabs

1. Click into the **Spectrogram** dock, press **F11**. The Spectrogram
   dock fills the whole window (the central tabs and the other docks
   hide) and a thin focus banner appears at the top.
2. Press **Esc** — the previous layout is restored exactly.
3. Repeat for **Devices**, **Stations**, **Log**: click the dock (or its
   title bar ⛶ button), **F11** to focus, **Esc** (or **F11** again) to
   restore. Each must round-trip back to the same positions.
4. **Central-tabs focus:** click inside the central tab group (e.g. the
   Live tab), press **F11**. Now *all four docks* hide and the central
   tabs maximise; the banner shows the current tab's name. **Esc**
   restores the docks exactly.

### 3. Detach a bottom dock to a floating window

1. **Ctrl+Shift+3** (or **View → Detach → Spectrogram**, or the ⧉ button
   on the Spectrogram title bar) detaches the Spectrogram dock into a
   floating OS window. (The central tabs are *not* detachable — only the
   four docks are.)
2. If you have a second monitor, drag the floating window onto it and
   resize. The spectrogram keeps updating.
3. **Double-click the floating window's title bar** to re-dock it; it
   returns to its remembered bottom position.

### 4. Per-device Live tabs + CPU bound

1. Add **three** devices (Devices dock toolbar → **+ Add device** three
   times, or paste three blocks into your YAML). Use the IRIS, GFZ, and
   a third public server from the M2/M3 tables.
2. The **Live** central tab shows an **All** sub-tab plus one sub-tab
   per device, each with a small connection-state dot in the tab label.
3. Switch between device tabs — the view changes instantly and shows
   recent data immediately. Only the **visible** tab redraws at full
   rate; hidden tabs keep rolling their buffers but skip the costly
   `setData`/`setImage` calls (the setData-pause mechanism). Watch a
   process monitor: CPU stays bounded with one visible tab even with
   three devices streaming.
4. In a device tab, toggle individual **stream chips** in the chip
   toolbar above the plots — each chip shows/hides one NSLC in that tab
   only. The choice persists across restarts.

### 5. Keyboard-shortcuts reference

1. **Help → Keyboard shortcuts…** opens a read-only modal listing every
   shortcut in groups (Focus, Docks, Devices, Live view, Application).
2. Confirm it lists **F11** / **Esc** (focus), **Alt+1..4** /
   **Ctrl+Shift+1..4** (dock toggle / detach with the four dock names:
   Devices, Stations, Spectrogram, Log), **Ctrl+N** / **Ctrl+E** /
   **Del** (device new/edit/remove), and **Ctrl+Q** (quit), plus the
   Live-tab navigation note.
3. Close it — it is read-only and never mutates state.

### 6. Reset returns everything to default

1. Enter focus mode on any dock (**F11**) and detach a couple of docks
   to floating windows.
2. **View → Reset window layout…** → **Yes**.
3. Everything returns to the default layout: focus mode exits, the
   banner disappears, floating docks re-dock, and the window resizes to
   1600×1000. Open devices and the config on disk are unaffected — only
   `geometry` and `windowState` in QSettings are cleared.

## Detections end-to-end (M8)

Goal: prove STA/LTA triggers flow all the way through — live table,
markers on the trace + spectrogram, the central "why did this fire?"
pane, persistence, and recent-on-startup history. Builds on the
"Bandpass on ANMO with STA/LTA tap" chain above.

### 1. Make it fire on ambient noise (for testing only)

Edit your IRIS device's `sta_lta` stage to a deliberately low
`on_threshold` so it triggers on background noise:

```yaml
    - { type: sta_lta, sta: 1.0, lta: 30.0, on_threshold: 1.8, off_threshold: 1.2 }
```

This is a *test* setting — a real deployment uses `on_threshold: 3.5`
or higher (step 4 restores it).

### 2. Run the app and watch detections appear

1. Launch: `uv run python -m echosmonitor`.
2. Click the **Detections** tab (the leftmost central tab). Within a
   minute or two of live data (after the LTA warm-up of ~one `lta`
   window), rows appear,
   newest at the top: Time · Device · NSLC · Kind (`sta_lta`) · Score ·
   Duration · Δ-from-previous. An in-progress trigger shows a ticking
   `open Ns` duration that freezes once it closes.
3. On the **Live** trace for that stream, an **amber onset line**
   appears at each detection, becoming a shaded amber region once the
   trigger closes. Toggle the trace's **⚑** header button — markers on
   that trace hide/show. Toggle **View ▸ Show detection markers** — all
   markers hide/show at once.
4. Open the **Spectrogram** dock (**Alt+3**) for the same stream: a thin
   amber vertical line marks the same detection time, aligned with the
   trace (shared wall-clock axis).

### 3. Inspect "why did this fire?"

1. Click a detection row. The **detail pane on the right of the
   Detections tab** switches from the "Select a detection…" hint to the
   detail view: the trace segment on top, the recomputed **STA/LTA ratio
   curve** below with the dashed **on** and **off** threshold lines and
   the trigger window shaded. (The whole flow stays inside the
   Detections tab — table on the left, detail on the right.)
2. Double-click a row: the central tab group switches to the **Live**
   tab, focused on that device's sub-tab.
3. Right-click a row → **Copy as ObsPy snippet**; paste it into a
   Python REPL to confirm it is a valid `read()` + `trim()` one-liner.
4. Filter the table by device / NSLC / minimum score / time window —
   rows disappear from the view but the underlying counts are unchanged
   (filtering is view-side).

### 4. Restart → recent detections reappear as history

1. Quit the app (**Ctrl+Q**) and relaunch.
2. The Detections table is **pre-populated** with the last 24 h of
   detections (up to `ui.recent_detections_limit`, default 200), shown
   **dimmed** to mark them as historical (read from the DB index — no
   waveforms are loaded).
3. Double-click an old historical row whose data is no longer in the
   live buffer: the Detections-tab detail pane shows the honest
   *"archive replay arrives in a later milestone"* message rather than a
   fabricated trace.

### 5. Restore a sane threshold

1. Edit the `sta_lta` stage back to `on_threshold: 3.5,
   off_threshold: 1.5` and save.
2. Confirm the false triggers stop: no new rows appear on quiet
   background, and the table stops growing.

### 6. Troubleshooting

- **No detections ever appear.** The empty-state message names the
  cause: the stream needs a `sta_lta` stage in its `dsp_chain`. Confirm
  the chain (Stations dock → chain editor) and that the device is
  CONNECTED with data flowing.
- **Detections appear but no markers.** Check **View ▸ Show detection
  markers** is ticked and the per-trace **⚑** button is enabled.
- **Spectrogram has no markers.** Markers are placed on the wall-clock
  Spectrogram *dock* view; the small inline spectrogram pane under each
  trace uses a column-index axis and is intentionally not marked.

## High-sample-rate throughput (render decoupling)

Verifies the fix for the high-fs incident: rendering must never throttle
acquisition/DSP/detection/storage (CLAUDE.md rule 11). The automated
guards (`tests/core/test_high_rate_load.py`,
`tests/gui/test_trace_plot_decimation.py`) prove the flush tick is not
gated by render latency and that display is decimated; these manual steps
confirm it on a real high-rate device.

### 1. Point the app at the Echos device at 500 Hz × 3 channels

1. Configure the Echos device (Stations dock or `config`) for 500 Hz on
   3 channels, with a DSP chain that includes a band/highpass filter and
   a `sta_lta` stage, and `archive.enabled: true`.
2. Launch with debug logs to a file so the drop/disconnect markers are
   greppable:

   ```bash
   uv run python -m echosmonitor --log-level DEBUG 2>&1 | tee /tmp/highfs.log
   ```

### 2. Run for 5 minutes and watch the science-loss markers

Let it stream for at least 5 minutes, then check the log:

```bash
# SCIENCE-LOSS markers — these must be ABSENT:
grep -c dsp_chain_drop              /tmp/highfs.log   # detection lost samples
grep -c streaming_engine_archive_backpressure /tmp/highfs.log  # storage lost samples
# No server-side disconnect / reconnect cycle — must be ABSENT:
grep -E "Errno 9|seedlink_state.*RECONNECTING|seedlink_connect_failed" /tmp/highfs.log
# EXPECTED / benign (informational, not loss):
grep ring_buffer_allocated          /tmp/highfs.log   # once per stream (memory cost)
grep ring_buffer_saturated          /tmp/highfs.log   # once per stream (display window full)
grep -c ring_buffer_overwrite       /tmp/highfs.log   # DEBUG-only; steady rolling
```

PASS = zero `dsp_chain_drop`, zero `archive_backpressure`, and no
`[Errno 9]` disconnect/reconnect cycle for the whole run.

`ring_buffer_saturated` (INFO, once per stream) and `ring_buffer_overwrite`
(DEBUG) are **expected and benign**: the per-stream ring is a fixed
60 s display/snapshot *history*, so once it fills it rolls the oldest
sample on every packet — that is not science loss (DSP, detection and
storage are fed on independent queues before the ring). The trace plot
is fed by the coalescer, not the ring. A device like the Echos that
emits large packets (e.g. 4096 samples ≈ 8 s at 500 Hz) and dumps a
buffered backlog on connect will saturate the ring within the first
~25 s — that startup burst is exactly the window that, pre-fix, stalled
the coupled render and starved the worker into a `[Errno 9]` disconnect
around t+37 s. Post-fix the burst is absorbed with no `dsp_chain_drop`,
no `archive_backpressure`, and no disconnect.

The live-plot **refresh cadence follows the device's packet cadence**
(≈ 8 s for 4096-sample packets), not the configured `refresh_hz` — the
app cannot render data that has not arrived yet. The spectrogram dock
updates roughly once per second.

### 3. Confirm the plots are continuous and the filtered plot is non-empty

1. The raw trace scrolls **continuously** (min/max decimated for display
   — transients/spikes are still visible, the line is not gappy).
2. In a stacked (filtered) view, the **lower/filtered plot is NOT empty**
   — it carries data within a few seconds of connecting. (This was the
   "second graph shows nothing" bug.)
3. Optionally raise the device to 1–4 kSPS and repeat: the display stays
   smooth (it decimates to `ui.max_display_rate_hz`, default 250), while
   detection and the archive still see the full rate.

### 4. CPU with everything on (V5)

1. With display + spectrogram + DSP + detection + archive all enabled at
   500 Hz × 3 channels, observe CPU for the app process:

   ```bash
   # PID of the app, then sample CPU% for ~30 s:
   pidstat -p $(pgrep -f echosmonitor) 5 6
   ```

2. Note the steady-state CPU%. It should be a small single-digit
   percentage on a modern laptop and should NOT climb over time (no
   render-driven backlog). Report the number against the pre-fix
   behaviour (which pegged a core and fell progressively behind).

## Physical units (M11)

Display fixed windows in physical units (velocity m/s, acceleration
m/s², displacement m) by deconvolving the instrument response. Counts
remain the source of truth; physical units are an **on-demand display
transform on FIXED windows only** — the live scrolling plots
intentionally stay in counts.

### 1. Configure response metadata for a device

1. Get a response file for a real station. For **IU.ANMO** you can fetch
   StationXML from a datacentre:
   ```bash
   uv run python -c "from obspy.clients.fdsn import Client; \
   Client('IRIS').get_stations(network='IU', station='ANMO', \
   location='00', channel='BHZ', level='response').write('anmo.xml', \
   format='STATIONXML')"
   ```
   For the **Echos** device, use the user's own StationXML / dataless /
   RESP file (the channels were renamed HN\* → EH\*/HH\* at the device +
   metadata level; the deconvolution simply trusts the response's native
   output unit, whatever the channel code says).
2. **Devices** panel → add/edit the device → set **Response metadata** to
   that file (the **Browse…** picker yields an absolute path) and leave
   the format on **Auto-detect**. Click **OK**.
3. A bad/unreadable file is rejected at save with a clear error — the
   dialog stays open so you can fix the path. A blank field = counts only
   (the device's physical-unit options stay disabled, with a tooltip).

### 2. Switch units on a detection / archive window

1. Let the device run so a detection lands in the **Detections** table
   (or use a known archived event). Select the row — the Detections-tab
   detail pane (right of the table) shows the trace (counts) + the
   STA/LTA ratio.
2. In the pane's **unit selector** (top-right), pick **Velocity (m/s)**.
   The top trace deconvolves OFF the GUI thread (a brief "computing…"),
   then the Y axis relabels to **m/s** and the waveform rescales. The
   bottom ratio/probability plot stays on counts (unchanged).
3. Try **Acceleration (m/s²)** and **Displacement (m)** — the axis
   relabels each time. Pick **Counts** to revert.
4. **Physical-sanity check:** for a quiet broadband the background noise
   should be of order ~1e-7 m/s (velocity). Acceleration looks
   "whiter" (high-frequency-emphasised), displacement "redder"
   (long-period-emphasised) — the expected integration/differentiation
   relationship.
5. Right-click a detection row → **Inspect in physical units → …** opens
   the pane already showing that unit.

### 3. Graceful degradation + honesty

1. Select a detection on a device WITHOUT response metadata: the three
   physical options are **disabled** with the tooltip *"No response
   metadata for this channel — set response_metadata in the device
   config."* Counts always works.
2. If a window straddles an archive **gap**, a physical request fails
   honestly (status-bar "Physical units unavailable: … gaps …") and the
   pane reverts to counts — it never shows counts mislabelled as m/s.
3. The deconvolution is a wait (rule 7): watch the structured log for
   `deconvolution_start` / `deconvolution_done` with `elapsed_s`. It runs
   on a **dedicated worker thread**, never the science DSP thread, so it
   cannot back-pressure live acquisition (rule 11).

## Archive tab (browse + static view + measurement + hand-off)

Prerequisite: let a device archive for a while (or point at an existing
SDS archive) so the `files` index has data. Open the **Archive** tab.

### 1. Browse — real extent + gaps, sensible default interval

1. Pick a **Device** and a 3-component **Station**. The line below the
   pickers reads `Archived: <start> → <end>` with the **real** recorded
   span (never a 1999/epoch placeholder), and the green/dark **coverage
   strip** shows where data exists vs gaps over the chosen range.
2. The **from/to** fields default to a recent slice **inside** the
   extent (the last ~10 min of available data). Adjust them as desired.
3. Pick a stream with NOTHING archived: the line reads *"No archived data
   for this stream."* and **Load window** is disabled (honest empty
   state).

### 2. Load — static 3C view + spectrogram + physical units

1. Click **Load window**. The 3C traces (Z/N/E) render on a wall-clock
   UTC axis; toggle **Stacked / Overlaid**. Any archive **gap** shows as
   a line-break, never interpolated.
2. A **spectrogram** of the primary (Z) component appears below
   (frequency Hz vs wall-clock). It was built off the GUI thread — watch
   the log for `archive_window_load_start` / `archive_window_load_done`
   with `elapsed_ms` (rule 7). The live drain keeps advancing during the
   load (rule 11) — a streaming device does not disconnect.
3. With Echos StationXML (response metadata) configured, switch **Units**
   to **Velocity (m/s)**: the Z/N/E traces relabel + rescale to m/s
   (Counts to revert). Gappy components stay in counts.

### 3. Measure — Δt and a frequency estimate by eye

1. Drag the two vertical cursors (red **A**, yellow **B**). The readout
   panel shows each cursor's **UTC time** and **amplitude** (in the
   current unit) on the **Cursor on:** component.
2. Place **A** and **B** on two successive peaks: the readout shows
   **Δt**, **Δamplitude**, and **f = 1/Δt (Hz)** + period — the manual
   "estimate a frequency by eye" tool. **Reset view** refits the window.

### 4. Hand-off — run HVSR on the exact window

1. Click **Run HVSR on this window**: the app switches to the **HVSR**
   tab with the device/station + archive **from/to** prefilled to the
   exact interval. Click **Run on archive** there — it computes HVSR over
   that window (the Archive tab does not reimplement HVSR).
2. **Round-trip check:** the window HVSR runs on is exactly the one
   you selected/measured — same start/end, same streams. No silent
   re-interpretation.
