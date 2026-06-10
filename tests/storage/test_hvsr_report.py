"""HVSR report (Stage D) — PDF validity + JSON/CSV round-trip + error paths.

Asserts structural presence (rule 10), not pixels: the PDF is a valid
non-empty multi-page document; the JSON/CSV reproduce the H/V curve, f0 and
settings; and a no-valid-windows / not-run export raises a clear error and
writes no malformed file.
"""

from __future__ import annotations

import json

import numpy as np
from obspy.core.utcdatetime import UTCDateTime
from scipy import signal

from echosmonitor.core.hvsr import HvsrAccumulator, HvsrResult, HvsrSettings
from echosmonitor.storage.hvsr_report import (
    HvsrExportError,
    ReportContext,
    export_hvsr_csv,
    export_hvsr_json,
    numeric_report_lines,
    report_title,
    write_hvsr_pdf,
)

_FS = 100.0
_WL = 20.0
_F0 = 5.0
_NPER = int(_WL * _FS)
_GROUP = {"Z": "XX.STA.00.HHZ", "N": "XX.STA.00.HHN", "E": "XX.STA.00.HHE"}
_CTX = ReportContext(
    # The period label carries only the time span — the report appends the
    # provenance once (so the widget must NOT embed it here).
    nslc_by_component=_GROUP,
    period_label="2026-01-01T00:00:00 to 2026-01-01T00:10:00",
    generated_at="2026-06-02T12:00:00",
)


def _resonant(n: int, rng: np.random.Generator) -> np.ndarray:
    b, a = signal.iirpeak(_F0 / (_FS / 2.0), 25.0)
    w = rng.standard_normal(n)
    return w * 0.3 + signal.lfilter(b, a, w) * 4.0


def _make_result(*, exclude_all: bool = False) -> HvsrResult:
    rng = np.random.default_rng(2)
    settings = HvsrSettings(window_length_s=_WL, freqmin_hz=0.5, freqmax_hz=40.0, resample_n=64)
    acc = HvsrAccumulator(
        settings,
        same_response=True,
        same_response_detail="Same-response assumed (single 3C station).",
        device="dev",
        station_key="XX.STA",
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
    if exclude_all:
        for wid in acc.window_ids():
            acc.set_window_override(wid, False)
    return acc.compute()


# ----------------------------------------------------------------------
def test_pdf_is_valid_nonempty_and_multipage(tmp_path) -> None:
    res = _make_result()
    path = tmp_path / "report.pdf"
    write_hvsr_pdf(res, path, _CTX)
    raw = path.read_bytes()
    assert raw.startswith(b"%PDF")
    assert b"%%EOF" in raw
    assert len(raw) > 2000
    # Two pages (plots + numbers) — structural presence, not pixels.
    assert raw.count(b"/Type /Page") >= 2


def test_json_round_trips_curve_f0_and_settings(tmp_path) -> None:
    res = _make_result()
    path = tmp_path / "data.json"
    export_hvsr_json(res, path, _CTX)
    loaded = json.loads(path.read_text())

    # f0 + settings reproduce.
    assert abs(loaded["f0_hz"] - res.f0_hz) < 1e-6
    assert loaded["settings"] == res.settings.model_dump()
    assert loaded["provenance"] == "live"
    assert loaded["same_response"] is True
    # The H/V mean curve reproduces element-for-element.
    mean = np.array(loaded["mean_curve"], dtype=float)
    assert mean.shape == res.mean_curve.shape
    assert np.allclose(mean, res.mean_curve, equal_nan=True)
    assert len(loaded["frequency_hz"]) == res.frequency.shape[0]
    # SESAME criteria fully recorded.
    assert len(loaded["reliability"]) == 3
    assert len(loaded["clarity"]) == 6
    assert {c["name"] for c in loaded["reliability"]} == {c.name for c in res.reliability}
    # Per-window curves + masks present for reproduction.
    assert len(loaded["window_curves"]) == res.n_windows_total
    assert len(loaded["effective_mask"]) == res.n_windows_total


def test_csv_round_trips_curve(tmp_path) -> None:
    res = _make_result()
    path = tmp_path / "data.csv"
    export_hvsr_csv(res, path, _CTX)
    text = path.read_text()
    # Scalars + settings live in comment lines.
    assert "# f0_hz:" in text
    assert "# settings:" in text
    # Parse the table (skip comment lines) and reproduce the curve.
    rows = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    header = rows[0].split(",")
    assert header[:4] == ["frequency_hz", "mean_hv", "median_hv", "lognormal_sigma"]
    data = np.array([[float(x) for x in r.split(",")[:2]] for r in rows[1:]])
    assert data.shape[0] == res.frequency.shape[0]
    assert np.allclose(data[:, 0], res.frequency, rtol=1e-4)
    assert np.allclose(data[:, 1], res.mean_curve, rtol=1e-4, equal_nan=True)


def test_export_none_raises_and_writes_nothing(tmp_path) -> None:
    for fn in (export_hvsr_json, export_hvsr_csv, write_hvsr_pdf):
        path = tmp_path / f"{fn.__name__}.out"
        try:
            fn(None, path, _CTX)
            raise AssertionError(f"{fn.__name__} should have raised")
        except HvsrExportError:
            pass
        assert not path.exists()  # no malformed file left behind


def test_report_title_has_single_provenance_suffix() -> None:
    """FIX 4(a): the provenance suffix appears exactly once (no '(live) (live)')."""
    res = _make_result()
    title = report_title(res, _CTX)
    assert title.count("(live)") == 1
    # And the period label itself must not carry provenance (the report adds it).
    assert "(live)" not in _CTX.period_label


def test_numeric_lines_contain_full_counts_vs_physical_statement() -> None:
    """FIX 4(b): the long same-response line is wrapped, not truncated."""
    res = _make_result()
    lines = numeric_report_lines(res, _CTX)
    assert "COUNTS vs PHYSICAL UNITS" in lines
    joined = " ".join(s.strip() for s in lines)
    # The full statement is present (every word survives the wrap).
    for token in res.same_response_detail.split():
        assert token in joined
    # No single rendered line overflows the page width.
    assert all(len(s) <= 92 for s in lines)


def test_report_psd_uses_the_configured_smoothing() -> None:
    """FEATURE 5: the report PSD is the result's PSD (smoothed when enabled)."""
    smoothed = _make_result()  # default psd_smoothing=True
    f_sm, db_sm = smoothed.psd_z
    # The smoothed PSD lands on the resample_n centre frequencies, not the
    # raw Welch grid — i.e. the on-screen/report PSD reflects the setting.
    assert f_sm.shape[0] == smoothed.settings.resample_n
    ptp_sm = float(np.var(np.diff(db_sm)))
    assert ptp_sm < 0.2  # demonstrably smooth


def test_app_version_matches_package_metadata() -> None:
    """The report header version must not drift from the package version."""
    import importlib.metadata

    from echosmonitor.storage.hvsr_report import APP_VERSION

    assert importlib.metadata.version("echosmonitor") == APP_VERSION


def test_export_zero_valid_windows_raises(tmp_path) -> None:
    res = _make_result(exclude_all=True)
    assert res.n_windows_valid == 0
    path = tmp_path / "empty.json"
    try:
        export_hvsr_json(res, path, _CTX)
        raise AssertionError("should have raised on zero valid windows")
    except HvsrExportError:
        pass
    assert not path.exists()
