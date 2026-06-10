"""SeisComP Data Structure (SDS) path helpers.

Pure functions: no I/O, no Qt, no global state. The MseedWriter and
metadata indexer compose these to translate (UTCDateTime, StreamID) ↔
file path and to split a trace at UTC midnight before writing.

SDS layout (per ``.claude/skills/miniseed-sds/SKILL.md``)::

    {archive}/{year}/{network}/{station}/{channel}.D/{net}.{sta}.{loc}.{cha}.D.{year}.{doy}

* ``year`` is 4-digit, ``doy`` is 3-digit zero-padded day-of-year.
* ``D`` (data) is the type marker — ``R`` (raw) and ``Q`` (quality
  controlled) variants exist in the SDS spec but the live archive
  only writes ``D``.
* Empty SEED location codes render as ``..`` in the filename
  (``IU.ANMO..BHZ.D.2026.130``), which is the SDS canonical
  representation.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from obspy import UTCDateTime

if TYPE_CHECKING:
    from obspy import Trace

from seedlink_dashboard.core.models import StreamID

_SECONDS_PER_DAY = 86_400

# Characters allowed verbatim in a single sanitized path segment.
_SAFE_SEGMENT_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_COLLAPSE_UNDERSCORES = re.compile(r"_+")
# Number of hex chars from the sha1 digest used for the empty-result fallback.
_FALLBACK_HASH_LEN = 8


def sanitize_device_name(name: str) -> str:
    """Make a device name safe for use as ONE SDS path segment.

    The result is a single filesystem path component (never contains a
    separator) drawn only from ``[A-Za-z0-9._-]``. The sanitization,
    applied in order, is:

    1. Every character not in ``[A-Za-z0-9._-]`` (spaces, ``/``, ``\\``,
       and any other reserved or unicode character) is replaced with a
       single underscore.
    2. Consecutive underscores are collapsed to one.
    3. Leading and trailing ``_`` and ``.`` characters are stripped, so
       the segment never begins or ends with a dot (which would create a
       hidden file or trailing-dot directory) or an underscore.
    4. The path-traversal names ``.`` and ``..`` are rejected: any input
       that reduces to one of them maps to the empty-result fallback.
    5. If the result is empty (e.g. the name was all separators), fall
       back to a short deterministic token ``device_`` + the first
       ``_FALLBACK_HASH_LEN`` hex chars of ``sha1(name)`` so distinct
       names keep distinct directories.

    Args:
        name: The user- or YAML-supplied device name. May contain
            arbitrary characters when loaded from YAML.

    Returns:
        A non-empty, separator-free, filesystem-safe path segment.

    Note:
        This function is NOT injective: distinct raw names can collapse to
        the same segment (e.g. ``"a/b"``, ``"a b"`` and ``"a_b"`` all map to
        ``"a_b"``; ``"Echos"`` and ``"Echos_"`` both map to ``"Echos"``).
        Two devices sharing a sanitized segment would share one physical SDS
        tree — the cross-device collision this layout exists to prevent.
        Global injectivity across a config's devices is therefore enforced at
        config-load time by
        :meth:`~seedlink_dashboard.config.schema.RootConfig._devices_map_to_distinct_archive_dirs`,
        which rejects any config whose device names collapse together.
    """
    replaced = _SAFE_SEGMENT_CHARS.sub("_", name)
    collapsed = _COLLAPSE_UNDERSCORES.sub("_", replaced)
    stripped = collapsed.strip("_.")
    if stripped in ("", ".", ".."):
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:_FALLBACK_HASH_LEN]
        return f"device_{digest}"
    return stripped


def device_sds_root(base_root: Path, device_name: str) -> Path:
    """Return the per-device SDS root above a full standard SDS tree.

    The returned path is ``base_root / sanitize_device_name(device_name)``
    — the per-device SDS root above a full standard SDS
    ``YEAR/NET/STA/CHAN.D`` tree. Namespacing the SDS tree per device
    keeps two devices that emit the same SEED NSLC writing to physically
    distinct files.

    Args:
        base_root: The shared archive base root.
        device_name: The owning device's configured name (sanitized here).

    Returns:
        The per-device SDS root.
    """
    return base_root / sanitize_device_name(device_name)


def sds_path(root: Path, t: UTCDateTime, sid: StreamID) -> Path:
    """Build the canonical SDS path for one (timestamp, stream).

    Args:
        root: Archive root directory.
        t: The UTC timestamp of any sample on the target day. Only the
            calendar date is used (the time-of-day component is
            ignored).
        sid: SEED naming tuple.

    Returns:
        The SDS path. Caller is responsible for ``mkdir(parents=True)``
        on ``path.parent`` before opening the file.
    """
    year_str = f"{t.year:04d}"
    doy_str = f"{t.julday:03d}"
    filename = f"{sid.network}.{sid.station}.{sid.location}.{sid.channel}.D.{year_str}.{doy_str}"
    return root / year_str / sid.network / sid.station / f"{sid.channel}.D" / filename


def parse_sds_path(p: Path) -> tuple[StreamID, int, int] | None:
    """Inverse of :func:`sds_path`.

    Returns ``(StreamID, year, doy)`` if ``p`` matches the SDS layout
    (the last five path components plus filename grammar), otherwise
    ``None``. Used by future re-indexers; absent from the M5 hot path.

    Note:
        Only the trailing five SDS components are parsed; the per-device
        segment added by :func:`device_sds_root` sits ABOVE them and is not
        returned. A re-indexer that needs the owning device must read it from
        the path component above ``<year>`` itself.
    """
    parts = p.parts
    if len(parts) < 5:
        return None
    year_str, net, sta, cha_d, filename = parts[-5:]
    if len(year_str) != 4 or not year_str.isdigit():
        return None
    if not cha_d.endswith(".D"):
        return None
    cha_from_dir = cha_d[:-2]
    fparts = filename.split(".")
    # net.sta.loc.cha.D.year.doy → 7 segments. Empty loc renders as an
    # empty segment, preserving the count.
    if len(fparts) != 7:
        return None
    f_net, f_sta, f_loc, f_cha, type_marker, f_year, f_doy = fparts
    if type_marker != "D":
        return None
    if f_net != net or f_sta != sta or f_cha != cha_from_dir or f_year != year_str:
        return None
    if len(f_doy) != 3 or not f_doy.isdigit():
        return None
    return (
        StreamID(network=f_net, station=f_sta, location=f_loc, channel=f_cha),
        int(year_str),
        int(f_doy),
    )


def day_bounds(t: UTCDateTime) -> tuple[UTCDateTime, UTCDateTime]:
    """Return ``(start_of_day, next_midnight)`` in UTC for the day of ``t``.

    Both bounds are :class:`UTCDateTime` instances. Treat the interval
    as half-open ``[start_of_day, next_midnight)``: a sample at exactly
    ``next_midnight`` belongs to the next day, not this one.
    """
    start = UTCDateTime(t.year, t.month, t.day)
    next_midnight = start + _SECONDS_PER_DAY
    return start, next_midnight


def split_at_midnight(trace: Trace) -> list[Trace]:
    """Split ``trace`` if it straddles UTC midnight.

    Returns one trace if every sample lies within the same UTC day, or
    two traces (pre-midnight, post-midnight) if any sample lies in the
    next day. Boundary convention matches the SDS skill: a trace whose
    last sample is *exactly* at midnight stays whole (the midnight
    sample is kept with the day on which the trace started). Splits
    only fire when ``endtime > next_midnight`` — strict greater-than.

    Args:
        trace: ObsPy ``Trace``. Not mutated; copies are returned.

    Returns:
        ``[trace]`` or ``[pre, post]`` as ObsPy traces. Empty traces
        are not produced — callers can rely on each returned trace
        having at least one sample.
    """
    _, next_midnight = day_bounds(trace.stats.starttime)
    if trace.stats.endtime <= next_midnight:
        return [trace]
    delta = trace.stats.delta
    pre = trace.copy()
    pre.trim(endtime=next_midnight - delta, nearest_sample=False)
    post = trace.copy()
    post.trim(starttime=next_midnight, nearest_sample=False)
    result: list[Trace] = []
    if pre.stats.npts > 0:
        result.append(pre)
    if post.stats.npts > 0:
        result.append(post)
    return result
