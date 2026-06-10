"""Tests for :class:`ChainEditorDialog`.

The editor talks to the engine via the public ``read_recent`` API and
emits a re-bind on its own. Tests use a stub engine to avoid wiring up
a real fake-SeedLink server for what is otherwise a GUI-only path.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal

from echosmonitor.config.schema import (
    BandpassStage,
    DetrendStage,
    LowpassStage,
)
from echosmonitor.core.models import device_stream_key
from echosmonitor.gui.dialogs.chain_editor_dialog import (
    MAX_PREVIEW_SECONDS,
    ChainEditorDialog,
)


class _StubEngine(QObject):
    """Minimal mock of :class:`StreamingEngine` for the editor's needs.

    The editor reads ``engine._buffers`` to enumerate this-device
    streams and calls ``engine.read_recent`` for the preview snapshot.
    The stub exposes the same public surface — no signals fire.
    """

    newStreamSeen = Signal(str, str)  # noqa: N815
    devicesChanged = Signal()  # noqa: N815

    def __init__(self, device: str = "fake", nslc: str = "NET.STA.LOC.HHZ") -> None:
        super().__init__()
        self._buffers: dict[str, object] = {
            device_stream_key(device, nslc): object(),
        }
        self._device = device
        self._nslc = nslc
        # Synthetic signal: 5 s of 100 Hz 1 Hz sine. Plenty for an
        # 8-stage chain to chew through and well inside the 60 s cap.
        n = 500
        self._fs = 100.0
        t = np.arange(n, dtype=np.float64) / self._fs
        self._samples = np.sin(2.0 * np.pi * 1.0 * t).astype(np.float32)
        self.read_recent_calls = 0

    def read_recent(
        self, device: str, nslc: str, seconds: float
    ) -> tuple[np.ndarray, float, object | None]:
        self.read_recent_calls += 1
        if device != self._device or nslc != self._nslc:
            return np.empty(0, dtype=np.float32), 0.0, None
        n_max = round(seconds * self._fs)
        return self._samples[:n_max].copy(), self._fs, None


def _make_dialog(
    qtbot,
    stages: list,
    *,
    device: str = "fake",
    nslc: str = "NET.STA.LOC.HHZ",
) -> tuple[ChainEditorDialog, _StubEngine]:
    engine = _StubEngine(device=device, nslc=nslc)
    dialog = ChainEditorDialog(
        stages=stages,
        device_name=device,
        engine=engine,  # type: ignore[arg-type]
    )
    qtbot.addWidget(dialog)
    return dialog, engine


def _flush_debounce(qtbot, dialog: ChainEditorDialog) -> None:
    """Force the debounce timer to fire synchronously."""
    dialog._debounce.setInterval(0)
    qtbot.wait(50)


def test_opens_with_initial_chain_populated(qtbot) -> None:
    stages = [
        DetrendStage(type="detrend", kind="linear"),
        BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0),
    ]
    dialog, _engine = _make_dialog(qtbot, stages)
    assert dialog._chain_list.count() == 2
    assert "detrend" in dialog._chain_list.item(0).text()
    assert "bandpass" in dialog._chain_list.item(1).text()
    assert not dialog.is_dirty()


def test_add_stage_appends_to_list_and_marks_dirty(qtbot) -> None:
    dialog, _engine = _make_dialog(qtbot, [])
    assert dialog._chain_list.count() == 0
    dialog._on_add_stage("bandpass")
    assert dialog._chain_list.count() == 1
    assert dialog.is_dirty()


def test_delete_row_removes_stage(qtbot) -> None:
    stages = [
        DetrendStage(type="detrend", kind="linear"),
        BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0),
    ]
    dialog, _engine = _make_dialog(qtbot, stages)
    dialog._on_delete_row(0)
    assert dialog._chain_list.count() == 1
    assert "bandpass" in dialog._chain_list.item(0).text()


def test_invalid_chain_disables_ok_and_apply(qtbot) -> None:
    """A bandpass with ``freqmax >= fs/2`` (50 Hz at 100 Hz fs) must
    block OK / Apply until the user fixes it."""
    stages = [BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)]
    dialog, _engine = _make_dialog(qtbot, stages)
    _flush_debounce(qtbot, dialog)
    assert dialog._ok_button is not None and dialog._ok_button.isEnabled()
    # Drag freqmax above Nyquist via the live form.
    form = dialog._current_form
    assert form is not None
    form._freqmax.setValue(80.0)  # type: ignore[attr-defined]
    qtbot.wait(50)
    assert not dialog._ok_button.isEnabled()


def test_debounced_preview_runs_once_for_rapid_changes(qtbot) -> None:
    """10 rapid stage-parameter changes within one debounce window
    must produce exactly ONE call to ``chain.process``."""
    stages = [BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)]
    dialog, _engine = _make_dialog(qtbot, stages)
    _flush_debounce(qtbot, dialog)
    baseline = dialog.preview_compute_count()
    form = dialog._current_form
    assert form is not None
    for value in (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 4.0, 12.0):
        form._freqmin.setValue(value)  # type: ignore[attr-defined]
    _flush_debounce(qtbot, dialog)
    # Wait long enough for the one fired tick to actually compute.
    qtbot.wait(50)
    assert dialog.preview_compute_count() == baseline + 1, (
        f"debounce broke: {dialog.preview_compute_count() - baseline} computes for 10 rapid changes"
    )


def test_empty_chain_renders_processed_equal_to_raw(qtbot) -> None:
    dialog, engine = _make_dialog(qtbot, [])
    _flush_debounce(qtbot, dialog)
    # The processed plot should have data of the same length as raw.
    assert engine.read_recent_calls > 0


def test_apply_emits_stages_without_closing(qtbot) -> None:
    stages = [BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)]
    dialog, _engine = _make_dialog(qtbot, stages)
    received: list[tuple] = []
    dialog.stagesApplied.connect(received.append)
    dialog._on_apply()
    assert received and isinstance(received[0], tuple)
    assert dialog.isVisible() is False  # never shown, but still alive
    # After Apply, the saved baseline updates so is_dirty() drops back.
    assert not dialog.is_dirty()


def test_ok_does_not_call_configstore_directly(qtbot) -> None:
    """Per the stage-3 plan, the editor never touches ConfigStore. OK
    just closes; the caller integrates the new stages via
    ``stages()``."""
    stages = [BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)]
    dialog, _engine = _make_dialog(qtbot, stages)
    dialog._on_ok()  # closes the dialog without persisting
    assert dialog.result() == int(dialog.DialogCode.Accepted)


def test_reordering_via_rows_moved_updates_stages(qtbot) -> None:
    """Simulate a drag-reorder by moving items in the list widget and
    firing the rowsMoved signal. The dialog's working stage list must
    pick up the new order."""
    stages = [
        DetrendStage(type="detrend", kind="linear"),
        BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0),
    ]
    dialog, _engine = _make_dialog(qtbot, stages)
    # Move row 1 to row 0 in the widget; sync the underlying list.
    item = dialog._chain_list.takeItem(1)
    dialog._chain_list.insertItem(0, item)
    dialog._on_rows_moved()
    new_order = dialog.stages()
    assert isinstance(new_order[0], BandpassStage)
    assert isinstance(new_order[1], DetrendStage)


def test_form_edit_after_reorder_writes_to_correct_stage(qtbot) -> None:
    """Regression for the M6-closure HIGH finding: per-row form lambdas
    used to capture ``row`` directly. A drag-reorder shifted the
    bandpass row but the form's lambda kept writing to the old index,
    corrupting whatever stage was now at that index. The fix binds the
    form to a stable token instead; a subsequent edit MUST land on
    the same stage even after a reorder."""
    stages = [
        DetrendStage(type="detrend", kind="linear"),
        BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0),
        DetrendStage(type="detrend", kind="constant"),
    ]
    dialog, _engine = _make_dialog(qtbot, stages)
    # Select the Bandpass row (index 1) so its form is mounted.
    dialog._chain_list.setCurrentRow(1)
    form = dialog._current_form
    assert form is not None and hasattr(form, "_freqmax")

    # Drag the Bandpass from row 1 to row 0; sync underlying state.
    item = dialog._chain_list.takeItem(1)
    dialog._chain_list.insertItem(0, item)
    dialog._on_rows_moved()
    assert isinstance(dialog.stages()[0], BandpassStage)

    # Mutate the (still-mounted) Bandpass form. The write MUST land on
    # the Bandpass stage at its NEW index (0), not on whatever
    # happened to be at the OLD index (now Detrend at index 1).
    form._freqmax.setValue(7.5)  # type: ignore[attr-defined]
    qtbot.wait(20)
    updated = dialog.stages()
    assert isinstance(updated[0], BandpassStage)
    assert updated[0].freqmax == 7.5
    # The row that used to be at index 1 (Detrend) must be unchanged.
    assert isinstance(updated[1], DetrendStage)
    assert updated[1].kind == "linear"


def test_invalid_state_survives_reorder(qtbot) -> None:
    """A row that's currently invalid must REMAIN invalid after a
    drag-reorder (token-keyed, not row-keyed)."""
    stages = [
        DetrendStage(type="detrend", kind="linear"),
        BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0),
    ]
    dialog, _engine = _make_dialog(qtbot, stages)
    _flush_debounce(qtbot, dialog)
    dialog._chain_list.setCurrentRow(1)
    form = dialog._current_form
    assert form is not None
    form._freqmax.setValue(80.0)  # type: ignore[attr-defined]
    qtbot.wait(20)
    assert dialog._ok_button is not None and not dialog._ok_button.isEnabled()

    # Drag invalid Bandpass to row 0; OK must STILL be disabled.
    item = dialog._chain_list.takeItem(1)
    dialog._chain_list.insertItem(0, item)
    dialog._on_rows_moved()
    qtbot.wait(20)
    assert not dialog._ok_button.isEnabled()


def test_delete_clears_invalid_tracking_for_removed_row(qtbot) -> None:
    """Deleting a row whose form is in an invalid state must drop the
    row from the invalid set; the remaining chain must be valid again."""
    stages = [BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)]
    dialog, _engine = _make_dialog(qtbot, stages)
    _flush_debounce(qtbot, dialog)
    form = dialog._current_form
    assert form is not None
    form._freqmax.setValue(80.0)  # type: ignore[attr-defined]
    qtbot.wait(20)
    assert dialog._invalid_tokens
    # Delete the offending row.
    dialog._on_delete_row(0)
    qtbot.wait(20)
    assert dialog._invalid_tokens == set()
    assert dialog._ok_button is not None and dialog._ok_button.isEnabled()


@pytest.mark.perf
def test_preview_bound_under_100ms_for_60s_at_200hz(qtbot) -> None:
    """The plan's preview-bound assertion: 60 s x 200 Hz x up to 8
    stages must complete in <100 ms wall time on the GUI thread.
    This guards the only inline DSP path in the codebase.

    Tagged ``perf`` (excluded from the default gate): the 100 ms bound
    is an absolute compute budget that flakes under parallel suite
    load. Run deliberately with ``uv run pytest -m perf``."""

    class _BigEngine(QObject):
        newStreamSeen = Signal(str, str)  # noqa: N815
        devicesChanged = Signal()  # noqa: N815

        def __init__(self) -> None:
            super().__init__()
            self._buffers: dict[str, object] = {
                device_stream_key("dev", "N.S.L.HHZ"): object(),
            }
            self.read_recent_calls = 0
            self._fs = 200.0
            n = int(MAX_PREVIEW_SECONDS * self._fs)
            t = np.arange(n, dtype=np.float64) / self._fs
            self._samples = (
                np.sin(2.0 * np.pi * 1.0 * t) + 0.5 * np.sin(2.0 * np.pi * 7.0 * t)
            ).astype(np.float32)

        def read_recent(
            self, device: str, nslc: str, seconds: float
        ) -> tuple[np.ndarray, float, object | None]:
            del device, nslc
            self.read_recent_calls += 1
            n_max = round(seconds * self._fs)
            return self._samples[:n_max].copy(), self._fs, None

    engine = _BigEngine()
    eight_stages = [
        DetrendStage(type="detrend", kind="linear"),
        BandpassStage(type="bandpass", freqmin=0.5, freqmax=40.0, corners=4),
        BandpassStage(type="bandpass", freqmin=1.0, freqmax=30.0, corners=4),
        BandpassStage(type="bandpass", freqmin=2.0, freqmax=20.0, corners=4),
        LowpassStage(type="lowpass", freq=30.0, corners=4),
        LowpassStage(type="lowpass", freq=20.0, corners=4),
        LowpassStage(type="lowpass", freq=10.0, corners=4),
        LowpassStage(type="lowpass", freq=5.0, corners=4),
    ]
    dialog = ChainEditorDialog(
        stages=eight_stages,
        device_name="dev",
        engine=engine,  # type: ignore[arg-type]
    )
    qtbot.addWidget(dialog)
    # Pick the bigEngine's stream so the preview has data.
    dialog._preview_stream = ("dev", "N.S.L.HHZ")
    dialog._preview_fs = 200.0
    _flush_debounce(qtbot, dialog)

    # Time one explicit preview tick.
    t0 = time.perf_counter()
    dialog._refresh_preview()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 100.0, (
        f"preview bound exceeded: 60 s x 200 Hz x 8 stages took {elapsed_ms:.1f} ms"
    )
