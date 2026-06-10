"""Lock the contract between docs/MANUAL_TESTS.md, the bundled
``default.yaml`` example block, and the runtime schema + factory.

The "Bandpass on ANMO with STA/LTA tap" example in the manual-tests doc
is the canonical user-facing example for the live DSP chain. The two
device blocks commented into ``config/default.yaml`` mirror it (IRIS)
and add a second profile pointing at GFZ as a second public-server
example. If any field name in the schema, the factory, or the doc
drifts, this test fails — surfacing the rename instead of letting it
ship broken.

This is a regression guard for the M2 rename incident where renaming a
schema field would silently leave the doc snippet unparseable, and
extends with M3 multi-device coverage so both example blocks always
build a concrete chain.
"""

from __future__ import annotations

import yaml

from echosmonitor.config.schema import (
    BandpassStage,
    DetrendStage,
    DeviceConfig,
    StaLtaStage,
)
from echosmonitor.core.models import StreamID
from echosmonitor.dsp.factory import build_chain
from echosmonitor.dsp.stages import Bandpass, Detrend, StaLta

# Verbatim copy of the dsp_chain block from the "Bandpass on ANMO with
# STA/LTA tap (M2)" section of docs/MANUAL_TESTS.md. If the docs change,
# this string must be updated in lockstep.
_DOC_DSP_CHAIN_YAML = """
dsp_chain:
  - { type: detrend, kind: constant }
  - { type: bandpass, freqmin: 0.5, freqmax: 8.0, corners: 4, zerophase: false }
  - { type: sta_lta, sta: 1.0, lta: 30.0, on_threshold: 3.5, off_threshold: 1.5 }
"""


# Verbatim copy of the IRIS device example commented into the bundled
# ``config/default.yaml``. The "#" prefix is stripped programmatically
# below so the YAML round-trip is identical to what a user uncommenting
# the block would feed pyyaml.
_DEFAULT_YAML_IRIS_BLOCK = """
- name: iris-iu-anmo
  host: rtserve.iris.washington.edu
  port: 18000
  reconnect: { initial_delay_s: 1.0, max_delay_s: 60.0 }
  selectors:
    - { network: IU, station: ANMO, location: "00", channel: BHZ }
  dsp_chain:
    - { type: detrend, kind: constant }
    - { type: bandpass, freqmin: 0.5, freqmax: 8.0, corners: 4, zerophase: false }
    - { type: sta_lta, sta: 1.0, lta: 30.0, on_threshold: 3.5, off_threshold: 1.5 }
"""


# Verbatim copy of the GFZ device example commented into the bundled
# ``config/default.yaml``. Replaces the INGV example that became
# unroutable from EU consumer ISPs in May 2026 (POSTMORTEMS 2026-05-09).
# Same DSP tuning as IRIS (0.5-8 Hz bandpass) — global-scale, broadband
# instrument. The second-device contract is now host/selector
# distinctness, not chain distinctness.
_DEFAULT_YAML_GFZ_BLOCK = """
- name: gfz-de
  host: geofon.gfz-potsdam.de
  port: 18000
  reconnect: { initial_delay_s: 1.0, max_delay_s: 60.0, connect_timeout_s: 10.0 }
  selectors:
    - { network: GE, station: WLF, location: "", channel: BHZ }
  dsp_chain:
    - { type: detrend, kind: constant }
    - { type: bandpass, freqmin: 0.5, freqmax: 8.0, corners: 4, zerophase: false }
    - { type: sta_lta, sta: 1.0, lta: 30.0, on_threshold: 3.5, off_threshold: 1.5 }
"""


def test_docs_bandpass_anmo_yaml_validates_against_schema() -> None:
    """The doc snippet must validate as a `DeviceConfig.dsp_chain`."""
    payload = yaml.safe_load(_DOC_DSP_CHAIN_YAML)
    device = DeviceConfig(
        name="iris-iu-anmo",
        host="rtserve.iris.washington.edu",
        port=18000,
        dsp_chain=payload["dsp_chain"],
    )

    assert len(device.dsp_chain) == 3
    assert isinstance(device.dsp_chain[0], DetrendStage)
    assert isinstance(device.dsp_chain[1], BandpassStage)
    assert isinstance(device.dsp_chain[2], StaLtaStage)

    detrend = device.dsp_chain[0]
    bandpass = device.dsp_chain[1]
    sta_lta = device.dsp_chain[2]
    assert detrend.kind == "constant"
    assert bandpass.freqmin == 0.5
    assert bandpass.freqmax == 8.0
    assert bandpass.corners == 4
    assert bandpass.zerophase is False
    assert sta_lta.sta == 1.0
    assert sta_lta.lta == 30.0
    assert sta_lta.on_threshold == 3.5
    assert sta_lta.off_threshold == 1.5


def test_docs_bandpass_anmo_yaml_builds_concrete_chain() -> None:
    """The doc snippet must also build successfully via `build_chain` and
    produce exactly three concrete stages of the expected types — locking
    the contract between docs, schema, and stage classes."""
    payload = yaml.safe_load(_DOC_DSP_CHAIN_YAML)
    device = DeviceConfig(
        name="iris-iu-anmo",
        host="rtserve.iris.washington.edu",
        port=18000,
        dsp_chain=payload["dsp_chain"],
    )

    sid = StreamID("IU", "ANMO", "00", "BHZ")
    # ANMO BHZ runs at 20 Hz — the doc's freqmax of 8 Hz must stay
    # below Nyquist or the factory raises.
    chain = build_chain(list(device.dsp_chain), fs_in=20.0, stream_id=sid, live=True)
    stages = chain.stages
    assert len(stages) == 3
    assert isinstance(stages[0], Detrend)
    assert isinstance(stages[1], Bandpass)
    assert isinstance(stages[2], StaLta)


def test_default_yaml_iris_example_block_parses_and_builds() -> None:
    """The IRIS example commented into ``config/default.yaml`` must parse
    as one ``DeviceConfig`` and produce a concrete chain — guards against
    a future schema rename silently breaking the canonical example."""
    blocks = yaml.safe_load(_DEFAULT_YAML_IRIS_BLOCK)
    assert isinstance(blocks, list) and len(blocks) == 1
    raw = blocks[0]
    device = DeviceConfig(**raw)
    assert device.name == "iris-iu-anmo"
    assert device.host == "rtserve.iris.washington.edu"
    assert len(device.selectors) == 1
    assert len(device.dsp_chain) == 3

    sid = StreamID("IU", "ANMO", "00", "BHZ")
    # IU.ANMO.00.BHZ runs at 20 Hz — freqmax=8 Hz < Nyquist (10 Hz).
    chain = build_chain(list(device.dsp_chain), fs_in=20.0, stream_id=sid, live=True)
    assert isinstance(chain.stages[0], Detrend)
    assert isinstance(chain.stages[1], Bandpass)
    assert isinstance(chain.stages[2], StaLta)


def test_default_yaml_gfz_example_block_parses_and_builds() -> None:
    """The GFZ example commented into ``config/default.yaml`` must parse
    as one ``DeviceConfig`` and produce a concrete chain. Distinct from
    the IRIS block in host and selector (same DSP tuning); locks the
    second-device example contract."""
    blocks = yaml.safe_load(_DEFAULT_YAML_GFZ_BLOCK)
    assert isinstance(blocks, list) and len(blocks) == 1
    raw = blocks[0]
    device = DeviceConfig(**raw)
    assert device.name == "gfz-de"
    assert device.host == "geofon.gfz-potsdam.de"
    assert len(device.selectors) == 1
    sel = device.selectors[0]
    assert sel.network == "GE"
    assert sel.station == "WLF"
    assert sel.location == ""
    assert sel.channel == "BHZ"
    assert len(device.dsp_chain) == 3

    bp_cfg = device.dsp_chain[1]
    assert isinstance(bp_cfg, BandpassStage)
    assert bp_cfg.freqmin == 0.5
    assert bp_cfg.freqmax == 8.0

    sid = StreamID("GE", "WLF", "", "BHZ")
    # GE.WLF..BHZ runs at 20 Hz — freqmax=8 Hz < Nyquist (10 Hz).
    chain = build_chain(list(device.dsp_chain), fs_in=20.0, stream_id=sid, live=True)
    assert isinstance(chain.stages[0], Detrend)
    assert isinstance(chain.stages[1], Bandpass)
    assert isinstance(chain.stages[2], StaLta)


def test_bundled_default_yaml_contains_both_example_blocks() -> None:
    """The shipped ``default.yaml`` must keep both example blocks in
    sync with the literals tested above. If a future cleanup deletes
    one of them or renames a key, this test catches it before merge."""
    from importlib.resources import files

    text = files("echosmonitor.config").joinpath("default.yaml").read_text(encoding="utf-8")
    assert "name: iris-iu-anmo" in text
    assert "host: rtserve.iris.washington.edu" in text
    assert "name: gfz-de" in text
    assert "host: geofon.gfz-potsdam.de" in text
    # Both blocks share the same DSP tuning — distinguish them via the
    # selector's station code instead. ANMO is the IRIS station; WLF is
    # the GFZ one.
    assert "station: ANMO" in text
    assert "station: WLF" in text
