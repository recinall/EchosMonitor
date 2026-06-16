"""Tests for the pure instrument-response deconvolution core (M11 Stage A).

All tests run offline against bundled ObsPy example metadata — no network —
so they live in the default gate.

The scientific anchor is the forward/inverse round trip
(:func:`test_round_trip_recovers_velocity`): a known band-limited velocity
signal is forward-convolved *through* the IU.ANMO response to synthesise
COUNTS, then :meth:`ResponseRemover.to_physical` removes the response and
must recover the original velocity.
"""

from __future__ import annotations

import numpy as np
import obspy
import pytest
from obspy.core.util import get_example_file
from scipy.signal.windows import tukey

from echosmonitor.config.schema import DeviceConfig, ResponseMetadataConfig
from echosmonitor.core.exceptions import ResponseError
from echosmonitor.core.response import (
    ResponseProvider,
    ResponseRemover,
    default_pre_filt,
    inventory_from_stationxml_blob,
    load_inventory,
)

# IU.ANMO.00.BHZ: native units M/S -> COUNTS, fs = 20 Hz.
_NET, _STA, _LOC, _CHA = "IU", "ANMO", "00", "BHZ"
_NSLC = f"{_NET}.{_STA}.{_LOC}.{_CHA}"

# A 1 Hz burst over a 120 s window at 20 Hz: well inside the pre_filt flat
# band (0.01 Hz .. 0.45*20 = 9 Hz), so the deconvolution is in its stable
# region and the round trip is dominated by the (tiny) FFT/water-level
# numerics, not by pre_filt taper attenuation.
_SIGNAL_HZ = 1.0
_WINDOW_S = 120.0
_TUKEY_ALPHA = 0.2

# Edge fraction excluded from comparisons. The forward FFT convolution and
# the inverse remove_response each apply a cosine taper plus filter
# transients at the window edges; the central 50-60% is the clean,
# transient-free region we assert against.
_EDGE_FRAC = 0.2
_DERIV_EDGE_FRAC = 0.25

# Round-trip tolerances. The recovered velocity is near-perfect in the
# passband, so we demand a very high correlation and a peak-amplitude
# ratio within 10% (the spec's bound; in practice it lands within ~0.01%).
_MIN_CORRCOEF = 0.99
_AMP_RATIO_TOL = 0.10

# Derivative-relationship tolerances. ACC ~= d/dt VEL and VEL ~= d/dt DISP.
# Correlation is ~1.0; the amplitude ratio carries the ~1.7% error of the
# second-order ``np.gradient`` finite difference at 20 Hz / 1 Hz, so 5%.
_DERIV_MIN_CORRCOEF = 0.99
_DERIV_AMP_RATIO_TOL = 0.05


@pytest.fixture
def anmo_inventory() -> obspy.core.inventory.Inventory:
    """The bundled IU.ANMO.00.BHZ StationXML inventory."""
    return obspy.read_inventory(get_example_file("IU_ANMO_00_BHZ.xml"))


def _anmo_channel_stats(
    inv: obspy.core.inventory.Inventory,
) -> tuple[float, obspy.UTCDateTime]:
    """Return ``(sampling_rate, start_date)`` for the ANMO channel epoch."""
    cha = inv[0][0][0]
    return float(cha.sample_rate), cha.start_date


def _make_velocity(npts: int, fs: float) -> np.ndarray:
    """A tapered 1 Hz sine burst standing in for ground velocity (m/s)."""
    t = np.arange(npts) / fs
    burst = np.sin(2.0 * np.pi * _SIGNAL_HZ * t)
    return (burst * tukey(npts, _TUKEY_ALPHA)).astype(np.float64)


def _forward_to_counts(
    velocity: np.ndarray,
    resp: obspy.core.inventory.response.Response,
    fs: float,
) -> np.ndarray:
    """Forward-convolve a velocity signal through the response into COUNTS.

    Multiplies the rFFT of the velocity by the response's complex spectrum
    (evaluated for native output ``VEL``, i.e. the M/S -> COUNTS transfer
    function) and inverts. This is the exact inverse of what
    :meth:`Trace.remove_response` does, so removing the response should
    recover ``velocity``.
    """
    npts = velocity.size
    respc, _freqs = resp.get_evalresp_response(t_samp=1.0 / fs, nfft=npts, output="VEL")
    spectrum = np.fft.rfft(velocity)
    return np.fft.irfft(spectrum * respc, n=npts)


def _counts_stream(counts: np.ndarray, fs: float, starttime: obspy.UTCDateTime) -> obspy.Stream:
    """Wrap a COUNTS array in a Stream tagged as IU.ANMO.00.BHZ."""
    trace = obspy.Trace(data=counts.copy())
    trace.stats.network = _NET
    trace.stats.station = _STA
    trace.stats.location = _LOC
    trace.stats.channel = _CHA
    trace.stats.sampling_rate = fs
    trace.stats.starttime = starttime
    return obspy.Stream([trace])


def _central(arr: np.ndarray, edge_frac: float) -> slice:
    """Slice selecting the central, transient-free portion of ``arr``."""
    n = arr.size
    return slice(int(n * edge_frac), int(n * (1.0 - edge_frac)))


# ----------------------------------------------------------------------
# 1. Scientific round-trip correctness (the key test).
# ----------------------------------------------------------------------


def test_round_trip_recovers_velocity(anmo_inventory: obspy.core.inventory.Inventory) -> None:
    """Forward-convolve a known velocity to counts, then recover it.

    Comparing the central 60% (``_EDGE_FRAC`` trimmed each side) avoids
    taper and filter transients at the window edges. The recovered signal
    must correlate > 0.99 with the input and match its peak amplitude
    within 10%.
    """
    fs, start = _anmo_channel_stats(anmo_inventory)
    resp = anmo_inventory.get_response(_NSLC, start)
    npts = int(fs * _WINDOW_S)

    velocity = _make_velocity(npts, fs)
    counts = _forward_to_counts(velocity, resp, fs)
    stream = _counts_stream(counts, fs, start)

    recovered = ResponseRemover(anmo_inventory).to_physical(stream, "VEL")[0].data

    sl = _central(velocity, _EDGE_FRAC)
    cc = float(np.corrcoef(velocity[sl], recovered[sl])[0, 1])
    amp_ratio = float(np.max(np.abs(recovered[sl])) / np.max(np.abs(velocity[sl])))

    assert cc > _MIN_CORRCOEF, f"corrcoef {cc} below {_MIN_CORRCOEF}"
    assert abs(amp_ratio - 1.0) < _AMP_RATIO_TOL, f"amplitude ratio {amp_ratio}"


# ----------------------------------------------------------------------
# 2. VEL vs ACC vs DISP relationship.
# ----------------------------------------------------------------------


def test_vel_acc_disp_derivative_relationship(
    anmo_inventory: obspy.core.inventory.Inventory,
) -> None:
    """ACC ~= d/dt VEL and VEL ~= d/dt DISP in the passband.

    All three outputs come from the *same* synthesised counts. The
    amplitude tolerance (5%) absorbs the second-order finite-difference
    error of ``np.gradient`` at 20 Hz / 1 Hz; correlation stays ~1.0.
    """
    fs, start = _anmo_channel_stats(anmo_inventory)
    resp = anmo_inventory.get_response(_NSLC, start)
    npts = int(fs * _WINDOW_S)
    counts = _forward_to_counts(_make_velocity(npts, fs), resp, fs)

    remover = ResponseRemover(anmo_inventory)
    vel = remover.to_physical(_counts_stream(counts, fs, start), "VEL")[0].data
    acc = remover.to_physical(_counts_stream(counts, fs, start), "ACC")[0].data
    disp = remover.to_physical(_counts_stream(counts, fs, start), "DISP")[0].data

    sl = _central(vel, _DERIV_EDGE_FRAC)
    dt = 1.0 / fs

    acc_from_vel = np.gradient(vel, dt)
    cc_acc = float(np.corrcoef(acc_from_vel[sl], acc[sl])[0, 1])
    r_acc = float(np.max(np.abs(acc[sl])) / np.max(np.abs(acc_from_vel[sl])))

    vel_from_disp = np.gradient(disp, dt)
    cc_vel = float(np.corrcoef(vel_from_disp[sl], vel[sl])[0, 1])
    r_vel = float(np.max(np.abs(vel[sl])) / np.max(np.abs(vel_from_disp[sl])))

    assert cc_acc > _DERIV_MIN_CORRCOEF
    assert abs(r_acc - 1.0) < _DERIV_AMP_RATIO_TOL
    assert cc_vel > _DERIV_MIN_CORRCOEF
    assert abs(r_vel - 1.0) < _DERIV_AMP_RATIO_TOL


# ----------------------------------------------------------------------
# 3. No response for the NSLC -> ResponseError (no silent pass-through).
# ----------------------------------------------------------------------


def test_no_response_for_nslc_raises(
    anmo_inventory: obspy.core.inventory.Inventory,
) -> None:
    """A trace whose id is absent from the inventory must raise, naming it."""
    fs, start = _anmo_channel_stats(anmo_inventory)
    npts = int(fs * 10)
    trace = obspy.Trace(data=np.ones(npts, dtype=np.float64))
    trace.stats.network = "XX"
    trace.stats.station = "NONE"
    trace.stats.location = ""
    trace.stats.channel = "BHZ"
    trace.stats.sampling_rate = fs
    trace.stats.starttime = start
    stream = obspy.Stream([trace])

    with pytest.raises(ResponseError) as excinfo:
        ResponseRemover(anmo_inventory).to_physical(stream, "VEL")
    assert "XX.NONE..BHZ" in str(excinfo.value)


# ----------------------------------------------------------------------
# 4. All three metadata formats load.
# ----------------------------------------------------------------------


def test_load_inventory_all_formats() -> None:
    """StationXML, dataless SEED, and RESP all load with format='auto'."""
    examples = {
        "stationxml": "IU_ANMO_00_BHZ.xml",
        "dataless": "dataless.seed.BW_RJOB",
        "resp": "RESP.BW.FURT..EHZ",
    }
    from pathlib import Path

    for name in examples.values():
        inv = load_inventory(Path(get_example_file(name)))
        channels = inv.get_contents()["channels"]
        assert len(channels) >= 1, f"{name} yielded no channels"

    # Explicit format also works for StationXML.
    inv_explicit = load_inventory(Path(get_example_file("IU_ANMO_00_BHZ.xml")), fmt="stationxml")
    assert len(inv_explicit.get_contents()["channels"]) >= 1


# ----------------------------------------------------------------------
# 5. Source Stream is not mutated.
# ----------------------------------------------------------------------


def test_source_not_mutated(anmo_inventory: obspy.core.inventory.Inventory) -> None:
    """``to_physical`` must leave the source counts array untouched."""
    fs, start = _anmo_channel_stats(anmo_inventory)
    resp = anmo_inventory.get_response(_NSLC, start)
    npts = int(fs * _WINDOW_S)
    counts = _forward_to_counts(_make_velocity(npts, fs), resp, fs)
    stream = _counts_stream(counts, fs, start)

    before = stream[0].data.copy()
    before_dtype = stream[0].data.dtype

    ResponseRemover(anmo_inventory).to_physical(stream, "VEL")

    np.testing.assert_array_equal(stream[0].data, before)
    assert stream[0].data.dtype == before_dtype


# ----------------------------------------------------------------------
# 6. Gappy (masked) window is rejected.
# ----------------------------------------------------------------------


def test_gappy_window_rejected(
    anmo_inventory: obspy.core.inventory.Inventory,
) -> None:
    """A masked-array trace (a straddled gap) must raise, mentioning gaps."""
    fs, start = _anmo_channel_stats(anmo_inventory)
    npts = int(fs * 10)
    data = np.ma.array(np.ones(npts, dtype=np.float64))
    data[npts // 2 : npts // 2 + 5] = np.ma.masked

    trace = obspy.Trace(data=data)
    trace.stats.network = _NET
    trace.stats.station = _STA
    trace.stats.location = _LOC
    trace.stats.channel = _CHA
    trace.stats.sampling_rate = fs
    trace.stats.starttime = start
    stream = obspy.Stream([trace])

    with pytest.raises(ResponseError) as excinfo:
        ResponseRemover(anmo_inventory).to_physical(stream, "VEL")
    assert "gap" in str(excinfo.value).lower()


# ----------------------------------------------------------------------
# 7. Caching: same path + unchanged mtime returns the identical object.
# ----------------------------------------------------------------------


def test_load_inventory_caches_by_mtime() -> None:
    """A repeated load of an unchanged file returns the cached instance."""
    from pathlib import Path

    path = Path(get_example_file("IU_ANMO_00_BHZ.xml"))
    first = load_inventory(path)
    second = load_inventory(path)
    assert first is second


# ----------------------------------------------------------------------
# default_pre_filt sanity.
# ----------------------------------------------------------------------


def test_default_pre_filt_corners_ordered() -> None:
    """The four corners are strictly increasing and fs-scaled at the top."""
    fs = 100.0
    low_stop, low_pass, high_pass, high_stop = default_pre_filt(fs)
    assert low_stop < low_pass < high_pass < high_stop
    assert high_pass == pytest.approx(0.45 * fs)
    assert high_stop == pytest.approx(0.5 * fs)


# ----------------------------------------------------------------------
# ResponseProvider — config device -> ResponseRemover resolution.
# ----------------------------------------------------------------------


def _device(name: str, *, path: object = None, fmt: str = "auto") -> DeviceConfig:
    meta = ResponseMetadataConfig(path=path, format=fmt)  # type: ignore[arg-type]
    return DeviceConfig(name=name, host="h", response_metadata=meta)


def test_provider_unconfigured_device_returns_none() -> None:
    """A device with no metadata path yields no remover and isn't configured."""
    from pathlib import Path

    prov = ResponseProvider([_device("dev")], Path("/tmp"))
    assert prov.is_configured("dev") is False
    assert prov.remover_for("dev") is None
    assert prov.remover_for("unknown-device") is None


def test_provider_absolute_path_loads(anmo_inventory: obspy.core.inventory.Inventory) -> None:
    """An absolute metadata path resolves and builds a working remover."""
    from pathlib import Path

    xml = Path(get_example_file("IU_ANMO_00_BHZ.xml"))
    prov = ResponseProvider([_device("anmo", path=xml)], None)
    assert prov.is_configured("anmo") is True
    remover = prov.remover_for("anmo")
    assert isinstance(remover, ResponseRemover)
    _fs, start = _anmo_channel_stats(anmo_inventory)
    assert prov.available_for("anmo", _NSLC, start) is True
    assert prov.available_for("anmo", "XX.NONE..BHZ", start) is False


def test_provider_relative_path_resolves_against_config_dir() -> None:
    """A relative metadata path resolves against the config directory."""
    import shutil
    import tempfile
    from pathlib import Path

    src = Path(get_example_file("IU_ANMO_00_BHZ.xml"))
    with tempfile.TemporaryDirectory() as tmp:
        cfg_dir = Path(tmp)
        shutil.copy(src, cfg_dir / "resp.xml")
        prov = ResponseProvider([_device("anmo", path=Path("resp.xml"))], cfg_dir)
        remover = prov.remover_for("anmo")
        assert isinstance(remover, ResponseRemover)


def test_provider_bad_file_surfaces_or_unavailable() -> None:
    """A configured-but-unreadable file: remover_for raises, available_for False."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "not_inventory.xml"
        bad.write_text("this is not a valid inventory file", encoding="utf-8")
        prov = ResponseProvider([_device("dev", path=bad)], None)
        assert prov.is_configured("dev") is True
        with pytest.raises(ResponseError):
            prov.remover_for("dev")
        # available_for swallows the load error into a graceful False.
        assert prov.available_for("dev", _NSLC, obspy.UTCDateTime()) is False


# M6.6-B: StationXML blob parsing + the ResponseProvider blob fallback.


def _anmo_blob() -> str:
    from pathlib import Path

    return Path(get_example_file("IU_ANMO_00_BHZ.xml")).read_text(encoding="utf-8")


def test_inventory_from_stationxml_blob_parses_and_caches() -> None:
    blob = _anmo_blob()
    inv1 = inventory_from_stationxml_blob(blob)
    inv2 = inventory_from_stationxml_blob(blob)
    # Same bytes → cached identity (no re-parse).
    assert inv1 is inv2
    assert inv1.get_response(_NSLC, obspy.UTCDateTime("2015-01-01")) is not None


def test_inventory_from_stationxml_blob_bad_raises() -> None:
    with pytest.raises(ResponseError):
        inventory_from_stationxml_blob("<not-stationxml/>")


def test_provider_blob_fallback_resolves_response(
    anmo_inventory: obspy.core.inventory.Inventory,
) -> None:
    """M6.6-B: with no config-file path, a registered StationXML blob makes
    the device 'configured' and resolves a working remover (archive path)."""
    from pathlib import Path

    prov = ResponseProvider([_device("anmo")], Path("/tmp"))
    assert prov.is_configured("anmo") is False
    prov.set_stationxml_blob("anmo", _anmo_blob())
    assert prov.is_configured("anmo") is True
    remover = prov.remover_for("anmo")
    assert isinstance(remover, ResponseRemover)
    _fs, start = _anmo_channel_stats(anmo_inventory)
    assert prov.available_for("anmo", _NSLC, start) is True
    # Clearing the blob reverts to no-response.
    prov.set_stationxml_blob("anmo", None)
    assert prov.is_configured("anmo") is False
    assert prov.remover_for("anmo") is None


def test_provider_config_file_override_wins_over_blob(
    anmo_inventory: obspy.core.inventory.Inventory,
) -> None:
    """Rule 16: an explicit config-file response_metadata override beats a
    fetched StationXML blob. We prove the file path is taken even when a
    (garbage) blob is also registered — the file resolves, the blob is
    ignored."""
    from pathlib import Path

    xml = Path(get_example_file("IU_ANMO_00_BHZ.xml"))
    prov = ResponseProvider([_device("anmo", path=xml)], None)
    prov.set_stationxml_blob("anmo", "<garbage/>")
    # remover_for must resolve from the FILE (not raise on the garbage blob).
    remover = prov.remover_for("anmo")
    assert isinstance(remover, ResponseRemover)
    _fs, start = _anmo_channel_stats(anmo_inventory)
    assert prov.available_for("anmo", _NSLC, start) is True
