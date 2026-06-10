"""Tests for :class:`DeconvolutionWorker` (M11 B).

Exercises the real deconvolution path end-to-end with the bundled
IU.ANMO StationXML (rule 10 — observable result, not mechanism), plus
the no-response and gappy failure paths. Threading affinity is asserted
separately in the GUI wiring test.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from obspy import UTCDateTime
from obspy.core.util import get_example_file
from PySide6.QtCore import QObject, Qt, Slot

from echosmonitor.config.schema import DeviceConfig, ResponseMetadataConfig
from echosmonitor.core.deconvolution_worker import DeconvolutionWorker
from echosmonitor.core.response import ResponseProvider

_NSLC = "IU.ANMO.00.BHZ"
_FS = 20.0
# The bundled IU.ANMO response is valid around 2014; use a start time
# inside its epoch so the deconvolution finds a match.
_START = UTCDateTime("2014-01-01T00:00:00")


class _Spy(QObject):
    def __init__(self, worker: DeconvolutionWorker) -> None:
        super().__init__()
        self.ok: list[tuple[int, str, np.ndarray]] = []
        self.err: list[tuple[int, str]] = []
        worker.deconvolved.connect(self._on_ok, type=Qt.ConnectionType.DirectConnection)
        worker.failed.connect(self._on_err, type=Qt.ConnectionType.DirectConnection)

    @Slot(int, str, object)
    def _on_ok(self, token: int, label: str, samples: object) -> None:
        arr = samples if isinstance(samples, np.ndarray) else np.asarray(samples)
        self.ok.append((token, label, arr))

    @Slot(int, str)
    def _on_err(self, token: int, msg: str) -> None:
        self.err.append((token, msg))


def _provider_with_response() -> ResponseProvider:
    xml = Path(get_example_file("IU_ANMO_00_BHZ.xml"))
    dev = DeviceConfig(
        name="anmo",
        host="example.com",
        port=18000,
        response_metadata=ResponseMetadataConfig(path=xml, format="stationxml"),
    )
    return ResponseProvider([dev], config_dir=None)


def _provider_without_response() -> ResponseProvider:
    dev = DeviceConfig(name="bare", host="example.com", port=18000)
    return ResponseProvider([dev], config_dir=None)


def test_real_deconvolution_returns_physical_samples_and_label() -> None:
    """Real IU.ANMO StationXML: VEL output yields physical samples + label."""
    worker = DeconvolutionWorker(_provider_with_response())
    spy = _Spy(worker)
    rng = np.random.default_rng(0)
    counts = rng.standard_normal(int(_FS * 60)).astype(np.float64)

    worker.compute(1, "anmo", _NSLC, "VEL", counts, _FS, float(_START))

    assert not spy.err, f"unexpected failures: {spy.err}"
    assert len(spy.ok) == 1
    token, label, out = spy.ok[0]
    assert token == 1
    assert label == "Velocity (m/s)"
    assert out.shape == counts.shape
    assert out.dtype == np.float64
    # The physical trace must differ from the counts (a real transform ran),
    # and stay finite (rule 10: a non-degenerate, usable result).
    assert np.all(np.isfinite(out))
    assert not np.allclose(out, counts)
    assert float(np.var(out)) > 0.0


@pytest.mark.parametrize(
    ("output", "expected"),
    [("VEL", "Velocity (m/s)"), ("ACC", "Acceleration (m/s²)"), ("DISP", "Displacement (m)")],
)
def test_unit_labels(output: str, expected: str) -> None:
    worker = DeconvolutionWorker(_provider_with_response())
    spy = _Spy(worker)
    counts = np.random.default_rng(1).standard_normal(int(_FS * 30)).astype(np.float64)

    worker.compute(7, "anmo", _NSLC, output, counts, _FS, float(_START))

    assert not spy.err
    assert spy.ok[0][1] == expected


def test_no_response_device_emits_failed() -> None:
    worker = DeconvolutionWorker(_provider_without_response())
    spy = _Spy(worker)
    counts = np.zeros(100, dtype=np.float64)

    worker.compute(3, "bare", _NSLC, "VEL", counts, _FS, float(_START))

    assert not spy.ok
    assert len(spy.err) == 1
    assert spy.err[0][0] == 3
    assert "No response metadata" in spy.err[0][1]


def test_no_matching_response_for_time_emits_failed() -> None:
    """A channel/time with no matching response in the inventory fails."""
    worker = DeconvolutionWorker(_provider_with_response())
    spy = _Spy(worker)
    counts = np.zeros(100, dtype=np.float64)

    # Wrong NSLC → no match in the IU.ANMO inventory.
    worker.compute(4, "anmo", "XX.NONE.00.BHZ", "VEL", counts, _FS, float(_START))

    assert not spy.ok
    assert len(spy.err) == 1
    assert spy.err[0][0] == 4


def test_malformed_nslc_emits_failed_not_crash() -> None:
    worker = DeconvolutionWorker(_provider_with_response())
    spy = _Spy(worker)

    worker.compute(5, "anmo", "BAD", "VEL", np.zeros(10), _FS, float(_START))

    assert not spy.ok
    assert spy.err and spy.err[0][0] == 5
