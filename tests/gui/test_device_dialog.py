"""GUI tests for :class:`DeviceForm` and :class:`DeviceDialog` (M4 stage B).

Drives the form's validation pipeline with synthetic field values and
the dialog's accept/cancel paths with a stubbed :class:`ConfigStore`.
No real config file, no engine, no QThread.
"""

from __future__ import annotations

from typing import Any

import pytest
from obspy.core.util import get_example_file
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox

from echosmonitor.config.schema import (
    DeviceConfig,
    ResponseMetadataConfig,
    StreamSelectorConfig,
)
from echosmonitor.core.exceptions import ConfigError
from echosmonitor.gui.dialogs.device_dialog import DeviceDialog, DeviceForm


class StubConfigStore:
    """Minimal :class:`ConfigStore` stand-in for dialog tests.

    Records calls to ``add_device`` / ``update_device`` for assertion;
    accepts an optional pre-seeded device list so the duplicate-name
    check has something to compare against. Setting
    :attr:`raise_on_add` to a :class:`ConfigError` makes the next
    ``add_device`` call raise — used by the "dialog stays open on
    ConfigError" test.
    """

    def __init__(self, devices: list[DeviceConfig] | None = None) -> None:
        self._devices: list[DeviceConfig] = list(devices or [])
        self.add_calls: list[DeviceConfig] = []
        self.update_calls: list[tuple[str, DeviceConfig]] = []
        self.raise_on_add: ConfigError | None = None

    @property
    def root(self) -> Any:
        # The dialog accesses ``store.root.devices``; we mimic the
        # attribute chain with a tiny anonymous holder.
        class _Root:
            def __init__(self, devices: list[DeviceConfig]) -> None:
                self.devices = list(devices)

        return _Root(self._devices)

    def add_device(self, cfg: DeviceConfig) -> None:
        self.add_calls.append(cfg)
        if self.raise_on_add is not None:
            err = self.raise_on_add
            self.raise_on_add = None
            raise err
        self._devices.append(cfg)

    def update_device(self, name: str, cfg: DeviceConfig) -> None:
        self.update_calls.append((name, cfg))
        for i, d in enumerate(self._devices):
            if d.name == name:
                self._devices[i] = cfg
                return
        raise ConfigError(f"unknown device {name!r}")


# ----------------------------------------------------------------------
# DeviceForm tests
# ----------------------------------------------------------------------


def test_add_dialog_initially_invalid_with_empty_form(qtbot) -> None:
    """A blank-name form is invalid; the OK button stays disabled."""
    form = DeviceForm(existing_names=("foo",))
    qtbot.addWidget(form)
    assert form.is_valid() is False
    err = form.validation_error()
    assert err is not None
    assert "name" in err.lower()


def test_form_becomes_valid_with_minimum_fields(qtbot) -> None:
    """Filling name + host produces a valid form."""
    form = DeviceForm(existing_names=())
    qtbot.addWidget(form)
    form._name_edit.setText("iris")
    form._host_edit.setText("rtserve.iris.washington.edu")
    assert form.is_valid() is True
    assert form.validation_error() is None


def test_duplicate_name_marks_form_invalid(qtbot) -> None:
    """Setting name to an existing device's name fails validation."""
    form = DeviceForm(existing_names=("foo",))
    qtbot.addWidget(form)
    form._name_edit.setText("foo")
    form._host_edit.setText("any-host")
    assert form.is_valid() is False
    err = form.validation_error()
    assert err is not None
    assert "foo" in err
    assert "exists" in err.lower()


def test_edit_mode_does_not_collide_with_self(qtbot) -> None:
    """Renaming a device "to itself" must stay valid."""
    form = DeviceForm(existing_names=("foo",), editing_name="foo")
    qtbot.addWidget(form)
    form._name_edit.setText("foo")
    form._host_edit.setText("any-host")
    assert form.is_valid() is True


def test_to_config_round_trips_initial(qtbot) -> None:
    """Building a form with an `initial` device and reading it back
    yields an equal config (modulo whitespace-trimming on text)."""
    initial = DeviceConfig(
        name="iris",
        host="rtserve.iris.washington.edu",
        port=18000,
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
    )
    form = DeviceForm(initial=initial)
    qtbot.addWidget(form)
    assert form.is_valid() is True
    out = form.to_config()
    assert out.name == initial.name
    assert out.host == initial.host
    assert out.port == initial.port
    assert [(s.network, s.station, s.location, s.channel) for s in out.selectors] == [
        (s.network, s.station, s.location, s.channel) for s in initial.selectors
    ]


def test_set_device_channels_auto_derives_over_default_placeholder(qtbot) -> None:
    """A fresh add-form holds only the ``*.*.*`` placeholder; loading the
    device's StationXML channels REPLACES it with the exact NSLCs so the saved
    device is never left without concrete, streaming selectors (Bug 1+3)."""
    form = DeviceForm(existing_names=())
    qtbot.addWidget(form)
    form.set_device_channels(("XX.ECHOS.00.HHZ", "XX.ECHOS.00.HHN", "XX.ECHOS.00.HHE"))
    derived = [(s.network, s.station, s.location, s.channel) for s in form._read_selectors()]
    assert derived == [
        ("XX", "ECHOS", "00", "HHZ"),
        ("XX", "ECHOS", "00", "HHN"),
        ("XX", "ECHOS", "00", "HHE"),
    ]
    assert "auto-filled" in form._stationxml_status.text().lower()


def test_set_device_channels_keeps_user_customised_selectors(qtbot) -> None:
    """Auto-derivation must never clobber selectors the user actually set."""
    form = DeviceForm(
        initial=DeviceConfig(
            name="d", host="h", selectors=[StreamSelectorConfig(network="IU", station="ANMO")]
        )
    )
    qtbot.addWidget(form)
    form.set_device_channels(("XX.ECHOS.00.HHZ",))
    assert [(s.network, s.station) for s in form._read_selectors()] == [("IU", "ANMO")]
    assert "available" in form._stationxml_status.text().lower()


def test_set_device_channels_unavailable_status(qtbot) -> None:
    """No channels (StationXML unavailable) → an honest status, no auto-fill."""
    form = DeviceForm(existing_names=())
    qtbot.addWidget(form)
    form.set_device_channels(())
    assert "unavailable" in form._stationxml_status.text().lower()


def test_response_metadata_round_trips(qtbot) -> None:
    """An initial device's response_metadata prefills and reads back."""
    xml = get_example_file("IU_ANMO_00_BHZ.xml")
    initial = DeviceConfig(
        name="anmo",
        host="h",
        response_metadata=ResponseMetadataConfig(path=xml, format="stationxml"),
    )
    form = DeviceForm(initial=initial)
    qtbot.addWidget(form)
    assert form._response_path_edit.text() == str(xml)
    out = form.to_config()
    assert out.response_metadata.path is not None
    assert str(out.response_metadata.path) == str(xml)
    assert out.response_metadata.format == "stationxml"


def test_response_metadata_blank_is_counts_only(qtbot) -> None:
    """A blank response path yields the default (no metadata)."""
    form = DeviceForm()
    qtbot.addWidget(form)
    form._name_edit.setText("dev")
    form._host_edit.setText("h")
    out = form.to_config()
    assert out.response_metadata.path is None


def test_response_metadata_valid_file_passes_validation(qtbot) -> None:
    """A real StationXML file validates cleanly at save time."""
    form = DeviceForm()
    qtbot.addWidget(form)
    form._response_path_edit.setText(get_example_file("IU_ANMO_00_BHZ.xml"))
    assert form.validate_response_metadata() is None


def test_response_metadata_bad_file_fails_validation(qtbot, tmp_path) -> None:
    """A non-inventory file yields a human-readable validation error."""
    bad = tmp_path / "nope.xml"
    bad.write_text("not an inventory", encoding="utf-8")
    form = DeviceForm()
    qtbot.addWidget(form)
    form._response_path_edit.setText(str(bad))
    err = form.validate_response_metadata()
    assert err is not None and str(bad) in err


def test_invalid_port_outside_range_marks_invalid(qtbot) -> None:
    """The QSpinBox enforces 1..65535 by clamping, so we drive the
    invalid case via the spinbox's API and assert the form's logic."""
    form = DeviceForm()
    qtbot.addWidget(form)
    form._name_edit.setText("iris")
    form._host_edit.setText("any-host")
    # Drive an out-of-range value programmatically. QSpinBox clamps
    # programmatic ``setValue`` to the configured range, so a port
    # of 0 lands at the spinbox minimum (1). We instead drop the
    # range to 0..0 first so the spinbox can accept it, then assert
    # the form stays valid (port 0 IS still rejected by pydantic
    # via ``Field(ge=1)``).
    form._port_spin.setRange(0, 65535)
    form._port_spin.setValue(0)
    # Re-run the validation pipeline (the range change does not by
    # itself fire valueChanged).
    form._revalidate()
    assert form.is_valid() is False
    err = form.validation_error()
    assert err is not None
    assert "port" in err.lower()


# ----------------------------------------------------------------------
# DeviceDialog tests
# ----------------------------------------------------------------------


def _click_ok(dialog: DeviceDialog) -> None:
    """Click the dialog's OK button via the QDialogButtonBox API."""
    button = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert button is not None
    button.click()


def test_dialog_accept_calls_store_add_device(qtbot) -> None:
    """A complete form + OK click invokes ``store.add_device`` once."""
    store = StubConfigStore()
    form = DeviceForm()
    qtbot.addWidget(form)
    form._name_edit.setText("iris")
    form._host_edit.setText("rtserve.iris.washington.edu")
    dialog = DeviceDialog(
        title="Add device",
        store=store,  # type: ignore[arg-type]
        form=form,
        on_accept=store.add_device,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    _click_ok(dialog)
    qtbot.wait(20)
    assert len(store.add_calls) == 1
    assert store.add_calls[0].name == "iris"
    assert dialog.result() == int(QDialog.DialogCode.Accepted)


def test_dialog_accept_keeps_dialog_open_on_config_error(
    qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ConfigError from the store keeps the dialog visible.

    ``QMessageBox.critical`` would block the test thread modally, so we
    monkeypatch it to a no-op that records the call. The dialog's
    contract: rejection happens only on explicit Cancel; an aborted
    accept path leaves ``result()`` at the default (Rejected) but the
    dialog is still visible.
    """
    store = StubConfigStore()
    store.raise_on_add = ConfigError("simulated validation failure")
    seen: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: seen.append(args[2] if len(args) > 2 else ""),
    )
    form = DeviceForm()
    qtbot.addWidget(form)
    form._name_edit.setText("iris")
    form._host_edit.setText("any-host")
    dialog = DeviceDialog(
        title="Add device",
        store=store,  # type: ignore[arg-type]
        form=form,
        on_accept=store.add_device,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    _click_ok(dialog)
    qtbot.wait(20)
    assert len(store.add_calls) == 1
    # Dialog stayed open: the rejection only happens on Cancel; an
    # aborted accept leaves the dialog ``isVisible() is True``.
    assert dialog.isVisible() is True
    assert any("simulated" in s for s in seen)


def test_dialog_ok_button_disabled_when_form_invalid(qtbot) -> None:
    """OK is disabled while the form is invalid; enables on validity."""
    store = StubConfigStore()
    form = DeviceForm()
    qtbot.addWidget(form)
    dialog = DeviceDialog(
        title="Add device",
        store=store,  # type: ignore[arg-type]
        form=form,
        on_accept=store.add_device,
    )
    qtbot.addWidget(dialog)
    ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok is not None
    # Initial: name and host empty -> invalid.
    assert ok.isEnabled() is False
    # Type valid values: button enables.
    form._name_edit.setText("iris")
    form._host_edit.setText("any-host")
    qtbot.wait(20)
    assert ok.isEnabled() is True


# M6 stage 3: the chain edit button is disabled when no engine is
# supplied, enabled when one is. Without an engine the live preview
# could not draw anything, so launching the editor would mislead
# users.
def test_chain_edit_button_disabled_without_engine(qtbot) -> None:
    form = DeviceForm()
    qtbot.addWidget(form)
    assert form._chain_edit_button.isEnabled() is False
    tip = form._chain_edit_button.toolTip()
    assert "engine" in tip.lower() or "preview" in tip.lower()


def test_chain_edit_button_enabled_with_engine(qtbot, monkeypatch) -> None:
    """Construct the form with a stub engine and verify the button
    becomes clickable. The click handler launches ChainEditorDialog —
    monkeypatch its exec to a no-op so the test does not block."""
    from echosmonitor.gui.dialogs.chain_editor_dialog import ChainEditorDialog

    class _StubEngine:
        def __init__(self) -> None:
            self._buffers: dict[str, object] = {}

        def read_recent(self, *_args, **_kwargs):
            import numpy as _np

            return _np.empty(0, dtype=_np.float32), 0.0, None

    form = DeviceForm(engine=_StubEngine())  # type: ignore[arg-type]
    qtbot.addWidget(form)
    assert form._chain_edit_button.isEnabled() is True

    exec_calls: list[ChainEditorDialog] = []
    monkeypatch.setattr(
        ChainEditorDialog,
        "exec",
        lambda self: exec_calls.append(self) or int(self.DialogCode.Rejected),
    )
    form._on_edit_chain_clicked()
    assert len(exec_calls) == 1


# ----------------------------------------------------------------------
# NSLC-collision informational banner
# ----------------------------------------------------------------------


def _filled_form(form: DeviceForm, name: str, sel: StreamSelectorConfig) -> None:
    """Type ``name`` + a single selector into a freshly-built form.

    Offscreen Qt can't deliver real keystrokes, so we set the field text
    programmatically and replace the seeded wildcard selector row, then
    drive the same revalidate entry point per-keystroke editing would.
    """
    form._name_edit.setText(name)
    form._host_edit.setText("example.org")
    # Replace the default wildcard row with the concrete selector.
    tree = form._selector_tree
    while tree.topLevelItemCount():
        tree.takeTopLevelItem(0)
    form._append_selector_row(sel.network, sel.station, sel.location, sel.channel)
    form._revalidate()


def test_collision_banner_visible_and_save_still_enabled(qtbot) -> None:
    """A draft sharing an NSLC with another device shows the banner AND
    keeps the form valid — the warning is informational, never blocking."""
    sel = StreamSelectorConfig(network="XX", station="ECHOS", location="00", channel="HHZ")
    other = DeviceConfig(name="Echos", host="h", selectors=[sel])
    form = DeviceForm(existing_devices=[other])
    qtbot.addWidget(form)
    _filled_form(form, "Echos_WK", sel)

    # Observable 1: the banner is visible and names the other device + NSLC.
    # ``isVisibleTo(form)`` reports the widget's own visibility flag without
    # requiring the (never-shown) top-level window to be mapped — the real
    # observable here is "the banner would be visible", not "the window is up".
    assert form._collision_label.isVisibleTo(form) is True
    text = form._collision_label.text()
    assert "Echos" in text
    assert "XX.ECHOS.00.HHZ" in text
    # Observable 2 (CRITICAL): the warning must not block save.
    assert form.is_valid() is True

    dialog = DeviceDialog(
        title="Add",
        store=StubConfigStore(),
        form=form,
        on_accept=lambda _cfg: None,
    )
    qtbot.addWidget(dialog)
    ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok is not None
    assert ok.isEnabled() is True


def test_collision_banner_hidden_for_distinct_station(qtbot) -> None:
    """No overlap (different concrete station) → the banner stays hidden."""
    other = DeviceConfig(
        name="Other",
        host="h",
        selectors=[
            StreamSelectorConfig(network="YY", station="WXYZ", location="00", channel="BHZ")
        ],
    )
    form = DeviceForm(existing_devices=[other])
    qtbot.addWidget(form)
    _filled_form(
        form,
        "Echos_WK",
        StreamSelectorConfig(network="XX", station="ECHOS", location="00", channel="HHZ"),
    )
    assert form._collision_label.isVisibleTo(form) is False
    assert form.is_valid() is True


def test_collision_banner_no_self_collision_when_editing(qtbot) -> None:
    """Editing a device against its OWN entry in existing_devices must not
    fire a self-collision banner."""
    sel = StreamSelectorConfig(network="XX", station="ECHOS", location="00", channel="HHZ")
    me = DeviceConfig(name="Echos", host="h", selectors=[sel])
    form = DeviceForm(
        initial=me,
        existing_devices=[me],
        editing_name="Echos",
    )
    qtbot.addWidget(form)
    # The prefill + initial revalidate already ran; assert no banner.
    assert form._collision_label.isVisibleTo(form) is False
    assert form.is_valid() is True


# Suppress unused-warning helpers — these imports are kept above for
# readability of the test file but Qt's type checking is satisfied.
_unused: tuple[Any, ...] = (Qt,)
