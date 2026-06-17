# EchosMonitor

Desktop monitoring, recording, and analysis application for **Echos** seismic
devices running the `firmware_seedlink` variant (ESP32-S3 node, 3 velocimeter
channels + optional HN1, SeedLink v3 on TCP 18000, HTTP REST API). See
`CLAUDE.md` for the architecture contract and `ROADMAP.md` for the milestone
plan and decision log.

## Install

Download the artifact for your OS from the
[latest release](https://github.com/recinall/EchosMonitor/releases/latest):

| OS | Artifact | Notes |
|----|----------|-------|
| **Windows** | `EchosMonitor-<ver>-windows-setup.exe` (installer) or `…-windows-portable.zip` | Unsigned — SmartScreen may warn; choose "More info → Run anyway". |
| **Linux** | `EchosMonitor-<ver>-x86_64.AppImage` | `chmod +x` then run. |
| **macOS** | `EchosMonitor-<ver>.dmg` | Unsigned — right-click → Open, or `xattr -dr com.apple.quarantine /Applications/EchosMonitor.app`. |

### System requirements

- **Windows:** Windows 10/11 (x64).
- **Linux:** a glibc-based x86_64 distribution with the usual desktop libraries
  (the AppImage bundles the rest).
- **macOS:** **macOS 13 (Ventura) or newer**, Apple Silicon or Intel. *Earlier
  macOS (12 and below) is not supported* — the bundled Qt (PySide6 6.11)
  requires macOS 13+.

Artifacts are currently **unsigned**; code signing / notarization is planned
(ROADMAP M7-D).

## Develop

Requires Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                      # install (dev group included)
uv run echosmonitor          # launch
uv run pytest                # test gate (excludes -m perf)
uv run ruff check src tests
uv run mypy src
```
