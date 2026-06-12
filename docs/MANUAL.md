# EchosMonitor — Field Manual

EchosMonitor records and analyses data from **Echos** seismic nodes
(`firmware_seedlink` variant: 3 velocimeter channels + optional HN1,
SeedLink server on TCP 18000, HTTP REST API). This manual walks the
field workflow end to end: **deploy → configure → record → HVSR →
report**.

Nothing in EchosMonitor starts by itself. Acquisition has three explicit
states per device — **Idle → Monitoring → Recording** — and every
transition is yours.

---

## 1. Deploy

1. Power the Echos node and connect it to the site network.
   * **Already on your WiFi/LAN** — the device announces itself over
     mDNS as `echos.local` (or similar); skip to §2.
   * **Factory fresh** — the device starts its own WiFi access point.
     Join that WiFi; the device answers at `http://192.168.4.1`. Its
     initial admin password is printed **once** on the device serial
     console at first boot — note it down. (To force AP mode later,
     hold button B for ≥ 5 s.)
2. Give the GNSS antenna sky view. Timestamps are only as good as the
   clock discipline — see §3 "Clock health".

## 2. Configure (add the device)

On a fresh install the **first-run wizard** opens by itself (later:
Help → First-run wizard). Three steps:

1. **Welcome** — choose *Find my device on the network* (mDNS scan,
   recommended), *setup (AP) mode* (probes `192.168.4.1`), or skip.
2. **Find your Echos device** — discovered nodes appear as they are
   confirmed; a node that does not advertise on mDNS can be entered
   manually by host name ("Check device" runs the same verification).
   Only verified Echos nodes are selectable.
3. **Name and credentials** — pick a device name and (optionally) enter
   the admin password. The password goes to the **OS keyring**, never
   into the config file; it is needed only to *change* settings on the
   device — reading always works without it. The stream selectors are
   set up automatically from the device's own StationXML channel list.

Devices can also be added any time from the **Devices** dock:
**+ Add device** (manual) or **Discover…** (mDNS scan with the same
verification; already-configured nodes are marked).

Server-side settings (sampling/OSR, gains, SeedLink port/ring/auth,
calibration, reboot) live **on the device** and are edited through the
device dialog (double-click the device row, or Edit): the dialog reads
the current values from the REST API and writes them back with
read-modify-write semantics. A SeedLink-config write triggers the
device's in-place 7-step restart; the dialog shows the progress and the
app reconnects automatically. After 5 wrong passwords the device locks
authenticated requests for a growing window (30 s → … → 1 h) — the
dialog shows the wait; don't hammer.

## 3. Monitor

Select the **Live** tab and press **▶ Monitor** in the session toolbar.
Monitoring shows live traces, PSD, spectrogram and detections — and
writes **nothing** to disk.

The Devices dock is the health surface. Its *Echos* column updates from
the status poller:

```
fw 1.4.2 · up 2h · 1 cli · ring 12% · GNSS 9sat · clk PPS
```

**Clock health** (`clk`) is first-class:

| Token | Meaning | Timestamp quality |
|---|---|---|
| `clk PPS` | GNSS time + PPS PLL locked | sample-accurate (best) |
| `clk GNSS` | GNSS time, PPS not locked | second-accurate |
| `clk NTP` | network time only | network accuracy |
| `clk hold (!)` | sources lost, clock drifting | degrading — check antenna |
| `clk none (!)` | never synchronized | unreliable — fix before recording |

Don't start a survey recording while a station shows `(!)`.

## 4. Record

1. Press **⏺ Record…** and give the session a **project name** — the
   project is the archive unit: everything lands under
   `<archive root>/<project>/<device>/<SDS tree>` as MiniSEED day
   files, with a per-project metadata index (`archive.db`).
2. Choose which devices record. Recording = monitoring + disk writes;
   the per-device badge turns **● REC** (loud red).
3. The toolbar shows project · elapsed · bytes written.
4. **⏹ Stop** ends the session immediately. A session interrupted by a
   crash is closed (flagged dirty) on the next launch — the data files
   themselves are always the source of truth.

The archive root is set in **File → Settings…** (empty = the platform
default; the dialog shows the exact path). Settings take effect at the
next launch; the archive root applies to new sessions.

## 5. Review (Archive tab)

The **Archive** tab lists recording sessions by project name and date.
Select a session to browse its waveforms, export windows (MiniSEED /
SAC / CSV), and hand a time range to the HVSR tools. The selected
session is also what the HVSR Array's archive mode reads — see below.

## 6. HVSR

### Single station (HVSR tab)

Pick the device (its Z/N/E group is detected), set the window length /
smoothing / frequency band, and start. Windows accumulate live and the
H/V curve refines as N grows; SESAME reliability and clarity criteria
are evaluated on every recompute. Reject/accept individual windows by
clicking them. *Run on archive* computes the same analysis over a
recorded time range instead of the live stream.

### Multi-station (HVSR Array tab)

Tick several stations; all share one settings panel. Each station
accumulates its **own** windows (a dropout on one station never stalls
the others) and gets its own H/V curve, f₀/T₀/A₀ and SESAME verdicts —
curves are **compared, never averaged across stations** (f₀ varies with
the subsurface; a cross-station mean is not a defined quantity). Note
A₀ comparisons across stations are response-sensitive; f₀ comparisons
are not.

*Run on archive* runs the array over a recorded range: select the
session in the Archive tab first — that session's data is what gets
read (the message tells you which archive root was searched if nothing
is found).

The **Map** tab shows the stations at their reported positions (from
the device GNSS/StationXML, or your manual override in the device
dialog) and can overlay the array's f₀ as a colour ramp — a quick
spatial read of resonance variation across the site.

## 7. Report

From the HVSR tabs: **Save PDF report** (single-station page, or the
array comparison page followed by one page per station) and **Export
JSON** (the full numbers: curves, windows, criteria, settings,
geometry). Array reports record that positions were resolved at *run*
time. Reports are never written half-finished — the file appears only
complete.

---

## Quick reference

| Action | Where |
|---|---|
| Add / discover a device | Devices dock → **+ Add device** / **Discover…** |
| First-run wizard again | Help → First-run wizard |
| Device settings (on-device) | double-click device row → device dialog |
| Start live view | session toolbar **▶ Monitor** |
| Start recording session | **⏺ Record…** (project name) |
| Stop everything | **⏹ Stop** |
| App settings (archive root, theme, display) | File → **Settings…** |
| Browse recordings | **Archive** tab |
| H/V analysis | **HVSR** / **HVSR Array** tabs |
| Station map + f₀ overlay | **Map** tab |

## Troubleshooting

* **Device not discovered** — it may sit on another subnet or not
  advertise on mDNS: add it manually by host name or IP. mDNS names
  (`echos.local`) are preferred in the config because they survive
  DHCP lease changes.
* **`(auth_failed)` after renaming a device** — the stored password is
  keyed by the device name; re-enter it in the device dialog after a
  rename.
* **`429 / locked out`** — too many wrong passwords; wait out the
  window shown in the dialog (a device reboot also clears it).
* **`clk hold (!)` that never recovers** — check the GNSS antenna and
  sky view; NTP needs the site network to reach a time server.
* **Empty "no archived data" result with a root path in the message** —
  the HVSR archive run searched that directory; check the Archive tab's
  selected session and the time range.
* **Lost admin password** — it cannot be recovered from the app. Read
  it from the device serial console at boot, or factory-reset to AP
  mode (button B ≥ 5 s) and set it up again.
