"""Pure instrument-response deconvolution core (M11 Stage A).

Converts seismic data in COUNTS to physical units by removing the
instrument response read from station metadata (StationXML, dataless
SEED, or RESP). Output units are:

* ``VEL``  -> ground velocity in m/s,
* ``ACC``  -> ground acceleration in m/s**2,
* ``DISP`` -> ground displacement in m,

*regardless* of the sensor type. ObsPy's :meth:`Trace.remove_response`
reads the response's native output unit from the metadata and
integrates/differentiates to the requested output. This is why the
"is the channel a velocimeter or an accelerometer?" question is
irrelevant here: the metadata defines the native unit and ObsPy converts
to whatever ``output`` we ask for.

Design constraints (CLAUDE.md):

* This module is *pure*. It takes an :class:`obspy.Stream` and an
  :class:`obspy.Inventory` and returns a *new* Stream. It never mutates
  the source Stream/Trace (counts are the source of truth, rule 8), never
  writes files, never touches a database, and pulls in no Qt.
* Deconvolution is a *wait* (rule 7): each call logs a structured line at
  start and at completion with the elapsed wall time. The caller is
  responsible for running it off the GUI thread.
"""

from __future__ import annotations

import hashlib
import io
import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import obspy
import structlog

from echosmonitor.core.exceptions import ResponseError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from obspy.core.inventory import Inventory
    from obspy.core.inventory.response import Response
    from obspy.core.stream import Stream
    from obspy.core.utcdatetime import UTCDateTime

    from echosmonitor.config.schema import DeviceConfig

_log = structlog.get_logger(__name__)

# ----------------------------------------------------------------------
# Pre-filter corner fractions.
#
# ``remove_response`` divides the spectrum by the (complex) instrument
# response. At the long-period (low-frequency) end the response gain
# tends to zero (no DC sensitivity), so that division blows up; at the
# short-period (near-Nyquist) end the anti-alias response rolls off, so
# dividing there amplifies noise. The 4-corner cosine ``pre_filt`` tapers
# both ends BEFORE the division: it passes ``[low_pass, high_pass]`` flat
# and cosine-tapers down to zero outside ``[low_stop, high_stop]``.
# ----------------------------------------------------------------------

# Lower stop/pass corners are absolute frequencies in Hz (independent of
# fs): below 0.005 Hz the deconvolution is fully suppressed; the band
# ramps up to flat by 0.01 Hz. These stabilise the unstable long-period
# end where the response gain -> 0.
_PRE_FILT_LOW_STOP = 0.005
_PRE_FILT_LOW_PASS = 0.01

# Upper pass/stop corners are fractions of the sampling rate: flat up to
# 0.45*fs, cosine-tapered to zero at Nyquist (0.5*fs), so noise near
# Nyquist (where the anti-alias filter rolls off) is not amplified.
_PRE_FILT_HIGH_PASS_FRAC = 0.45
_PRE_FILT_HIGH_STOP_FRAC = 0.5

# Instrument-aware LOW corners (the new default when no explicit pre_filt
# is supplied) are derived from the response's own corner frequency
# ``f0 = (smallest non-zero |pole|) / (2*pi)`` rather than hard-coded at a
# broadband value. Below f0 a velocimeter's gain rolls off as omega**2, so
# dividing there inverts that roll-off and amplifies sub-corner noise into
# a spurious low-frequency lobe (a 4.5 Hz geophone tapered down to 0.01 Hz
# is inverted by ~(4.5/0.01)**2 ~ 2e5). Tapering from ``f0/2`` up to ``f0``
# stops the deconvolution exactly where the instrument itself stops being
# sensitive, generalising correctly across sensor classes (a broadband's
# ~0.0083 Hz lowest pole yields corners near its own corner; a 4.5 Hz
# geophone yields ~2.25/4.5 Hz).
_PRE_FILT_LOW_STOP_F0_FRAC = 0.5  # low_stop = f0 * 0.5  (i.e. f0/2)
_PRE_FILT_LOW_PASS_F0_FRAC = 1.0  # low_pass = f0 * 1.0  (i.e. f0)

# Mapping from this module's lower-case format aliases to the format
# strings ObsPy's ``read_inventory`` expects.
_FORMAT_MAP: dict[str, str] = {
    "stationxml": "STATIONXML",
    "dataless": "SEED",
    "resp": "RESP",
}

# Module-level inventory cache keyed on (resolved-path-str, mtime). A
# fresh mtime (file rewritten) yields a new key, so the cache invalidates
# itself naturally without an explicit eviction path.
_INVENTORY_CACHE: dict[tuple[str, float], Inventory] = {}

# M6.6-B: parsed-inventory cache for StationXML BLOBS (persisted device
# XML, not a file). Keyed on the blob's sha1 so a re-parse of the same
# bytes is free; a changed blob is a new key. Bounded by the small number
# of distinct device StationXML documents in play.
_BLOB_INVENTORY_CACHE: dict[str, Inventory] = {}


def inventory_from_stationxml_blob(xml: str) -> Inventory:
    """Parse a raw FDSN StationXML string into an ObsPy :class:`Inventory`.

    Cached by blob hash (see :data:`_BLOB_INVENTORY_CACHE`). Mirrors
    :func:`load_inventory` but sources the bytes from memory (the device
    StationXML persisted per session, M6.6-B) rather than a file.

    Raises:
        ResponseError: the blob cannot be parsed as StationXML.
    """
    key = hashlib.sha1(xml.encode("utf-8")).hexdigest()
    cached = _BLOB_INVENTORY_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        inv = obspy.read_inventory(io.BytesIO(xml.encode("utf-8")), format="STATIONXML")
    except Exception as exc:  # obspy raises a broad family here
        raise ResponseError(f"failed to parse StationXML blob: {exc}") from exc
    _BLOB_INVENTORY_CACHE[key] = inv
    return inv


def load_inventory(path: Path, fmt: str = "auto") -> Inventory:
    """Read station metadata into an ObsPy :class:`Inventory`, with caching.

    Args:
        path: Filesystem path to a StationXML, dataless SEED, or RESP file.
        fmt: One of ``"auto"`` (let ObsPy sniff the format — the default),
            ``"stationxml"``, ``"dataless"``, or ``"resp"``. Anything other
            than ``"auto"`` is mapped to the corresponding ObsPy format
            string and passed explicitly.

    Returns:
        The parsed :class:`Inventory`. Repeated calls for the same path
        return the *same* cached object as long as the file's
        modification time is unchanged.

    Raises:
        ResponseError: if ``fmt`` is not a recognised alias, or the file
            cannot be read/parsed (the path is named in the message).
    """
    if fmt != "auto" and fmt not in _FORMAT_MAP:
        raise ResponseError(
            f"unknown inventory format {fmt!r}; expected 'auto' or one of {sorted(_FORMAT_MAP)}"
        )

    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise ResponseError(f"cannot stat inventory file {str(path)!r}: {exc}") from exc

    cache_key = (str(path), mtime)
    cached = _INVENTORY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        if fmt == "auto":
            inv = obspy.read_inventory(str(path))
        else:
            inv = obspy.read_inventory(str(path), format=_FORMAT_MAP[fmt])
    except Exception as exc:  # obspy raises a broad family here
        raise ResponseError(f"failed to read inventory from {str(path)!r}: {exc}") from exc

    _INVENTORY_CACHE[cache_key] = inv
    return inv


def default_pre_filt(fs: float) -> tuple[float, float, float, float]:
    """Derive a 4-corner cosine pre-filter from the sampling rate.

    The four corners ``(low_stop, low_pass, high_pass, high_stop)`` bound
    the band that is kept flat before the spectral division in
    :meth:`Trace.remove_response`:

    * ``low_stop=0.005 Hz`` / ``low_pass=0.01 Hz`` form a high-pass-like
      ramp that suppresses the unstable long-period end of the
      deconvolution. The instrument response gain tends to zero at DC, so
      dividing by it there amplifies noise without bound; tapering from
      0.005 -> 0.01 Hz stabilises it.
    * ``high_pass=0.45*fs`` / ``high_stop=0.5*fs`` taper from 0.45 of the
      sampling rate up to Nyquist (0.5*fs). Near Nyquist the anti-alias
      response rolls off, so dividing there amplifies high-frequency
      noise; the taper avoids that.

    Args:
        fs: Sampling rate in Hz.

    Returns:
        ``(low_stop, low_pass, high_pass, high_stop)`` in Hz, suitable as
        the ``pre_filt`` argument to :meth:`Trace.remove_response`.
    """
    return (
        _PRE_FILT_LOW_STOP,
        _PRE_FILT_LOW_PASS,
        _PRE_FILT_HIGH_PASS_FRAC * fs,
        _PRE_FILT_HIGH_STOP_FRAC * fs,
    )


def instrument_pre_filt(response: Response, fs: float) -> tuple[float, float, float, float] | None:
    """Derive instrument-aware pre-filter corners from a response.

    The LOW corners are anchored to the instrument's *own* corner frequency
    ``f0 = (smallest non-zero |pole| across all PAZ stages) / (2*pi)``
    rather than a hard-coded broadband value. Below f0 a velocimeter's gain
    rolls off (typically as omega**2), so dividing by the response there
    inverts the roll-off and amplifies sub-corner noise into a spurious
    low-frequency lobe. Tapering from ``f0 * _PRE_FILT_LOW_STOP_F0_FRAC``
    (low_stop) up to ``f0 * _PRE_FILT_LOW_PASS_F0_FRAC`` (low_pass) stops
    the deconvolution where the instrument stops being sensitive.

    The HIGH corners keep the existing fs-based anti-alias logic:
    ``high_pass = _PRE_FILT_HIGH_PASS_FRAC * fs`` (0.45*fs),
    ``high_stop = _PRE_FILT_HIGH_STOP_FRAC * fs`` (0.5*fs).

    Args:
        response: An ObsPy :class:`~obspy.core.inventory.response.Response`.
        fs: Sampling rate in Hz (sets the high anti-alias corners).

    Returns:
        ``(low_stop, low_pass, high_pass, high_stop)`` in Hz, or ``None``
        when the response carries no identifiable PAZ pole (coefficient- or
        sensitivity-only stages). The caller falls back to
        :func:`default_pre_filt` in that case. ``None`` is also returned
        when the derived corners are pathological (``low_pass >= high_pass``,
        i.e. f0 above the anti-alias band) so the caller can fall back.
    """
    poles: list[complex] = []
    for stage in getattr(response, "response_stages", []) or []:
        stage_poles = getattr(stage, "poles", None)
        if stage_poles:
            poles.extend(stage_poles)

    nonzero_abs = [abs(p) for p in poles if abs(p) > 0.0]
    if not nonzero_abs:
        return None

    f0 = min(nonzero_abs) / (2.0 * math.pi)
    low_stop = f0 * _PRE_FILT_LOW_STOP_F0_FRAC
    low_pass = f0 * _PRE_FILT_LOW_PASS_F0_FRAC
    high_pass = _PRE_FILT_HIGH_PASS_FRAC * fs
    high_stop = _PRE_FILT_HIGH_STOP_FRAC * fs

    # Guard ordering: low_stop < low_pass < high_pass < high_stop. A
    # pathological f0 (above the anti-alias band) collapses the band; signal
    # the caller to fall back rather than hand remove_response a bad filter.
    if not (low_stop < low_pass < high_pass < high_stop):
        return None

    return (low_stop, low_pass, high_pass, high_stop)


class ResponseRemover:
    """Removes instrument responses from count data using one inventory.

    Holds a single :class:`Inventory` and converts COUNTS streams to
    physical units (VEL m/s, ACC m/s**2, DISP m) via ObsPy's
    :meth:`Trace.remove_response`. The output unit is determined purely by
    the ``output`` argument and the response metadata's native unit — the
    sensor type / channel-code naming does not matter, because the
    response carries its own native input/output units and ObsPy
    integrates or differentiates to reach the requested ``output``.

    The instance never mutates the streams handed to it (it works on a
    copy) and performs no I/O beyond reading the in-memory inventory.
    """

    def __init__(
        self,
        inventory: Inventory,
        pre_filt_override: tuple[float, float, float, float] | None = None,
    ) -> None:
        """Store the inventory used for all subsequent deconvolutions.

        Args:
            inventory: The in-memory :class:`Inventory` providing responses.
            pre_filt_override: Optional 4-corner cosine pre-filter in Hz
                (typically from the device's configured
                ``response_metadata.pre_filt``). When set, it takes
                precedence over the instrument-aware derived corners but is
                still overridden by an explicit ``pre_filt`` argument to
                :meth:`to_physical`. ``None`` leaves the per-trace
                instrument-aware derivation in charge.
        """
        self._inventory = inventory
        self._pre_filt_override = pre_filt_override

    def available_for(self, nslc: str, t: UTCDateTime) -> bool:
        """Report whether a matching response exists for ``nslc`` at ``t``.

        Args:
            nslc: A ``"NET.STA.LOC.CHA"`` identifier (an ObsPy ``Trace.id``).
            t: The time at which the response must be valid (typically the
                trace start time).

        Returns:
            ``True`` iff :meth:`Inventory.get_response` finds a response,
            ``False`` otherwise (no match, or any lookup failure).
        """
        try:
            self._inventory.get_response(nslc, t)
        except Exception:  # obspy raises a broad family; any miss is "unavailable"
            return False
        return True

    def response_fingerprint(self, nslc: str, t: UTCDateTime) -> tuple[object, ...] | None:
        """A comparable fingerprint of ``nslc``'s response at ``t``, or ``None``.

        ``(overall_sensitivity, input_units, output_units, stage_count)`` —
        enough to decide whether two channels carry the *same* response
        (an HVSR ratio of identical responses cancels, so counts are valid)
        without a brittle deep object comparison. ``None`` when no response
        matches. A public accessor so callers (e.g.
        :func:`echosmonitor.core.hvsr.responses_identical`) need not
        reach into the private inventory.
        """
        try:
            resp = self._inventory.get_response(nslc, t)
        except Exception:
            return None
        sens = getattr(resp, "instrument_sensitivity", None)
        value = getattr(sens, "value", None) if sens is not None else None
        in_u = getattr(sens, "input_units", None) if sens is not None else None
        out_u = getattr(sens, "output_units", None) if sens is not None else None
        n_stages = len(getattr(resp, "response_stages", []) or [])
        rounded = round(float(value), 6) if value is not None else None
        return (rounded, in_u, out_u, n_stages)

    def _resolve_pre_filt(
        self,
        explicit: tuple[float, float, float, float] | None,
        nslc: str,
        starttime: UTCDateTime,
        fs: float,
    ) -> tuple[float, float, float, float]:
        """Resolve the effective pre-filter for one trace, by precedence.

        Precedence (first that applies wins):

        1. ``explicit`` — the ``pre_filt`` argument to :meth:`to_physical`;
        2. the instance ``pre_filt_override`` (device config);
        3. the instrument-aware :func:`instrument_pre_filt` derived from the
           trace's own response (looked up defensively — any failure or a
           pole-free response falls through);
        4. :func:`default_pre_filt` from the sampling rate.

        Args:
            explicit: The caller-supplied 4-corner pre-filter or ``None``.
            nslc: The trace's ``"NET.STA.LOC.CHA"`` id (for response lookup).
            starttime: The trace start time (response epoch selector).
            fs: The trace sampling rate in Hz (high anti-alias corners).

        Returns:
            The 4-corner ``(low_stop, low_pass, high_pass, high_stop)`` in Hz.
        """
        if explicit is not None:
            return explicit
        if self._pre_filt_override is not None:
            return self._pre_filt_override

        try:
            resp = self._inventory.get_response(nslc, starttime)
            derived = instrument_pre_filt(resp, fs)
        except Exception as exc:  # obspy raises a broad family; degrade gracefully
            _log.warning(
                "instrument_pre_filt_lookup_failed",
                nslc=nslc,
                error=str(exc),
                fallback="default_pre_filt",
            )
            derived = None

        if derived is not None:
            return derived

        _log.info(
            "instrument_pre_filt_unavailable",
            nslc=nslc,
            reason="no_paz_pole_or_pathological_corners",
            fallback="default_pre_filt",
        )
        return default_pre_filt(fs)

    def to_physical(
        self,
        stream: Stream,
        output: Literal["VEL", "ACC", "DISP"],
        pre_filt: tuple[float, float, float, float] | None = None,
        water_level: float = 60.0,
    ) -> Stream:
        """Return a *new* Stream in physical units, response removed.

        Output-unit semantics: ``VEL`` -> m/s, ``ACC`` -> m/s**2,
        ``DISP`` -> m, regardless of the instrument type.
        :meth:`Trace.remove_response` reads the response's native output
        unit from the metadata and integrates/differentiates to the
        requested ``output``.

        The source ``stream`` is never mutated — work happens on
        ``stream.copy()`` (counts remain the source of truth, rule 8).

        Gap handling: a window that straddled an archive gap (the reader's
        ``merge(method=0)`` leaves a masked array) is *rejected*, not
        deconvolved per-segment. A fixed display window the user
        explicitly selected should be contiguous; per-segment
        deconvolution would silently change what is shown (each segment
        gets its own taper and edge transients). A multi-trace Stream of
        the same id that merges cleanly is fine — only true ``np.ma``
        masked gaps are rejected.

        Args:
            stream: Source Stream in COUNTS. Not mutated.
            output: Target physical unit (``"VEL"``, ``"ACC"``, ``"DISP"``).
            pre_filt: Optional 4-corner cosine pre-filter in Hz. This is the
                highest-precedence source: when supplied it is used for every
                trace verbatim. When ``None``, the effective pre-filter is
                resolved per-trace via :meth:`_resolve_pre_filt`: the
                instance ``pre_filt_override`` (device config) if set, else
                the instrument-aware :func:`instrument_pre_filt` derived from
                that trace's response, else :func:`default_pre_filt` from its
                sampling rate.
            water_level: Deconvolution water level in dB (clamps the
                inverse response gain to avoid division blow-up).

        Returns:
            A new Stream (the working copy) with every trace converted to
            ``output`` units.

        Raises:
            ResponseError: if any trace has no matching response for its
                NSLC/start time, or if any trace carries a masked gap.
        """
        out = stream.copy()
        for trace in out:
            nslc = trace.id
            starttime = trace.stats.starttime

            # Validate BOTH endpoints: a fixed window could in principle span
            # a response-epoch boundary (valid at start, invalid at end). If
            # either end has no matching response we refuse rather than apply
            # one epoch's response across the boundary.
            endtime = trace.stats.endtime
            if not self.available_for(nslc, starttime) or not self.available_for(nslc, endtime):
                raise ResponseError(
                    f"no instrument response for {nslc} over [{starttime}, {endtime}]; "
                    f"refusing to pass counts through as physical units"
                )

            if np.ma.isMaskedArray(trace.data):
                raise ResponseError(
                    f"window for {nslc} starting {starttime} contains gaps "
                    f"(masked data); deconvolution requires a contiguous window"
                )

            tr_pre_filt = self._resolve_pre_filt(
                pre_filt, nslc, starttime, trace.stats.sampling_rate
            )

            _log.info(
                "deconvolution_start",
                nslc=nslc,
                output=output,
                npts=int(trace.stats.npts),
            )
            started = time.perf_counter()
            trace.remove_response(
                inventory=self._inventory,
                output=output,
                pre_filt=tr_pre_filt,
                water_level=water_level,
                taper=True,
            )
            elapsed = time.perf_counter() - started
            _log.info(
                "deconvolution_done",
                nslc=nslc,
                output=output,
                elapsed_s=elapsed,
            )

        return out


class ResponseProvider:
    """Resolves a device's configured response metadata to a remover.

    A thin adapter between :class:`~echosmonitor.config.schema.
    DeviceConfig.response_metadata` and :class:`ResponseRemover`: it maps a
    device name to the :class:`ResponseRemover` built from that device's
    metadata file (or reports that none is configured). Inventory parsing is
    cached by ``(path, mtime)`` inside :func:`load_inventory`, so a fresh
    :class:`ResponseRemover` is rebuilt cheaply on every call and a rewritten
    metadata file is picked up automatically (no stale inventory).

    Pure: no Qt, no live-path coupling. Relative ``path`` values resolve
    against ``config_dir`` (the directory of the active config file), exactly
    like other config paths; absolute paths are used verbatim.
    """

    def __init__(self, devices: Iterable[DeviceConfig], config_dir: Path | None) -> None:
        """Index ``devices`` by name and remember the config directory.

        Args:
            devices: The configured devices (their ``response_metadata`` is
                read lazily, on each lookup).
            config_dir: Directory the active config file lives in, used to
                resolve relative metadata paths. ``None`` falls back to the
                current working directory.
        """
        self._devices = {d.name: d for d in devices}
        self._config_dir = config_dir
        # M6.6-B: per-device persisted/fetched StationXML blobs. Written on
        # the GUI thread (set_stationxml_blob) and read by the decon/HVSR
        # workers via remover_for, so a lock guards the dict (response.py is
        # not a rule-2 pure module; a small lock is sanctioned here).
        self._blobs: dict[str, str] = {}
        self._blob_lock = threading.Lock()

    def set_stationxml_blob(self, device_name: str, xml_blob: str | None) -> None:
        """Register (or clear) a device's StationXML blob (M6.6-B).

        ``xml_blob=None`` clears it. The config-file ``response_metadata``
        override still WINS over a registered blob (rule 16: explicit
        override > fetched StationXML). Call on the GUI thread.
        """
        with self._blob_lock:
            if xml_blob is None:
                self._blobs.pop(device_name, None)
            else:
                self._blobs[device_name] = xml_blob

    def is_configured(self, device_name: str) -> bool:
        """Whether ``device_name`` has resolvable response metadata.

        True when a config-file ``response_metadata.path`` is set OR a
        persisted/fetched StationXML blob is registered (M6.6-B) — both
        yield a :class:`ResponseRemover` from :meth:`remover_for`.
        """
        dev = self._devices.get(device_name)
        if dev is not None and dev.response_metadata.path is not None:
            return True
        with self._blob_lock:
            return device_name in self._blobs

    def _resolve_path(self, device_name: str) -> Path | None:
        dev = self._devices.get(device_name)
        if dev is None or dev.response_metadata.path is None:
            return None
        path = Path(dev.response_metadata.path)
        if path.is_absolute():
            return path
        base = self._config_dir if self._config_dir is not None else Path.cwd()
        return (base / path).resolve()

    def remover_for(self, device_name: str) -> ResponseRemover | None:
        """Build a :class:`ResponseRemover` for ``device_name``.

        Returns ``None`` when the device has no metadata configured.

        Raises:
            ResponseError: metadata is configured but the file cannot be
                read/parsed (surfaced so the UI can report it rather than
                silently disabling physical units).
        """
        path = self._resolve_path(device_name)
        if path is not None:
            dev = self._devices[device_name]
            inv = load_inventory(path, dev.response_metadata.format)
            return ResponseRemover(inv, pre_filt_override=dev.response_metadata.pre_filt)
        # No config-file override → fall back to a persisted/fetched
        # StationXML blob (M6.6-B). Parsing is cached by blob hash.
        with self._blob_lock:
            blob = self._blobs.get(device_name)
        if blob is not None:
            inv = inventory_from_stationxml_blob(blob)
            return ResponseRemover(inv)
        return None

    def available_for(self, device_name: str, nslc: str, t: UTCDateTime) -> bool:
        """Whether a usable response exists for ``device_name``/``nslc`` at ``t``.

        ``True`` only when metadata is configured, loads, AND contains a
        matching response. Any load/parse failure yields ``False`` (the UI
        treats it as "no physical units available"); use :meth:`remover_for`
        when you need the failure surfaced as an exception.
        """
        try:
            remover = self.remover_for(device_name)
        except ResponseError:
            return False
        if remover is None:
            return False
        return remover.available_for(nslc, t)
