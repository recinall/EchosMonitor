"""Waveform window exports — MiniSEED and CSV (M3-C, rule 8).

All file writing lives here, in ``storage/``, and is ATOMIC in the
repo's canonical shape (skill ``miniseed-sds``): temp file in the same
directory → flush + fsync → ``os.replace``. A cancelled or failed
export leaves NO partial file at the destination — the temp is
unlinked on every non-success path.

The functions are pure I/O over data the caller already read
(rule 2: no Qt, no engine state); the archive read itself happens on
the export worker's thread via :class:`ArchiveReader`. MiniSEED is
written from the SPLIT stream so explicit gaps stay separate records
and obspy preserves the original sample values; CSV scatters the
components onto one shared time grid with gaps as empty cells (never
interpolated — the same honesty contract the plots follow).
"""

from __future__ import annotations

import csv
import os
from typing import TYPE_CHECKING

import numpy as np
import structlog
from obspy import Stream, UTCDateTime

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

_log = structlog.get_logger(__name__)

# Cooperative-stop poll cadence for the CSV row loop (rule 7): a
# day-long high-rate window is tens of millions of rows — the worker's
# stop flag must be observable while it streams out.
_CSV_STOP_POLL_ROWS = 50_000


class ArchiveExportError(Exception):
    """An export could not produce a complete file at the destination."""


class ExportCancelledError(ArchiveExportError):
    """The export was cooperatively cancelled; no file was produced."""


def _atomic_tmp(path: Path) -> Path:
    """The temp-file path used for the atomic write (same dir → same fs)."""
    return path.with_name(path.name + ".tmp")


def write_window_mseed(stream: Stream, path: Path) -> int:
    """Write a read-back window to ``path`` as MiniSEED; return bytes.

    ``stream`` is the (possibly masked) merged window from
    :meth:`ArchiveReader.read_window`. ``split()`` turns masked gaps
    back into separate contiguous traces, so the written records carry
    exactly the archived samples — gaps stay gaps, dtype is preserved
    and obspy picks the matching encoding.

    Raises:
        ArchiveExportError: empty stream (nothing archived in the
            window) or the filesystem write failed.
    """
    contiguous = stream.split() if len(stream) else stream
    if len(contiguous) == 0 or sum(int(tr.stats.npts) for tr in contiguous) == 0:
        raise ArchiveExportError("nothing archived in the selected interval")
    tmp = _atomic_tmp(path)
    try:
        contiguous.write(str(tmp), format="MSEED")
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        n_bytes = tmp.stat().st_size
        os.replace(tmp, path)
    except ArchiveExportError:
        _unlink_quiet(tmp)  # uphold the no-partial-artifact contract
        raise
    except Exception as exc:
        _unlink_quiet(tmp)
        raise ArchiveExportError(f"MiniSEED write failed: {exc}") from exc
    _log.info(
        "archive_export_mseed_written",
        path=str(path),
        n_traces=len(contiguous),
        n_bytes=n_bytes,
    )
    return int(n_bytes)


def write_window_csv(
    path: Path,
    epochs: np.ndarray,
    columns: Mapping[str, np.ndarray],
    *,
    header_meta: Mapping[str, str] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Write one shared-grid trace window to ``path`` as CSV; return bytes.

    ``epochs`` is the regular time grid; ``columns`` maps a column name
    (the NSLC) to its sample array (same length, ``NaN`` at gaps —
    rendered as EMPTY cells, never interpolated values). ``header_meta``
    becomes leading ``# key: value`` comment lines. ``should_stop`` is
    polled every ``_CSV_STOP_POLL_ROWS`` rows (rule 7) — on cancel the
    temp file is removed and :class:`ExportCancelledError` raised, so a
    partial CSV can never appear at the destination.
    """
    if epochs.size == 0 or not columns:
        raise ArchiveExportError("nothing archived in the selected interval")
    names = list(columns)
    arrays = [np.asarray(columns[name], dtype=np.float64) for name in names]
    if any(arr.shape[0] != epochs.shape[0] for arr in arrays):
        raise ArchiveExportError("column length mismatch against the time grid")
    tmp = _atomic_tmp(path)
    try:
        with open(tmp, "w", newline="") as fh:
            for key, value in (header_meta or {}).items():
                fh.write(f"# {key}: {value}\n")
            writer = csv.writer(fh)
            writer.writerow(["time_iso", "epoch", *names])
            for i in range(epochs.shape[0]):
                if (
                    should_stop is not None
                    and i % _CSV_STOP_POLL_ROWS == 0
                    and should_stop()
                ):
                    raise ExportCancelledError("export cancelled")
                epoch = float(epochs[i])
                row = [str(UTCDateTime(epoch)), repr(epoch)]
                for arr in arrays:
                    v = arr[i]
                    row.append("" if not np.isfinite(v) else repr(float(v)))
                writer.writerow(row)
            fh.flush()
            os.fsync(fh.fileno())
        n_bytes = tmp.stat().st_size
        os.replace(tmp, path)
    except ExportCancelledError:
        _unlink_quiet(tmp)
        raise
    except ArchiveExportError:
        _unlink_quiet(tmp)
        raise
    except Exception as exc:
        _unlink_quiet(tmp)
        raise ArchiveExportError(f"CSV write failed: {exc}") from exc
    _log.info(
        "archive_export_csv_written",
        path=str(path),
        n_rows=int(epochs.shape[0]),
        n_columns=len(names),
        n_bytes=n_bytes,
    )
    return int(n_bytes)


def _unlink_quiet(tmp: Path) -> None:
    try:
        tmp.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best-effort cleanup
        _log.warning("archive_export_tmp_unlink_failed", path=str(tmp))
