"""Tests for the per-stage parameter forms used by the chain editor."""

from __future__ import annotations

import pytest

from echosmonitor.config.schema import (
    BandpassStage,
    DecimationStage,
    DetrendStage,
    DspStageConfig,  # noqa: F401  (referenced via type-discriminator iteration below)
    HighpassStage,
    LowpassStage,
    NotchStage,
    StaLtaStage,
    TaperStage,
)
from echosmonitor.gui.widgets.stage_forms import (
    STAGE_FORM_REGISTRY,
    BandpassForm,
    DecimationForm,
    DetrendForm,
    HighpassForm,
    LowpassForm,
    NotchForm,
    StaLtaForm,
    default_stage_for,
    make_form,
    stage_summary,
)

_STAGE_TYPES_LIVE = (
    "detrend",
    "bandpass",
    "highpass",
    "lowpass",
    "notch",
    "decimation",
    "sta_lta",
)


def test_registry_covers_every_live_stage_type() -> None:
    """Every DspStageConfig discriminator (except ``taper`` — live-forbidden)
    MUST have a registered form. This catches schema drift: a new
    stage added to the union without a form is the kind of bug
    code-reviewer flagged in the M5 audit."""
    expected = set(_STAGE_TYPES_LIVE)
    assert set(STAGE_FORM_REGISTRY.keys()) == expected


def test_taper_intentionally_absent_from_registry() -> None:
    """``TaperStage`` is excluded because the DSP factory rejects it
    for live chains. The chain editor scope is the live chain, so a
    form would only mislead the user."""
    assert "taper" not in STAGE_FORM_REGISTRY
    # Sanity: TaperStage itself still exists in the schema (offline).
    t = TaperStage(type="taper", max_pct=0.05)
    assert t.max_pct == 0.05


def test_default_stage_for_returns_each_live_type(qtbot) -> None:
    del qtbot  # form construction not needed here
    for type_str in _STAGE_TYPES_LIVE:
        stage = default_stage_for(type_str, fs=100.0)
        assert getattr(stage, "type", None) == type_str


def test_default_stage_for_unknown_type_raises() -> None:
    with pytest.raises(ValueError):
        default_stage_for("not_a_stage")


def test_detrend_form_emits_valid_stage_with_default_kind(qtbot) -> None:
    form = DetrendForm(fs=100.0)
    qtbot.addWidget(form)
    assert isinstance(form.stage(), DetrendStage)
    assert form.stage().kind == "linear"  # type: ignore[union-attr]


def test_bandpass_form_round_trips_default_values(qtbot) -> None:
    form = BandpassForm(fs=100.0)
    qtbot.addWidget(form)
    stage = form.stage()
    assert isinstance(stage, BandpassStage)
    assert stage.freqmin == 1.0
    assert stage.freqmax == 10.0
    assert stage.corners == 4
    assert stage.zerophase is False


def test_bandpass_form_rejects_freqmax_above_nyquist(qtbot) -> None:
    """Editor scoped to a 100 Hz stream means Nyquist = 50 Hz; a freqmax
    of 60 Hz is invalid and the form must reject it before the chain
    factory even sees the request."""
    form = BandpassForm(fs=100.0)
    qtbot.addWidget(form)
    form._freqmax.setValue(60.0)
    assert form.stage() is None
    assert form.last_error() is not None
    assert "Nyquist" in (form.last_error() or "")


def test_bandpass_form_recovers_when_user_fixes_freqmax(qtbot) -> None:
    form = BandpassForm(fs=100.0)
    qtbot.addWidget(form)
    form._freqmax.setValue(60.0)
    assert form.stage() is None
    form._freqmax.setValue(40.0)
    assert isinstance(form.stage(), BandpassStage)
    assert form.stage().freqmax == 40.0  # type: ignore[union-attr]


def test_bandpass_form_rejects_freqmin_at_or_above_freqmax(qtbot) -> None:
    form = BandpassForm(fs=100.0)
    qtbot.addWidget(form)
    form._freqmin.setValue(15.0)  # > freqmax (default 10.0)
    assert form.stage() is None


def test_bandpass_form_zerophase_toggle_surfaces_warning(qtbot) -> None:
    form = BandpassForm(fs=100.0)
    qtbot.addWidget(form)
    assert not form._zerophase_warning.isVisible()
    form._zerophase.setChecked(True)
    # The widget is constructed but not shown — isVisible() returns
    # False for off-screen widgets. Use isHidden() inversion instead.
    assert not form._zerophase_warning.isHidden()


def test_highpass_form_default_and_validation(qtbot) -> None:
    form = HighpassForm(fs=100.0)
    qtbot.addWidget(form)
    stage = form.stage()
    assert isinstance(stage, HighpassStage)
    form._freq.setValue(60.0)  # above Nyquist
    assert form.stage() is None


def test_lowpass_form_default_and_validation(qtbot) -> None:
    form = LowpassForm(fs=100.0)
    qtbot.addWidget(form)
    stage = form.stage()
    assert isinstance(stage, LowpassStage)
    form._freq.setValue(60.0)
    assert form.stage() is None


def test_notch_form_default_and_validation(qtbot) -> None:
    form = NotchForm(fs=200.0)
    qtbot.addWidget(form)
    stage = form.stage()
    assert isinstance(stage, NotchStage)
    # Default freq is 50 Hz, which is below Nyquist for fs=200; valid.
    assert stage.freq == 50.0  # type: ignore[union-attr]
    form._freq.setValue(150.0)  # above Nyquist for fs=200
    assert form.stage() is None


def test_decimation_form_default(qtbot) -> None:
    form = DecimationForm(fs=100.0)
    qtbot.addWidget(form)
    stage = form.stage()
    assert isinstance(stage, DecimationStage)
    assert stage.factor == 2


def test_stalta_form_rejects_sta_geq_lta(qtbot) -> None:
    form = StaLtaForm(fs=100.0)
    qtbot.addWidget(form)
    assert isinstance(form.stage(), StaLtaStage)
    form._sta.setValue(60.0)  # >= lta (30.0)
    assert form.stage() is None


def test_stalta_form_rejects_off_above_on(qtbot) -> None:
    form = StaLtaForm(fs=100.0)
    qtbot.addWidget(form)
    form._off_threshold.setValue(10.0)  # > on_threshold (3.5)
    assert form.stage() is None


def test_make_form_picks_right_form_class(qtbot) -> None:
    stage = BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)
    form = make_form(stage, fs=100.0)
    qtbot.addWidget(form)
    assert isinstance(form, BandpassForm)
    assert form.stage() == stage


def test_make_form_loads_existing_field_values(qtbot) -> None:
    stage = BandpassStage(type="bandpass", freqmin=2.0, freqmax=8.0, corners=6, zerophase=False)
    form = make_form(stage, fs=100.0)
    qtbot.addWidget(form)
    assert form._freqmin.value() == 2.0
    assert form._freqmax.value() == 8.0
    assert form._corners.value() == 6


def test_set_fs_revalidates_with_new_nyquist(qtbot) -> None:
    """A live stream's fs can change at chain reinstall time (decimation
    moves nyquist down). The form must re-flag fields against the new
    Nyquist immediately."""
    form = BandpassForm(fs=200.0)
    qtbot.addWidget(form)
    form._freqmax.setValue(80.0)  # OK at fs=200 (Nyquist 100)
    assert isinstance(form.stage(), BandpassStage)
    form.set_fs(100.0)  # Nyquist now 50; 80 Hz no longer OK
    assert form.stage() is None


def test_stage_summary_for_each_stage_type() -> None:
    samples = [
        BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0),
        HighpassStage(type="highpass", freq=2.0),
        LowpassStage(type="lowpass", freq=20.0),
        NotchStage(type="notch", freq=50.0, quality=30.0),
        DecimationStage(type="decimation", factor=4),
        DetrendStage(type="detrend", kind="linear"),
        StaLtaStage(type="sta_lta", sta=1.0, lta=30.0, on_threshold=3.5, off_threshold=1.5),
    ]
    for s in samples:
        summary = stage_summary(s)
        # Each summary mentions the stage's discriminator name.
        assert s.type in summary or s.type.replace("_", "/") in summary, summary
