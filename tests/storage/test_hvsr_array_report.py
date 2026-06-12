"""Array HVSR report (M5-C) — PDF structure + JSON round-trip + error paths.

Structural presence (rule 10), not pixels: the PDF is a valid document with
one comparison page plus two pages per valid station; the JSON embeds each
station's full single-station structure (a superset of the single export),
the geometry with distances, the unpositioned diff and the per-device
errors; nothing valid to export raises and writes no file.
"""

from __future__ import annotations

import json

import numpy as np
from obspy.core.utcdatetime import UTCDateTime
from scipy import signal

from echosmonitor.core.hvsr import HvsrAccumulator, HvsrResult, HvsrSettings
from echosmonitor.core.hvsr_array import ArrayHvsrResult
from echosmonitor.core.positions import ResolvedPosition, station_geometry
from echosmonitor.storage.hvsr_report import (
    ArrayReportContext,
    HvsrExportError,
    array_comparison_lines,
    array_result_to_dict,
    export_hvsr_array_json,
    write_hvsr_array_pdf,
)

_FS = 100.0
_WL = 20.0
_F0 = 5.0
_NPER = int(_WL * _FS)


def _group(sta: str) -> dict[str, str]:
    return {c: f"XX.{sta}.00.HH{c}" for c in ("Z", "N", "E")}


_CTX = ArrayReportContext(
    group_by_device={"alpha": _group("STA"), "beta": _group("STB")},
    period_label="2026-01-01T00:00:00 to 2026-01-01T00:10:00",
    generated_at="2026-06-12T12:00:00",
)
_POSITIONS = {
    "alpha": ResolvedPosition("alpha", 45.0, 11.0, 100.0, "stationxml", 0.0),
    "beta": ResolvedPosition("beta", 45.001, 11.0, 101.0, "gnss", 0.0),
}


def _resonant(n: int, rng: np.random.Generator) -> np.ndarray:
    b, a = signal.iirpeak(_F0 / (_FS / 2.0), 25.0)
    w = rng.standard_normal(n)
    return w * 0.3 + signal.lfilter(b, a, w) * 4.0


def _station_result(device: str, seed: int) -> HvsrResult:
    rng = np.random.default_rng(seed)
    settings = HvsrSettings(window_length_s=_WL, freqmin_hz=0.5, freqmax_hz=40.0, resample_n=64)
    acc = HvsrAccumulator(
        settings,
        same_response=True,
        same_response_detail="Same-response assumed (single 3C station).",
        device=device,
        station_key=f"XX.{device}",
        provenance="live",
    )
    t0 = UTCDateTime("2026-01-01")
    for i in range(12):
        acc.add_window(
            rng.standard_normal(_NPER),
            _resonant(_NPER, rng),
            _resonant(_NPER, rng),
            t0 + i * _WL,
            _FS,
        )
    return acc.compute()


def _array_result(
    *,
    devices: tuple[str, ...] = ("alpha", "beta"),
    with_beta_result: bool = False,
    beta_error: str = "boom",
    positions: dict[str, ResolvedPosition] | None = None,
) -> ArrayHvsrResult:
    results = {"alpha": _station_result("alpha", 2)}
    errors: dict[str, str] = {}
    if with_beta_result:
        results["beta"] = _station_result("beta", 3)
    elif "beta" in devices:
        errors["beta"] = beta_error
    pos = positions if positions is not None else _POSITIONS
    return ArrayHvsrResult(
        measurement_id="hvsr-array-1",
        devices=devices,
        results=results,
        errors=errors,
        geometry=station_geometry(pos, devices),
        settings=HvsrSettings(window_length_s=_WL, freqmin_hz=0.5, freqmax_hz=40.0),
        provenance="live",
        elapsed_ms=10.0,
    )


# ----------------------------------------------------------------------
def test_pdf_has_comparison_page_plus_two_per_valid_station(tmp_path) -> None:
    res = _array_result(with_beta_result=True)
    path = tmp_path / "array.pdf"
    write_hvsr_array_pdf(res, path, _CTX)
    raw = path.read_bytes()
    assert raw.startswith(b"%PDF")
    assert b"%%EOF" in raw
    # 1 comparison + 2 pages x 2 valid stations.
    assert raw.count(b"/Type /Page") >= 5


def test_pdf_skips_stations_without_valid_result(tmp_path) -> None:
    res = _array_result(with_beta_result=False)  # beta failed
    path = tmp_path / "array.pdf"
    write_hvsr_array_pdf(res, path, _CTX)
    raw = path.read_bytes()
    pages = raw.count(b"/Type /Page")
    # 1 comparison + 2 for alpha only.
    assert 3 <= pages < 5


def test_json_embeds_stations_geometry_errors_and_unpositioned(tmp_path) -> None:
    positions = {"alpha": _POSITIONS["alpha"]}  # beta unpositioned
    res = _array_result(with_beta_result=False, positions=positions)
    path = tmp_path / "array.json"
    export_hvsr_array_json(res, path, _CTX)
    payload = json.loads(path.read_text())
    assert payload["schema"] == "echosmonitor.hvsr-array/1"
    assert payload["devices"] == ["alpha", "beta"]
    assert payload["errors"] == {"beta": "boom"}
    # Each present station embeds the FULL single-station structure.
    station = payload["stations"]["alpha"]
    assert station["schema"] == "echosmonitor.hvsr/1"
    assert station["device"] == "alpha"
    assert station["nslc_by_component"]["Z"] == "XX.STA.00.HHZ"
    assert len(station["mean_curve"]) == len(station["frequency_hz"])
    assert "beta" not in payload["stations"]
    # Geometry: positions with source, no distances (single station), the
    # unpositioned diff said explicitly (rule 16).
    geo = payload["geometry"]
    assert geo["positions"]["alpha"]["source"] == "stationxml"
    assert geo["distances_m"] == {}
    assert geo["unpositioned"] == ["beta"]
    assert "response-sensitive" in payload["a0_note"]


def test_json_distances_between_positioned_stations(tmp_path) -> None:
    res = _array_result(with_beta_result=True)
    path = tmp_path / "array.json"
    export_hvsr_array_json(res, path, _CTX)
    payload = json.loads(path.read_text())
    distances = payload["geometry"]["distances_m"]
    assert list(distances) == ["alpha|beta"]
    # ~111 m for 0.001 deg of latitude.
    assert 100.0 < distances["alpha|beta"] < 125.0


def test_comparison_lines_cover_results_failures_and_geometry() -> None:
    res = _array_result(with_beta_result=False)
    text = "\n".join(array_comparison_lines(res, _CTX))
    assert "alpha" in text and "f0 " in text
    assert "compute failed — boom" in text
    assert "response-sensitive" in text  # the A0 honesty note
    assert "lat 45.000000" in text and "source: stationxml" in text
    assert "alpha - beta:" in text  # the distances block


def test_comparison_never_prints_nan_for_zero_valid_station(tmp_path) -> None:
    """A present result whose windows were ALL rejected (f0 = honest NaN)
    must read "no valid windows", never "f0 nan", and must not fake a
    legend entry on the overlay or inflate the valid-station count."""
    base = _array_result(with_beta_result=True)
    rejected_beta = _rejected_copy(base.results["beta"])
    res = ArrayHvsrResult(
        measurement_id=base.measurement_id,
        devices=base.devices,
        results={"alpha": base.results["alpha"], "beta": rejected_beta},
        errors={},
        geometry=base.geometry,
        settings=base.settings,
        provenance="live",
        elapsed_ms=1.0,
    )
    text = "\n".join(array_comparison_lines(res, _CTX))
    assert "nan" not in text.lower()
    assert "no valid windows" in text
    # The PDF still renders (alpha is valid) — structurally 1 + 2 pages.
    path = tmp_path / "array.pdf"
    write_hvsr_array_pdf(res, path, _CTX)
    pages = path.read_bytes().count(b"/Type /Page")
    assert 3 <= pages < 5


def test_export_nothing_valid_raises_and_writes_nothing(tmp_path) -> None:
    path = tmp_path / "nope.json"
    for bad in (None,):
        try:
            export_hvsr_array_json(bad, path, _CTX)
        except HvsrExportError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected HvsrExportError")
    # An array whose only station has zero valid windows is not exportable.
    res = _array_result(with_beta_result=False)
    all_rejected = ArrayHvsrResult(
        measurement_id=res.measurement_id,
        devices=("alpha",),
        results={
            "alpha": _rejected_copy(res.results["alpha"]),
        },
        errors={},
        geometry=res.geometry,
        settings=res.settings,
        provenance="live",
        elapsed_ms=1.0,
    )
    try:
        write_hvsr_array_pdf(all_rejected, tmp_path / "nope.pdf", _CTX)
    except HvsrExportError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected HvsrExportError")
    assert not path.exists()
    assert not (tmp_path / "nope.pdf").exists()


def _rejected_copy(r: HvsrResult) -> HvsrResult:
    from dataclasses import replace

    return replace(r, n_windows_valid=0)


def test_array_dict_settings_match_measurement_settings() -> None:
    res = _array_result(with_beta_result=True)
    payload = array_result_to_dict(res, _CTX)
    assert payload["settings"]["window_length_s"] == _WL
    assert payload["provenance"] == "live"
