"""Per-stage parameter forms for the DSP chain editor.

Each form widget renders the editable fields of one
:class:`DspStageConfig` discriminated-union variant. When any field
changes the form attempts to rebuild the pydantic model via
``model_validate``; on success it emits ``stageChanged(stage_model)``
with the new value, on failure it emits ``stageInvalid(reason)`` and
the chain editor disables OK / Apply until the user fixes the input.

Fs-aware validation (Bandpass / Highpass / Lowpass / Notch):
the form takes an ``fs`` value at construction time. Frequency entries
beyond the Nyquist limit ``fs/2`` are flagged with a red border and a
tooltip; the chain build will fail anyway so this is purely a hint
that surfaces *before* the user clicks OK.

Zerophase warning: ticking ``zerophase`` on Bandpass / Highpass /
Lowpass renders a yellow inline note. Live streaming still uses a
causal filter under the hood (the DSP factory force-falls-back); the
note tells the user the chain will *not* behave the way they expect
in live mode and that ``zerophase=True`` is "offline review only".

Taper is intentionally not represented here. The DSP factory rejects
``TaperStage`` for live chains (an offline-only operation), and the
editor scope is the live chain.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import ValidationError
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QWidget,
)

from echosmonitor.config.schema import (
    BandpassStage,
    DecimationStage,
    DetrendStage,
    HighpassStage,
    LowpassStage,
    NotchStage,
    StaLtaStage,
)

# Style sheets — kept module-level so the chain editor and the
# downstream tests can target them with QSS selectors.
_INVALID_STYLE = "QDoubleSpinBox, QSpinBox { border: 1px solid #c0392b; background: #2b1316; }"
_VALID_STYLE = ""

# Inline yellow warning shown on causal-filter forms when the user
# ticks ``zerophase``.
_ZEROPHASE_WARNING = (
    "⚠ zerophase is acausal — live streaming forces causal. Use only for offline review."
)
_ZEROPHASE_STYLE = "QLabel { color: #d9a441; font-size: 10px; }"


class _StageForm(Protocol):
    """Contract every stage form satisfies."""

    stageChanged: Signal  # emitted with the new pydantic stage on validate-success  # noqa: N815
    stageInvalid: Signal  # emitted with the validation error str on failure  # noqa: N815

    def set_fs(self, fs: float | None) -> None:
        """Tell the form the current stream's input sample rate so it
        can do Nyquist-aware validation. ``None`` disables the check
        (the chain editor falls back to a sane default of 100 Hz)."""

    def stage(self) -> object | None:
        """Return the most recent valid pydantic stage, or ``None`` if
        the form is in an invalid state."""


def _new_spin(
    *,
    minimum: float = 0.0,
    maximum: float = 1e6,
    step: float = 0.1,
    decimals: int = 4,
) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSingleStep(step)
    spin.setDecimals(decimals)
    return spin


def _new_int_spin(*, minimum: int = 1, maximum: int = 1000) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    return spin


class _BaseStageForm(QWidget):
    """Shared scaffolding for the per-stage forms.

    Subclasses build their fields in ``_build_fields`` and implement
    ``_assemble_stage`` to construct a pydantic model from the current
    field values. The base class wires every field-change signal to
    :meth:`_revalidate`, manages the fs-aware red-border highlighting,
    and emits ``stageChanged`` / ``stageInvalid`` accordingly.
    """

    stageChanged = Signal(object)  # noqa: N815
    stageInvalid = Signal(str)  # noqa: N815

    def __init__(self, fs: float | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fs: float | None = fs
        self._current_stage: object | None = None
        self._last_error: str | None = None
        self._form_layout = QFormLayout(self)
        self._form_layout.setContentsMargins(0, 0, 0, 0)
        self._build_fields()
        self._revalidate()

    # ------------------------------------------------------------------
    # Public API (matches _StageForm protocol)
    # ------------------------------------------------------------------
    def set_fs(self, fs: float | None) -> None:
        self._fs = fs
        self._revalidate()

    def stage(self) -> object | None:
        return self._current_stage

    def last_error(self) -> str | None:
        return self._last_error

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------
    def _build_fields(self) -> None:
        """Subclass: populate ``self._form_layout`` and connect field
        signals to ``self._revalidate``."""
        raise NotImplementedError

    def _assemble_stage(self) -> object:
        """Subclass: build the pydantic model from current values.
        Raises ``ValidationError`` for the caller to catch."""
        raise NotImplementedError

    def _highlight_fields_for(self, error: ValidationError | None) -> None:
        """Subclass: paint the offending fields red. ``error`` is
        ``None`` when the form is in an fs-aware Nyquist failure;
        subclasses still paint from ``self._last_error`` in that case.
        Default no-op."""

    def _clear_highlights(self) -> None:
        """Subclass: revert any red highlights. Default no-op."""

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _revalidate(self) -> None:
        try:
            stage = self._assemble_stage()
        except ValidationError as exc:
            self._current_stage = None
            self._last_error = str(exc.errors()[0].get("msg", "invalid"))
            self._highlight_fields_for(exc)
            self.stageInvalid.emit(self._last_error)
            return

        # fs-aware Nyquist hint, applied AFTER the pydantic round-trip
        # succeeds. The chain factory would also catch this at build
        # time; surfacing it here just saves a click for the user.
        nyquist_error = self._check_nyquist(stage)
        if nyquist_error is not None:
            self._current_stage = None
            self._last_error = nyquist_error
            # Subclasses paint their own fields red from
            # ``self._last_error``; we do not synthesize a ValidationError
            # because pydantic v2 forbids user instantiation.
            self._highlight_fields_for(None)
            self.stageInvalid.emit(nyquist_error)
            return

        self._current_stage = stage
        self._last_error = None
        self._clear_highlights()
        self.stageChanged.emit(stage)

    def _check_nyquist(self, _stage: object) -> str | None:
        return None


# ----------------------------------------------------------------------
# Per-stage form implementations
# ----------------------------------------------------------------------
class DetrendForm(_BaseStageForm):
    def _build_fields(self) -> None:
        self._kind = QComboBox(self)
        for k in ("linear", "constant", "demean"):
            self._kind.addItem(k)
        self._kind.currentIndexChanged.connect(self._revalidate)
        self._form_layout.addRow("Kind:", self._kind)

    def _assemble_stage(self) -> object:
        kind = self._kind.currentText()
        if kind not in ("linear", "constant", "demean"):
            kind = "linear"
        return DetrendStage.model_validate({"type": "detrend", "kind": kind})

    def load_from(self, stage: DetrendStage) -> None:
        idx = self._kind.findText(stage.kind)
        if idx >= 0:
            self._kind.setCurrentIndex(idx)


class _CausalFilterForm(_BaseStageForm):
    """Common base for Bandpass / Highpass / Lowpass — they all share
    the corners + zerophase + Nyquist-warning machinery."""

    def _build_common_corners_and_zerophase(self) -> None:
        self._corners = _new_int_spin(minimum=1, maximum=12)
        self._corners.setValue(4)
        self._corners.valueChanged.connect(self._revalidate)
        self._form_layout.addRow("Corners:", self._corners)

        self._zerophase = QCheckBox(self)
        self._zerophase.toggled.connect(self._revalidate)
        self._zerophase.toggled.connect(self._update_zerophase_warning)
        self._zerophase_warning = QLabel(_ZEROPHASE_WARNING, self)
        self._zerophase_warning.setStyleSheet(_ZEROPHASE_STYLE)
        self._zerophase_warning.setVisible(False)
        zp_row = QHBoxLayout()
        zp_row.addWidget(self._zerophase)
        zp_row.addWidget(self._zerophase_warning, stretch=1)
        wrap = QWidget(self)
        wrap.setLayout(zp_row)
        self._form_layout.addRow("Zerophase:", wrap)

    def _update_zerophase_warning(self, checked: bool) -> None:
        self._zerophase_warning.setVisible(checked)


class BandpassForm(_CausalFilterForm):
    def _build_fields(self) -> None:
        self._freqmin = _new_spin(minimum=0.001, maximum=1000.0, step=0.1)
        self._freqmin.setValue(1.0)
        self._freqmin.valueChanged.connect(self._revalidate)
        self._freqmax = _new_spin(minimum=0.001, maximum=1000.0, step=0.1)
        self._freqmax.setValue(10.0)
        self._freqmax.valueChanged.connect(self._revalidate)
        self._form_layout.addRow("Freqmin (Hz):", self._freqmin)
        self._form_layout.addRow("Freqmax (Hz):", self._freqmax)
        self._build_common_corners_and_zerophase()

    def _assemble_stage(self) -> object:
        return BandpassStage(
            type="bandpass",
            freqmin=self._freqmin.value(),
            freqmax=self._freqmax.value(),
            corners=self._corners.value(),
            zerophase=self._zerophase.isChecked(),
        )

    def _check_nyquist(self, stage: object) -> str | None:
        if self._fs is None or not isinstance(stage, BandpassStage):
            return None
        nyq = self._fs / 2.0
        if stage.freqmax >= nyq:
            return f"freqmax ({stage.freqmax} Hz) must be < Nyquist ({nyq} Hz)"
        return None

    def _highlight_fields_for(self, _error: ValidationError | None) -> None:
        self._freqmax.setStyleSheet(_INVALID_STYLE)
        self._freqmax.setToolTip(self._last_error or "")

    def _clear_highlights(self) -> None:
        self._freqmax.setStyleSheet(_VALID_STYLE)
        self._freqmax.setToolTip("")

    def load_from(self, stage: BandpassStage) -> None:
        self._freqmin.setValue(stage.freqmin)
        self._freqmax.setValue(stage.freqmax)
        self._corners.setValue(stage.corners)
        self._zerophase.setChecked(stage.zerophase)


class HighpassForm(_CausalFilterForm):
    def _build_fields(self) -> None:
        self._freq = _new_spin(minimum=0.001, maximum=1000.0, step=0.1)
        self._freq.setValue(1.0)
        self._freq.valueChanged.connect(self._revalidate)
        self._form_layout.addRow("Freq (Hz):", self._freq)
        self._build_common_corners_and_zerophase()

    def _assemble_stage(self) -> object:
        return HighpassStage(
            type="highpass",
            freq=self._freq.value(),
            corners=self._corners.value(),
            zerophase=self._zerophase.isChecked(),
        )

    def _check_nyquist(self, stage: object) -> str | None:
        if self._fs is None or not isinstance(stage, HighpassStage):
            return None
        nyq = self._fs / 2.0
        if stage.freq >= nyq:
            return f"freq ({stage.freq} Hz) must be < Nyquist ({nyq} Hz)"
        return None

    def _highlight_fields_for(self, _error: ValidationError | None) -> None:
        self._freq.setStyleSheet(_INVALID_STYLE)
        self._freq.setToolTip(self._last_error or "")

    def _clear_highlights(self) -> None:
        self._freq.setStyleSheet(_VALID_STYLE)
        self._freq.setToolTip("")

    def load_from(self, stage: HighpassStage) -> None:
        self._freq.setValue(stage.freq)
        self._corners.setValue(stage.corners)
        self._zerophase.setChecked(stage.zerophase)


class LowpassForm(_CausalFilterForm):
    def _build_fields(self) -> None:
        self._freq = _new_spin(minimum=0.001, maximum=1000.0, step=0.1)
        self._freq.setValue(10.0)
        self._freq.valueChanged.connect(self._revalidate)
        self._form_layout.addRow("Freq (Hz):", self._freq)
        self._build_common_corners_and_zerophase()

    def _assemble_stage(self) -> object:
        return LowpassStage(
            type="lowpass",
            freq=self._freq.value(),
            corners=self._corners.value(),
            zerophase=self._zerophase.isChecked(),
        )

    def _check_nyquist(self, stage: object) -> str | None:
        if self._fs is None or not isinstance(stage, LowpassStage):
            return None
        nyq = self._fs / 2.0
        if stage.freq >= nyq:
            return f"freq ({stage.freq} Hz) must be < Nyquist ({nyq} Hz)"
        return None

    def _highlight_fields_for(self, _error: ValidationError | None) -> None:
        self._freq.setStyleSheet(_INVALID_STYLE)
        self._freq.setToolTip(self._last_error or "")

    def _clear_highlights(self) -> None:
        self._freq.setStyleSheet(_VALID_STYLE)
        self._freq.setToolTip("")

    def load_from(self, stage: LowpassStage) -> None:
        self._freq.setValue(stage.freq)
        self._corners.setValue(stage.corners)
        self._zerophase.setChecked(stage.zerophase)


class NotchForm(_BaseStageForm):
    def _build_fields(self) -> None:
        self._freq = _new_spin(minimum=0.001, maximum=1000.0, step=0.1)
        self._freq.setValue(50.0)
        self._freq.valueChanged.connect(self._revalidate)
        self._quality = _new_spin(minimum=0.1, maximum=1000.0, step=1.0)
        self._quality.setValue(30.0)
        self._quality.valueChanged.connect(self._revalidate)
        self._form_layout.addRow("Freq (Hz):", self._freq)
        self._form_layout.addRow("Quality:", self._quality)

    def _assemble_stage(self) -> object:
        return NotchStage(
            type="notch",
            freq=self._freq.value(),
            quality=self._quality.value(),
        )

    def _check_nyquist(self, stage: object) -> str | None:
        if self._fs is None or not isinstance(stage, NotchStage):
            return None
        nyq = self._fs / 2.0
        if stage.freq >= nyq:
            return f"freq ({stage.freq} Hz) must be < Nyquist ({nyq} Hz)"
        return None

    def _highlight_fields_for(self, _error: ValidationError | None) -> None:
        self._freq.setStyleSheet(_INVALID_STYLE)
        self._freq.setToolTip(self._last_error or "")

    def _clear_highlights(self) -> None:
        self._freq.setStyleSheet(_VALID_STYLE)
        self._freq.setToolTip("")

    def load_from(self, stage: NotchStage) -> None:
        self._freq.setValue(stage.freq)
        self._quality.setValue(stage.quality)


class DecimationForm(_BaseStageForm):
    def _build_fields(self) -> None:
        self._factor = _new_int_spin(minimum=2, maximum=16)
        self._factor.setValue(2)
        self._factor.valueChanged.connect(self._revalidate)
        self._no_filter = QCheckBox(self)
        self._no_filter.toggled.connect(self._revalidate)
        self._form_layout.addRow("Factor:", self._factor)
        self._form_layout.addRow("No filter:", self._no_filter)

    def _assemble_stage(self) -> object:
        return DecimationStage(
            type="decimation",
            factor=self._factor.value(),
            no_filter=self._no_filter.isChecked(),
        )

    def load_from(self, stage: DecimationStage) -> None:
        self._factor.setValue(stage.factor)
        self._no_filter.setChecked(stage.no_filter)


class StaLtaForm(_BaseStageForm):
    def _build_fields(self) -> None:
        self._sta = _new_spin(minimum=0.01, maximum=10000.0, step=0.5)
        self._sta.setValue(1.0)
        self._sta.valueChanged.connect(self._revalidate)
        self._lta = _new_spin(minimum=0.01, maximum=10000.0, step=1.0)
        self._lta.setValue(30.0)
        self._lta.valueChanged.connect(self._revalidate)
        self._on_threshold = _new_spin(minimum=0.01, maximum=1000.0, step=0.1)
        self._on_threshold.setValue(3.5)
        self._on_threshold.valueChanged.connect(self._revalidate)
        self._off_threshold = _new_spin(minimum=0.01, maximum=1000.0, step=0.1)
        self._off_threshold.setValue(1.5)
        self._off_threshold.valueChanged.connect(self._revalidate)
        self._form_layout.addRow("STA (s):", self._sta)
        self._form_layout.addRow("LTA (s):", self._lta)
        self._form_layout.addRow("On threshold:", self._on_threshold)
        self._form_layout.addRow("Off threshold:", self._off_threshold)

    def _assemble_stage(self) -> object:
        return StaLtaStage(
            type="sta_lta",
            sta=self._sta.value(),
            lta=self._lta.value(),
            on_threshold=self._on_threshold.value(),
            off_threshold=self._off_threshold.value(),
        )

    def _highlight_fields_for(self, _error: ValidationError | None) -> None:
        # Both sta>=lta and off>on map to "STA/LTA window or threshold
        # ordering is wrong" — paint both windows to be safe.
        self._sta.setStyleSheet(_INVALID_STYLE)
        self._lta.setStyleSheet(_INVALID_STYLE)
        for spin in (self._sta, self._lta, self._on_threshold, self._off_threshold):
            spin.setToolTip(self._last_error or "")

    def _clear_highlights(self) -> None:
        for spin in (self._sta, self._lta, self._on_threshold, self._off_threshold):
            spin.setStyleSheet(_VALID_STYLE)
            spin.setToolTip("")

    def load_from(self, stage: StaLtaStage) -> None:
        self._sta.setValue(stage.sta)
        self._lta.setValue(stage.lta)
        self._on_threshold.setValue(stage.on_threshold)
        self._off_threshold.setValue(stage.off_threshold)


# Public registry mapping stage type strings to their form classes. The
# chain editor uses this both to drive the "add stage" palette and to
# instantiate the right per-row form when a stage is selected. ``Taper``
# is deliberately absent — live chains forbid it.
STAGE_FORM_REGISTRY: dict[str, type[_BaseStageForm]] = {
    "detrend": DetrendForm,
    "bandpass": BandpassForm,
    "highpass": HighpassForm,
    "lowpass": LowpassForm,
    "notch": NotchForm,
    "decimation": DecimationForm,
    "sta_lta": StaLtaForm,
}


def stage_summary(stage: object) -> str:
    """Human-readable one-liner for the chain list (e.g.
    ``"bandpass 1.0-10.0 Hz, 4 corners, causal"``). Used by
    :class:`ChainEditorDialog` to label each row in the list."""
    if isinstance(stage, BandpassStage):
        kind = "zerophase" if stage.zerophase else "causal"
        return f"bandpass {stage.freqmin:g}-{stage.freqmax:g} Hz, {stage.corners} corners, {kind}"
    if isinstance(stage, HighpassStage):
        kind = "zerophase" if stage.zerophase else "causal"
        return f"highpass {stage.freq:g} Hz, {stage.corners} corners, {kind}"
    if isinstance(stage, LowpassStage):
        kind = "zerophase" if stage.zerophase else "causal"
        return f"lowpass {stage.freq:g} Hz, {stage.corners} corners, {kind}"
    if isinstance(stage, NotchStage):
        return f"notch {stage.freq:g} Hz, quality {stage.quality:g}"
    if isinstance(stage, DecimationStage):
        return f"decimation /{stage.factor}"
    if isinstance(stage, DetrendStage):
        return f"detrend ({stage.kind})"
    if isinstance(stage, StaLtaStage):
        return (
            f"sta/lta {stage.sta:g}/{stage.lta:g} s, "
            f"on={stage.on_threshold:g} off={stage.off_threshold:g}"
        )
    return type(stage).__name__


def default_stage_for(type_str: str, *, fs: float | None = None) -> object:
    """Return a sane default pydantic stage of the requested type,
    suitable for "Add stage" actions in the chain editor."""
    nyq = (fs or 100.0) / 2.0
    default_freq_min = max(0.001, min(1.0, nyq / 4.0))
    default_freq_max = max(default_freq_min + 0.5, min(10.0, nyq * 0.8))
    default_single_freq = max(0.001, min(1.0, nyq / 4.0))
    if type_str == "detrend":
        return DetrendStage(type="detrend", kind="linear")
    if type_str == "bandpass":
        return BandpassStage(
            type="bandpass",
            freqmin=default_freq_min,
            freqmax=default_freq_max,
            corners=4,
            zerophase=False,
        )
    if type_str == "highpass":
        return HighpassStage(
            type="highpass",
            freq=default_single_freq,
            corners=4,
            zerophase=False,
        )
    if type_str == "lowpass":
        return LowpassStage(
            type="lowpass",
            freq=default_freq_max,
            corners=4,
            zerophase=False,
        )
    if type_str == "notch":
        return NotchStage(
            type="notch",
            freq=max(0.001, min(50.0, nyq * 0.4)),
            quality=30.0,
        )
    if type_str == "decimation":
        return DecimationStage(type="decimation", factor=2, no_filter=False)
    if type_str == "sta_lta":
        return StaLtaStage(
            type="sta_lta",
            sta=1.0,
            lta=30.0,
            on_threshold=3.5,
            off_threshold=1.5,
        )
    raise ValueError(f"unknown stage type: {type_str!r}")


def make_form(stage: object, *, fs: float | None) -> _BaseStageForm:
    """Construct the right form widget for ``stage`` and pre-populate
    it with the stage's current field values."""
    type_str = type(stage).__name__.replace("Stage", "").lower()
    # The forms key off the discriminator string, not the class name —
    # match them up here.
    discriminator_map = {
        "detrend": "detrend",
        "bandpass": "bandpass",
        "highpass": "highpass",
        "lowpass": "lowpass",
        "notch": "notch",
        "decimation": "decimation",
        "stalta": "sta_lta",
    }
    type_key = discriminator_map.get(type_str, type_str)
    form_cls = STAGE_FORM_REGISTRY.get(type_key)
    if form_cls is None:
        raise ValueError(f"no form registered for stage type {type_str!r}")
    form = form_cls(fs=fs)
    loader = getattr(form, "load_from", None)
    if callable(loader):
        loader(stage)
    return form


__all__ = [
    "STAGE_FORM_REGISTRY",
    "BandpassForm",
    "DecimationForm",
    "DetrendForm",
    "HighpassForm",
    "LowpassForm",
    "NotchForm",
    "StaLtaForm",
    "default_stage_for",
    "make_form",
    "stage_summary",
]
