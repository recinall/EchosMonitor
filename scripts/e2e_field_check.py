"""M6.5-E headless field check — record the REAL device, verify A/B/C.

Loads the user's real config, starts a recording session on the named
device through the real StreamingEngine (exactly the engine path the
GUI drives), records for N minutes, stops, then analyses what landed on
disk:

* zero archive backpressure (stage A/C acceptance on real hardware);
* no gap/overlap chatter from stamp jitter (stage B) — segments on
  disk must be contiguous except for genuine disconnects;
* record fill / bytes / sample-count accounting.

Monitoring/streaming the device is user-authorized; this script makes
NO device-config writes and never touches the REST API (SeedLink TCP
only — no credentials needed).

Usage:
    uv run python scripts/e2e_field_check.py [--minutes 4]
        [--device echos] [--project M65_E2E_Claude] [--config PATH]
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from obspy import read
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from echosmonitor.config.loader import load_config
from echosmonitor.core.streaming_engine import StreamingEngine


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=4.0)
    ap.add_argument("--device", default="echos")
    ap.add_argument("--project", default="M65_E2E_Claude")
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication([])

    cfg, cfg_path = load_config(args.config)
    print(f"config: {cfg_path}")
    if args.device not in [d.name for d in cfg.devices]:
        print(f"device {args.device!r} not in config — abort")
        return 2

    engine = StreamingEngine(cfg)
    backpressure: list[tuple[str, int]] = []
    write_failures: list[str] = []
    engine.archiveBackpressure.connect(lambda d, n: backpressure.append((d, n)))
    engine.archiveWriteFailed.connect(lambda _d, _n, r: write_failures.append(r))

    print(f"recording {args.minutes:.1f} min into project {args.project!r} …")
    engine.start_session(args.project, [args.device])

    stop_timer = QTimer()
    stop_timer.setSingleShot(True)
    stop_timer.setInterval(int(args.minutes * 60_000))
    stop_timer.timeout.connect(app.quit)
    stop_timer.start()
    t0 = time.monotonic()
    app.exec()
    wall = time.monotonic() - t0

    status = engine.device_status().get(args.device)
    engine.stop()

    print(f"\n=== run: {wall:.0f}s wall")
    ok = True
    if status is None:
        print("NO DeviceStatus — device never registered")
        return 2
    print(
        f"packets={status.packets_received} bytes_rx={status.bytes_received} "
        f"archived_bytes={status.archive_bytes_written} "
        f"gaps={status.archive_gaps_total} overlaps={status.archive_overlaps_total} "
        f"last_error={status.archive_last_error!r}"
    )
    if status.packets_received == 0:
        print("FAIL: no packets received (device unreachable?)")
        return 2
    if backpressure:
        print(f"FAIL(A/C): archiveBackpressure fired: {backpressure}")
        ok = False
    else:
        print("PASS(A/C): zero archive backpressure")
    if write_failures:
        print(f"WARN: writeFailed events: {write_failures[:5]}")

    # On-disk analysis. Session root = <archive_root>/<project>/.
    base = cfg.app.archive_root
    if base is None:
        from platformdirs import user_data_dir

        base = Path(user_data_dir("echosmonitor")) / "archive"
    project_root = Path(base) / args.project
    files = sorted(p for p in project_root.rglob("*.D.*") if p.is_file())
    if not files:
        print(f"FAIL: no archive files under {project_root}")
        return 2
    print(f"archive files: {len(files)} under {project_root}")
    total_segments = 0
    for f in files:
        st = read(str(f))
        n_seg = len(st)
        total_segments += n_seg
        npts = sum(tr.stats.npts for tr in st)
        span = st[-1].stats.endtime - st[0].stats.starttime
        expected = round(span * st[0].stats.sampling_rate) + 1
        fill = npts / (f.stat().st_size // 512)
        offs = []
        for a, b in itertools.pairwise(st):
            offs.append(
                round(
                    (b.stats.starttime - (a.stats.endtime + a.stats.delta))
                    * a.stats.sampling_rate,
                    2,
                )
            )
        print(
            f"  {f.name}: segments={n_seg} npts={npts} expected~{expected} "
            f"coverage={npts / max(1, expected):.4f} samples/record={fill:.1f} "
            f"segment_offsets={offs}"
        )
        # Stage B acceptance on real jitter: no small-offset segment
        # splits (|offset| <= 5 samples would be jitter chatter the
        # rectifier should have absorbed).
        small = [o for o in offs if abs(o) <= 5.0]
        if small:
            print(f"  FAIL(B): jitter-scale segment splits on disk: {small}")
            ok = False
    if ok:
        print("\nVERDICT: PASS — A/C (no backpressure) and B (no jitter splits) hold on real hardware")
    else:
        print("\nVERDICT: FAIL — see lines above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
