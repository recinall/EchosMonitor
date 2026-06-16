"""Typed configuration schema (pydantic v2).

Mirror of ``config/default.yaml``. Every config key is modeled here; loader
deep-merges user YAML over the bundled defaults before validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from echosmonitor.core.hvsr import HvsrSettings

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
Theme = Literal["dark", "light"]


class _Base(BaseModel):
    """Frozen, strict base for all config models."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AppConfig(_Base):
    data_dir: Path | None = None
    log_level: LogLevel = "INFO"
    log_json: bool = False
    # Archive root resolved at engine start: when null, falls back to
    # ``platformdirs.user_data_dir("echosmonitor","EchosMonitor")/archive``.
    # Per-device ``archive.root_dir`` overrides this when set; this field is
    # the default applied to every device whose ``archive.root_dir`` is null.
    archive_root: Path | None = None
    # M6.6-D: max lines retained by the in-app Log tab's bounded buffer
    # (drop-oldest). Caps both the sink deque and the view's block count.
    log_max_lines: Annotated[int, Field(ge=1, le=100000)] = 1000


class UiConfig(_Base):
    theme: Theme = "dark"
    default_window_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    refresh_hz: Annotated[int, Field(ge=1, le=120)] = 20
    # Total visible TracePlots across all devices. Streams beyond this
    # cap stay constructed and receive data; only their visibility is
    # toggled. Most-recently-seen wins (engine-driven for M3 part 1; a
    # per-stream user toggle arrives in M3 part 2).
    max_visible_plots: Annotated[int, Field(ge=1, le=64)] = 8
    # Currently UNWIRED: the startup prefill went away with autostart
    # (rule 13); the cross-session detection-history prefill is an open
    # M3 item and this field is its knob when it lands. Deliberately not
    # exposed in the settings dialog until then.
    recent_detections_limit: Annotated[int, Field(ge=0, le=10000)] = 200
    # High-fs display throttle (rule 11): the maximum effective sample
    # rate handed to the trace *renderer*. Streams faster than this are
    # min/max (peak) decimated FOR DISPLAY ONLY — the engine ring buffer,
    # DSP, detection, and storage always keep full rate. Caps the per-flush
    # rendered point count at ``window_seconds * max_display_rate_hz`` (or
    # the plot pixel budget, whichever is smaller). 250 Hz is plenty for a
    # ~1500 px-wide plot; raise it only when zooming into short windows.
    max_display_rate_hz: Annotated[int, Field(ge=1, le=20000)] = 250


class StreamSelectorConfig(_Base):
    network: str = "*"
    station: str = "*"
    location: str = "*"
    channel: str = "*"


class ReconnectConfig(_Base):
    initial_delay_s: Annotated[float, Field(ge=0.1, le=3600.0)] = 1.0
    max_delay_s: Annotated[float, Field(ge=0.1, le=3600.0)] = 60.0
    # Bound the TCP handshake we drive before handing the socket to obspy.
    # The OS default (~127 s on Linux via tcp_syn_retries=6) is unacceptable
    # for a user-facing dashboard; 10 s is generous enough for legitimate
    # transcontinental links yet quick enough to surface a SYN-blackhole.
    # The lower bound 0.5 s rules out flapping configurations; the upper
    # bound 300 s rules out "infinite" misconfigurations.
    connect_timeout_s: Annotated[float, Field(ge=0.5, le=300.0)] = 10.0

    @model_validator(mode="after")
    def _max_ge_initial(self) -> ReconnectConfig:
        if self.max_delay_s < self.initial_delay_s:
            raise ValueError("reconnect.max_delay_s must be >= initial_delay_s")
        return self


class ArchiveConfig(_Base):
    """Per-device archive settings (M5).

    The ``encoding`` choice must be compatible with the data dtype the
    device actually emits. We do not know the dtype at config-load time
    (it arrives with the first packet), so the writer enforces the
    constraint at write time:

    * ``STEIM2`` + integer dtype → STEIM2 (preferred, lossless+packed).
    * ``STEIM2`` + float dtype  → falls back to ``FLOAT32`` and emits
      one INFO log per stream. The chosen encoding is reported back via
      ``MseedWriter.write`` so the engine can record what actually went
      to disk (for the metadata index in stage B).
    * ``FLOAT32`` → ``FLOAT32`` regardless of dtype (data cast as
      needed; ``int64`` casts to ``int32`` with overflow detection).
    * Other combinations raise on first write with an explicit message.
    """

    enabled: bool = False
    format: Literal["mseed_sds"] = "mseed_sds"
    root_dir: Path | None = None
    encoding: Literal["STEIM2", "STEIM1", "FLOAT32"] = "STEIM2"
    record_length: Literal[256, 512, 1024, 2048, 4096] = 512
    # Per-writer LRU cap on open file handles. Default 32 covers ~16
    # streams/device (typical) with a 2x safety margin. Operators
    # running many devices with many streams each may want to raise
    # this; the upper bound 1024 mirrors typical Linux ulimit -n.
    max_open_files: Annotated[int, Field(ge=4, le=1024)] = 32
    # Periodic fsync interval. Trades durability against syscall cost;
    # 5 s ≈ 12 fsyncs/min/file. The DB metadata index is gated on
    # fsync (DB-after-fsync invariant), so this also bounds DB lag.
    fsync_interval_s: Annotated[float, Field(ge=0.5, le=60.0)] = 5.0
    # M6.5-B: packet-stamp jitter tolerance for the archive gap
    # detector, in absolute milliseconds (device clock wobble is a
    # time property, not a sample count — the real echos.local stamps
    # wander up to ~±5 ms at 500 Hz in gap/overlap pairs that net
    # zero). Within the tolerance (floored at half a sample period)
    # packets are treated as contiguous and their stamps are SNAPPED
    # onto the expected sample grid before writing: no spurious
    # gap/overlap events, no MiniSEED record fragmentation. Beyond it
    # a real discontinuity is declared and the grid re-anchors to the
    # device stamps. Honest cost: a real discontinuity smaller than
    # the tolerance is absorbed as up to one tolerance of absolute
    # timing bias — inside the device's own stamping noise — and the
    # bias persists until the next over-tolerance event re-anchors.
    # 0 keeps only the half-sample floor.
    jitter_tolerance_ms: Annotated[float, Field(ge=0.0, le=100.0)] = 10.0
    # In-flight WARN threshold for the archive seam (M6.5-A). The
    # engine posts recorded packets straight to the storage QThread and
    # NEVER drops them (the archive is the science sink — the old
    # engine-side drop-oldest inbox lost recorded data during a replay
    # burst on the first field run); when more than this many traces
    # are in flight (sent minus writer-acked) the engine logs + emits
    # ``archiveBackpressure``, throttled. The name is kept for config
    # compatibility.
    queue_max: Annotated[int, Field(ge=16, le=1_000_000)] = 1024


# ---------------------------------------------------------------------------
# DSP chain — discriminated union on the ``type`` tag.
# ---------------------------------------------------------------------------


class DetrendStage(_Base):
    type: Literal["detrend"]
    kind: Literal["linear", "constant", "demean"] = "linear"


class TaperStage(_Base):
    type: Literal["taper"]
    max_pct: Annotated[float, Field(gt=0.0, le=0.5)] = 0.05


class BandpassStage(_Base):
    type: Literal["bandpass"]
    freqmin: Annotated[float, Field(gt=0.0)]
    freqmax: Annotated[float, Field(gt=0.0)]
    corners: Annotated[int, Field(ge=1, le=12)] = 4
    # Default is causal (False): live streaming must run forward-only so the
    # filter state can be carried across packet boundaries. The DSP factory
    # warn-logs and forces False when a live chain is configured with True.
    zerophase: bool = False

    @model_validator(mode="after")
    def _band_ordered(self) -> BandpassStage:
        if self.freqmin >= self.freqmax:
            raise ValueError("bandpass.freqmin must be < freqmax")
        return self


class HighpassStage(_Base):
    type: Literal["highpass"]
    freq: Annotated[float, Field(gt=0.0)]
    corners: Annotated[int, Field(ge=1, le=12)] = 4
    zerophase: bool = False


class LowpassStage(_Base):
    type: Literal["lowpass"]
    freq: Annotated[float, Field(gt=0.0)]
    corners: Annotated[int, Field(ge=1, le=12)] = 4
    zerophase: bool = False


class NotchStage(_Base):
    type: Literal["notch"]
    freq: Annotated[float, Field(gt=0.0)]
    quality: Annotated[float, Field(gt=0.0)] = 30.0


class DecimationStage(_Base):
    type: Literal["decimation"]
    factor: Annotated[int, Field(ge=2, le=16)]
    no_filter: bool = False


class StaLtaStage(_Base):
    type: Literal["sta_lta"]
    sta: Annotated[float, Field(gt=0.0)]
    lta: Annotated[float, Field(gt=0.0)]
    on_threshold: Annotated[float, Field(gt=0.0)]
    off_threshold: Annotated[float, Field(gt=0.0)]

    @model_validator(mode="after")
    def _windows_ordered(self) -> StaLtaStage:
        if self.sta >= self.lta:
            raise ValueError("sta_lta.sta must be < lta")
        if self.off_threshold > self.on_threshold:
            raise ValueError("sta_lta.off_threshold must be <= on_threshold")
        return self


DspStageConfig = Annotated[
    DetrendStage
    | TaperStage
    | BandpassStage
    | HighpassStage
    | LowpassStage
    | NotchStage
    | DecimationStage
    | StaLtaStage,
    Field(discriminator="type"),
]


class ResponseMetadataConfig(_Base):
    """Per-device instrument-response metadata (M11).

    Optional. When ``path`` is set, the dashboard can deconvolve the
    instrument response to display FIXED windows (detection detail,
    archive replay, explicit selections) in physical units — velocity
    (m/s), acceleration (m/s²) or displacement (m). Counts remain the
    source of truth (CLAUDE.md rule 8); physical units are an on-demand
    display transform, NEVER persisted in place of counts and NEVER
    applied to the live scrolling plots.

    ``path`` resolves like other config paths (absolute, or relative to
    the config directory). ``format`` selects the reader: ``auto`` lets
    :func:`obspy.read_inventory` sniff the format (StationXML / dataless
    SEED / RESP), which is the right default; the explicit values pin it
    when sniffing is ambiguous. Devices without a ``path`` simply cannot
    show physical units — the UI degrades gracefully (options disabled).

    The deconvolution trusts the response's native output unit from the
    metadata; it does NOT infer instrument type from the channel code, so
    a velocimeter wired to an accelerometer-style channel code is handled
    by correct metadata, not code heuristics.

    ``pre_filt`` is an OPTIONAL deconvolution stabilisation band override.
    When ``None`` (the default) the four cosine-taper corners are derived
    automatically from the instrument's own corner frequency (the lowest
    PAZ pole, ``|pole|/2π``) so the deconvolution stays within the sensor's
    usable band and does NOT amplify sub-corner noise — the right default
    for a geophone (e.g. a 4.5 Hz sensor gets low corners near 4.5 Hz, not
    0.01 Hz). Set it to ``[low_stop, low_pass, high_pass, high_stop]`` (Hz,
    strictly increasing) only to deliberately push BELOW the corner
    frequency, accepting the amplified low-frequency noise that entails.
    """

    path: Path | None = None
    format: Literal["auto", "stationxml", "dataless", "resp"] = "auto"
    pre_filt: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def _pre_filt_ordered(self) -> ResponseMetadataConfig:
        if self.pre_filt is not None:
            f1, f2, f3, f4 = self.pre_filt
            if not (0.0 < f1 < f2 < f3 < f4):
                raise ValueError(
                    "response_metadata.pre_filt must be 4 strictly increasing "
                    "positive corners [low_stop, low_pass, high_pass, high_stop]"
                )
        return self


class PositionOverride(_Base):
    """Manual device position (rule 16): wins over StationXML when set.

    Used by the Map tab and multi-device HVSR via the shared
    ``DevicePosition`` resolver (M4 ``core/positions.py``). Set it for
    deployments where the device's StationXML coordinates are absent or
    wrong (e.g. GNSS-less indoor installs).
    """

    lat: Annotated[float, Field(ge=-90.0, le=90.0)]
    lon: Annotated[float, Field(ge=-180.0, le=180.0)]
    elev_m: Annotated[float, Field(ge=-500.0, le=9000.0)] = 0.0


class EchosDeviceConfig(_Base):
    """Client-side settings for an Echos ``firmware_seedlink`` node (M1).

    Deliberately minimal (rule 15): the device's REST API is the single
    truth for server-side settings (OSR, gains, SeedLink port/ring/auth,
    StationXML profile, network) — the YAML carries only what the app
    needs to *reach* the device. The admin password is NEVER stored here:
    it lives in the OS keyring (file fallback) via
    :class:`~echosmonitor.config.credentials.CredentialsStore`, keyed by
    the device's config ``name`` (the rule-15 "credentials reference").

    A device without an ``echos`` section is a generic SeedLink server
    (e.g. the public test servers): no REST features, no status poller.
    """

    # The firmware's HTTP REST server (plain HTTP on the LAN).
    http_port: Annotated[int, Field(ge=1, le=65535)] = 80
    # Manual position override (rule 16); null → StationXML coordinates.
    position_override: PositionOverride | None = None
    # Cadence of the M1-C status poller (status / clients / ring usage).
    # Lower bound 1 s keeps the poller polite to the ESP32's HTTP server;
    # upper bound 1 h rules out "never" misconfigurations.
    poll_interval_s: Annotated[float, Field(ge=1.0, le=3600.0)] = 5.0


class DeviceConfig(_Base):
    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: Annotated[int, Field(ge=1, le=65535)] = 18000
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)
    selectors: list[StreamSelectorConfig] = Field(default_factory=list)
    dsp_chain: list[DspStageConfig] = Field(default_factory=list)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    # M11: optional instrument-response metadata for physical-unit display
    # on fixed windows. Blank = this device stays counts-only.
    response_metadata: ResponseMetadataConfig = Field(default_factory=ResponseMetadataConfig)
    # M1: present ⇔ this device is an Echos firmware_seedlink node managed
    # via its REST API. Null = generic SeedLink server (rule 15 keeps
    # server-side settings on the device, not here).
    echos: EchosDeviceConfig | None = None


class HvsrConfig(_Base):
    """HVSR (Nakamura H/V spectral ratio) analysis defaults.

    The config mirror of :class:`~echosmonitor.core.hvsr.HvsrSettings`
    (same field names + bounds). The runtime settings object lives in
    ``core/hvsr.py`` (it is serialised into the report and that module must
    not import the whole config tree); :meth:`to_settings` bridges the two.
    These are starting values the UI seeds its controls from; the user can
    change window length / smoothing / band / rejection per measurement.
    """

    enabled: bool = False
    window_length_s: Annotated[float, Field(gt=0.0)] = 60.0
    konno_ohmachi_b: Annotated[float, Field(gt=0.0)] = 40.0
    freqmin_hz: Annotated[float, Field(gt=0.0)] = 0.2
    freqmax_hz: Annotated[float, Field(gt=0.0)] = 20.0
    horizontal_method: Literal[
        "geometric_mean",
        "squared_average",
        "total_horizontal_energy",
        "maximum_horizontal_value",
    ] = "geometric_mean"
    rejection_method: Literal["frequency_domain", "none"] = "frequency_domain"
    rejection_n: Annotated[float, Field(ge=1.0, le=5.0)] = 2.0
    detrend: Literal["linear", "constant"] = "linear"
    resample_n: Annotated[int, Field(ge=64, le=4096)] = 512
    psd_smoothing: bool = True
    psd_konno_ohmachi_b: Annotated[float, Field(gt=0.0)] = 40.0

    @model_validator(mode="after")
    def _band_ordered(self) -> HvsrConfig:
        if self.freqmin_hz >= self.freqmax_hz:
            raise ValueError("hvsr.freqmin_hz must be < freqmax_hz")
        return self

    def to_settings(self) -> HvsrSettings:
        """Build the runtime ``HvsrSettings`` (lazy import avoids a cycle)."""
        from echosmonitor.core.hvsr import HvsrSettings

        return HvsrSettings(
            window_length_s=self.window_length_s,
            konno_ohmachi_b=self.konno_ohmachi_b,
            freqmin_hz=self.freqmin_hz,
            freqmax_hz=self.freqmax_hz,
            horizontal_method=self.horizontal_method,
            rejection_method=self.rejection_method,
            rejection_n=self.rejection_n,
            detrend=self.detrend,
            resample_n=self.resample_n,
            psd_smoothing=self.psd_smoothing,
            psd_konno_ohmachi_b=self.psd_konno_ohmachi_b,
        )


class RootConfig(_Base):
    app: AppConfig = Field(default_factory=AppConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    devices: list[DeviceConfig] = Field(default_factory=list)
    hvsr: HvsrConfig = Field(default_factory=HvsrConfig)

    @model_validator(mode="after")
    def _devices_map_to_distinct_archive_dirs(self) -> RootConfig:
        """Reject configs where two devices share one per-device SDS subtree.

        The archive is namespaced per device at
        ``archive_root/<sanitize_device_name(name)>/...``. Because
        :func:`~echosmonitor.storage.sds.sanitize_device_name` is not
        injective, two *distinct* device names can collapse to the same
        sanitized segment (``"Echos"`` / ``"Echos_"``, ``"a/b"`` / ``"a b"``)
        and would then write into ONE physical SDS tree — exactly the
        cross-device file collision the per-device layout prevents. Unlike a
        bare NSLC clash (which the per-device paths make safe, so it only
        warns), a sanitized-segment clash defeats the separation, so fail fast
        at load with an actionable message. The import is deferred to keep the
        ``storage`` dependency (and obspy) out of config-module import time.
        """
        from echosmonitor.storage.sds import sanitize_device_name

        seen: dict[str, str] = {}
        for dev in self.devices:
            segment = sanitize_device_name(dev.name)
            prior = seen.get(segment)
            if prior is not None and prior != dev.name:
                raise ValueError(
                    f"devices {prior!r} and {dev.name!r} map to the same archive "
                    f"directory {segment!r}; rename one so their per-device SDS "
                    f"trees stay separate"
                )
            seen[segment] = dev.name
        return self
