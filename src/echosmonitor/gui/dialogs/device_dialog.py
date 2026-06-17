"""Add / edit / remove device modal dialogs (M4 stage B).

The :class:`DeviceForm` is the inner reusable form; it is also embedded
in the first-run wizard (Stage C) so its public API takes ``initial``
and ``existing_names`` parameters and emits :attr:`isValid` on every
field change. It does not own the OK/Cancel chrome — that is the
:class:`DeviceDialog` wrapper's job.

The DSP chain section is intentionally read-only in Stage B (M5 will
make it editable). The Archive section is not exposed at all yet (M6).
A permanently-disabled "Edit chain..." button + a permanently-disabled
"Also delete archived data" checkbox set the right expectations now so
the future enhancements land without UI re-shuffling.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import (
    QRegularExpression,
    Qt,
    QThread,
    QTimer,
    Signal,
    SignalInstance,
    Slot,
)
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.config.credentials import CredentialsStore
from echosmonitor.config.schema import (
    DeviceConfig,
    EchosDeviceConfig,
    PositionOverride,
    ReconnectConfig,
    ResponseMetadataConfig,
    StreamSelectorConfig,
)
from echosmonitor.core.collisions import NslcCollision, find_nslc_collisions
from echosmonitor.core.echos_device_worker import EchosDeviceState, EchosDeviceWorker
from echosmonitor.core.exceptions import ConfigError, ResponseError
from echosmonitor.core.models import EchosPollTarget
from echosmonitor.gui.dialogs.echos_tabs import (
    AcquisitionTab,
    MaintenanceTab,
    NetworkTab,
    SeedlinkTab,
)

if TYPE_CHECKING:
    from echosmonitor.core.config_store import ConfigStore
    from echosmonitor.core.echos_api import EchosApiClient
    from echosmonitor.core.streaming_engine import StreamingEngine

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants — extracted so widget tweaks live in one place.
# ---------------------------------------------------------------------------

# Schema-level bounds duplicated here as widget bounds. Keeping them in
# sync with ``ReconnectConfig.connect_timeout_s`` and ``DeviceConfig.port``
# is a one-line change in two places, less risky than dynamically pulling
# them from the pydantic model at construction time (which would couple
# widget construction to an internal pydantic API).
_PORT_MIN = 1
_PORT_MAX = 65535
_PORT_DEFAULT = 18000
_TIMEOUT_MIN_S = 0.5
_TIMEOUT_MAX_S = 300.0
_TIMEOUT_DEFAULT_S = 10.0
_TIMEOUT_DECIMALS = 1
_TIMEOUT_STEP_S = 0.5

# Debounce for the StationXML auto-fetch after host edits (Bug 1+3): long
# enough that typing a hostname does not fire a request per keystroke.
_STATIONXML_DEBOUNCE_MS = 600

# Device-name allowed characters. Mirrors the ``[A-Za-z0-9_.-]+`` set
# the rest of the codebase uses for path-safe device identifiers (the
# YAML serialiser, the QSettings keys under ``StationBrowser/lastDeviceId``,
# the SDS root layout in M6). The validator rejects spaces and ``/`` so
# the user cannot accidentally introduce a key that collides with the
# composite ``f"{device}/{nslc}"`` form the engine uses internally.
_NAME_PATTERN = r"[A-Za-z0-9_.\-]+"

# Selector-tree column indices.
_SEL_COL_NET = 0
_SEL_COL_STA = 1
_SEL_COL_LOC = 2
_SEL_COL_CHA = 3
_SEL_HEADERS = ("NET", "STA", "LOC", "CHA")

# Default cells for a freshly-added selector row. Matches the schema's
# wildcard defaults except for ``location`` which is empty by SeedLink
# convention (most networks publish at "" for primary location).
_DEFAULT_SELECTOR_CELLS = ("*", "*", "", "*")

# Tooltip texts for the chain/remove-archive controls. M6 stage 3
# enables the chain editor; the button is disabled (with a hint
# tooltip) until the caller supplies an ``engine`` so the live preview
# has data to draw on.
_CHAIN_EDIT_DISABLED_TOOLTIP = (
    "Live engine reference required for the chain editor's preview. "
    "Open the dialog via the Devices panel or the File menu."
)
_REMOVE_ARCHIVE_TOOLTIP = "Archive deletion arrives in M6."

# Response-metadata (M11) controls. The format combo mirrors
# ``ResponseMetadataConfig.format``; ``auto`` lets ObsPy sniff the format.
_RESPONSE_FORMATS: tuple[tuple[str, str], ...] = (
    ("Auto-detect", "auto"),
    ("StationXML", "stationxml"),
    ("dataless SEED", "dataless"),
    ("RESP", "resp"),
)
# File-picker filter covering the three supported metadata formats.
_RESPONSE_FILE_FILTER = (
    "Response metadata (*.xml *.dataless *.seed RESP* *.resp);;StationXML (*.xml);;"
    "dataless SEED (*.dataless *.seed);;RESP (RESP* *.resp);;All files (*)"
)
_RESPONSE_TOOLTIP = (
    "Optional StationXML / dataless SEED / RESP file. Enables physical-unit "
    "display (velocity / acceleration / displacement) on fixed windows for "
    "this device. Blank = counts only."
)

# NSLC-collision warning banner (informational only — never blocks save).
# Amber text on a faint amber background so it reads as a non-fatal notice,
# visually distinct from the red ``QMessageBox.critical`` used for hard
# validation/save failures.
_COLLISION_BANNER_STYLE = (
    "QLabel { color: #b06800; background: #fff4e0; "
    "border: 1px solid #e0a85c; border-radius: 3px; padding: 4px 6px; }"
)

# M1-D: bounded join for the per-dialog Echos REST worker thread on
# dialog close (rule 7; matches the app-wide 2 s worker budgets).
_ECHOS_WORKER_JOIN_MS = 2000

# Lockout banner (rule 15: surface the 429 window honestly, never hammer).
_LOCKOUT_BANNER_STYLE = (
    "QLabel { color: #8a1f11; background: #fdecea; "
    "border: 1px solid #e0a8a0; border-radius: 3px; padding: 4px 6px; }"
)

# Index of the Connection tab (the client-side form). Server tabs follow.
_TAB_CONNECTION = 0


def _selector_summary(stages: Iterable[object]) -> str:
    """Render a human-readable one-liner for the DSP chain summary label.

    Empty chain renders as "no chain" so the placeholder reads naturally.
    Non-empty chains render as "<count> stages: <type1>, <type2>, ..." so
    the user can see at a glance what's installed without opening the
    (disabled) editor.
    """
    types = [getattr(s, "type", "?") for s in stages]
    if not types:
        return "no chain"
    return f"{len(types)} stage(s): {', '.join(types)}"


class DeviceForm(QWidget):
    """Reusable form for adding or editing a :class:`DeviceConfig`.

    Layout (top-to-bottom):

    * Name (QLineEdit + regex validator)
    * Host (QLineEdit, non-empty)
    * Port (QSpinBox 1..65535, default 18000)
    * Connect timeout (QDoubleSpinBox 0.5..300.0 s)
    * Selectors (QTreeWidget with NET/STA/LOC/CHA editable cells +
      add/remove buttons)
    * DSP chain summary label + disabled "Edit chain..." button

    Validation runs after every field change. The form keeps a cached
    "is currently valid" boolean and emits :attr:`isValid` whenever the
    cached value transitions. Callers (notably :class:`DeviceDialog`)
    use the signal to enable/disable their OK button.
    """

    # Fires on every field change — the boolean payload is the current
    # validity. Wired by :class:`DeviceDialog` to its OK button's
    # ``setEnabled``. Unconditional emission (rather than only on
    # transitions) makes the wiring trivial: the dialog never has to
    # query ``is_valid()`` before doing anything.
    isValid = Signal(bool)  # noqa: N815
    # M1-D: the Echos group checkbox toggled — the dialog enables /
    # disables the server-side tabs accordingly.
    echosEnabledChanged = Signal(bool)  # noqa: N815
    # M1-D: "Store password" clicked — (device_key, password). Routed to
    # the worker because keyring access can block (rule 1 / rule 15);
    # the password never lands in the config.
    credentialStoreRequested = Signal(str, str)  # noqa: N815
    # M1-D: host or HTTP port edited — any loaded server-side state now
    # belongs to a DIFFERENT device; the dialog drops the read-modify-
    # write baselines on this signal (review finding).
    connectionTargetChanged = Signal()  # noqa: N815

    def __init__(
        self,
        *,
        initial: DeviceConfig | None = None,
        existing_names: Iterable[str] = (),
        existing_devices: Iterable[DeviceConfig] = (),
        editing_name: str | None = None,
        engine: StreamingEngine | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Construct the form.

        Args:
            initial: Prefill values. ``None`` produces a blank form
                with the spinboxes at their defaults.
            existing_names: Device names already in the config, for
                duplicate-name checking. The check excludes
                ``editing_name`` so renaming a device "to itself"
                stays valid.
            existing_devices: Full configs of the OTHER devices already
                in the config, used for the informational NSLC-collision
                notice. Defaulted to empty so callers that don't pass it
                simply get no collision check (the banner stays hidden).
                The notice never blocks save — per-device SDS namespacing
                already makes a same-NSLC situation safe; we only inform.
            editing_name: When editing an existing device, the device's
                CURRENT name (before any pending edits). The
                duplicate-name check excludes this name from
                ``existing_names`` so the user can keep the name as-is.
                ``None`` for the add-device flow.
            engine: Live :class:`StreamingEngine` reference used by
                the DSP chain editor's preview. When ``None`` the
                Edit-chain button stays disabled with an explanatory
                tooltip; the chain itself is still editable through
                YAML.
            parent: Standard Qt parent.
        """
        super().__init__(parent)
        self._existing_names = set(existing_names)
        self._existing_devices = list(existing_devices)
        self._editing_name = editing_name
        self._initial = initial
        self._engine = engine
        self._validation_error: str | None = None
        self._is_valid_cached: bool = False

        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Name --------------------------------------------------------
        self._name_edit = QLineEdit(self)
        self._name_edit.setPlaceholderText("e.g. iris-iu-anmo")
        regex = QRegularExpression(_NAME_PATTERN)
        self._name_edit.setValidator(QRegularExpressionValidator(regex, self))
        form.addRow("Name:", self._name_edit)

        # NSLC-collision notice — informational only. Hidden until
        # ``_update_collision_notice`` finds an overlap with another
        # configured device. Word-wrapped so long device/NSLC lists don't
        # force the dialog wide. Spans the full form width (no label cell).
        self._collision_label = QLabel(self)
        self._collision_label.setWordWrap(True)
        self._collision_label.setStyleSheet(_COLLISION_BANNER_STYLE)
        self._collision_label.setVisible(False)
        form.addRow(self._collision_label)

        # Host --------------------------------------------------------
        self._host_edit = QLineEdit(self)
        self._host_edit.setPlaceholderText("e.g. rtserve.iris.washington.edu")
        form.addRow("Host:", self._host_edit)

        # Port --------------------------------------------------------
        self._port_spin = QSpinBox(self)
        self._port_spin.setRange(_PORT_MIN, _PORT_MAX)
        self._port_spin.setValue(_PORT_DEFAULT)
        form.addRow("Port:", self._port_spin)

        # Connect timeout --------------------------------------------
        self._timeout_spin = QDoubleSpinBox(self)
        self._timeout_spin.setRange(_TIMEOUT_MIN_S, _TIMEOUT_MAX_S)
        self._timeout_spin.setDecimals(_TIMEOUT_DECIMALS)
        self._timeout_spin.setSingleStep(_TIMEOUT_STEP_S)
        self._timeout_spin.setValue(_TIMEOUT_DEFAULT_S)
        self._timeout_spin.setSuffix(" s")
        form.addRow("Connect timeout:", self._timeout_spin)

        # Selectors --------------------------------------------------
        selector_box = QWidget(self)
        selector_layout = QVBoxLayout(selector_box)
        selector_layout.setContentsMargins(0, 0, 0, 0)
        self._selector_tree = QTreeWidget(selector_box)
        self._selector_tree.setColumnCount(len(_SEL_HEADERS))
        self._selector_tree.setHeaderLabels(list(_SEL_HEADERS))
        self._selector_tree.setRootIsDecorated(False)
        self._selector_tree.setUniformRowHeights(True)
        self._selector_tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed,
        )
        sel_header = self._selector_tree.header()
        for col in range(len(_SEL_HEADERS)):
            sel_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        selector_layout.addWidget(self._selector_tree)

        sel_buttons = QHBoxLayout()
        self._add_row_button = QPushButton("+ Add row", selector_box)
        self._remove_row_button = QPushButton("- Remove selected", selector_box)
        sel_buttons.addWidget(self._add_row_button)
        sel_buttons.addWidget(self._remove_row_button)
        # M1-D: replace the selector rows with the exact NSLCs the device
        # advertises in its StationXML. Enabled once a device load has
        # delivered channels (``set_device_channels``).
        self._use_channels_button = QPushButton("Use device channels", selector_box)
        self._use_channels_button.setEnabled(False)
        self._use_channels_button.setToolTip(
            "Load the device (Echos tabs) to derive selectors from its StationXML."
        )
        self._device_channels: tuple[str, ...] = ()
        sel_buttons.addWidget(self._use_channels_button)
        sel_buttons.addStretch(1)
        selector_layout.addLayout(sel_buttons)
        # Status of the device's StationXML auto-fetch (M7.1 / Bug 1+3): tells
        # the user the device metadata WAS downloaded (so "Response metadata"
        # reading "(none)" is not mistaken for "nothing was fetched"), and how
        # many selectors were auto-derived from it.
        self._stationxml_status = QLabel("", selector_box)
        self._stationxml_status.setStyleSheet("QLabel { color: #888; }")
        self._stationxml_status.setWordWrap(True)
        selector_layout.addWidget(self._stationxml_status)
        form.addRow("Selectors:", selector_box)

        # DSP chain summary + disabled Edit chain button -------------
        chain_box = QWidget(self)
        chain_layout = QHBoxLayout(chain_box)
        chain_layout.setContentsMargins(0, 0, 0, 0)
        self._chain_summary_label = QLabel("no chain", chain_box)
        self._chain_summary_label.setStyleSheet("QLabel { color: #888; }")
        chain_layout.addWidget(self._chain_summary_label, 1)
        self._chain_edit_button = QPushButton("Edit chain...", chain_box)
        if self._engine is not None:
            self._chain_edit_button.setEnabled(True)
            self._chain_edit_button.setToolTip(
                "Edit the DSP chain with a live preview of the filtered signal."
            )
            self._chain_edit_button.clicked.connect(self._on_edit_chain_clicked)
        else:
            self._chain_edit_button.setEnabled(False)
            self._chain_edit_button.setToolTip(_CHAIN_EDIT_DISABLED_TOOLTIP)
        chain_layout.addWidget(self._chain_edit_button)
        form.addRow("DSP chain:", chain_box)

        # Response metadata (M11) ------------------------------------
        response_box = QWidget(self)
        response_layout = QHBoxLayout(response_box)
        response_layout.setContentsMargins(0, 0, 0, 0)
        self._response_path_edit = QLineEdit(response_box)
        self._response_path_edit.setPlaceholderText("(none — counts only)")
        self._response_path_edit.setToolTip(_RESPONSE_TOOLTIP)
        self._response_format_combo = QComboBox(response_box)
        for label, code in _RESPONSE_FORMATS:
            self._response_format_combo.addItem(label, userData=code)
        self._response_browse_button = QPushButton("Browse…", response_box)
        self._response_browse_button.clicked.connect(self._on_browse_response)
        response_layout.addWidget(self._response_path_edit, 1)
        response_layout.addWidget(self._response_format_combo)
        response_layout.addWidget(self._response_browse_button)
        form.addRow("Response metadata:", response_box)

        # Echos client-side settings (M1-D; rule 15: only what the app
        # needs to REACH the device — server-side config lives on the
        # device, edited via the server tabs; the admin password lives
        # in the credentials store, never in this form's config).
        self._echos_group = QGroupBox("Echos device (REST management)", self)
        self._echos_group.setCheckable(True)
        self._echos_group.setChecked(False)
        echos_form = QFormLayout(self._echos_group)
        self._http_port_spin = QSpinBox(self._echos_group)
        self._http_port_spin.setRange(1, 65535)
        self._http_port_spin.setValue(80)
        echos_form.addRow("HTTP port:", self._http_port_spin)
        self._poll_interval_spin = QDoubleSpinBox(self._echos_group)
        self._poll_interval_spin.setRange(1.0, 3600.0)
        self._poll_interval_spin.setDecimals(1)
        self._poll_interval_spin.setValue(5.0)
        self._poll_interval_spin.setSuffix(" s")
        echos_form.addRow("Status poll interval:", self._poll_interval_spin)
        # Manual position override (rule 16): wins over StationXML.
        self._pos_check = QCheckBox("Manual position override", self._echos_group)
        echos_form.addRow("", self._pos_check)
        pos_box = QWidget(self._echos_group)
        pos_layout = QHBoxLayout(pos_box)
        pos_layout.setContentsMargins(0, 0, 0, 0)
        self._lat_spin = QDoubleSpinBox(pos_box)
        self._lat_spin.setRange(-90.0, 90.0)
        self._lat_spin.setDecimals(6)
        self._lat_spin.setPrefix("lat ")
        self._lon_spin = QDoubleSpinBox(pos_box)
        self._lon_spin.setRange(-180.0, 180.0)
        self._lon_spin.setDecimals(6)
        self._lon_spin.setPrefix("lon ")
        self._elev_spin = QDoubleSpinBox(pos_box)
        self._elev_spin.setRange(-500.0, 9000.0)
        self._elev_spin.setDecimals(1)
        self._elev_spin.setPrefix("elev ")
        self._elev_spin.setSuffix(" m")
        for spin in (self._lat_spin, self._lon_spin, self._elev_spin):
            pos_layout.addWidget(spin)
        echos_form.addRow("Position:", pos_box)
        # Admin password: stored via the CredentialsStore (keyring/file),
        # NEVER serialised with the config (rule 15). The field is
        # write-only; presence is reported via the status label. The whole
        # row is HIDDEN until a host wires ``credentialStoreRequested`` to
        # a real sink and calls :meth:`enable_credential_store` — the
        # first-run wizard embeds this form without a worker, and a Store
        # button that silently does nothing would be a rule-15 honesty
        # failure (review finding).
        self._credential_box = QWidget(self._echos_group)
        cred_form = QFormLayout(self._credential_box)
        cred_form.setContentsMargins(0, 0, 0, 0)
        pw_box = QWidget(self._credential_box)
        pw_layout = QHBoxLayout(pw_box)
        pw_layout.setContentsMargins(0, 0, 0, 0)
        self._admin_password_edit = QLineEdit(pw_box)
        self._admin_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._admin_password_edit.setPlaceholderText("(stored in keyring — never in the YAML)")
        self._store_password_button = QPushButton("Store", pw_box)
        pw_layout.addWidget(self._admin_password_edit, 1)
        pw_layout.addWidget(self._store_password_button)
        cred_form.addRow("Admin password:", pw_box)
        self._credential_status_label = QLabel("", self._credential_box)
        self._credential_status_label.setStyleSheet("QLabel { color: #888; }")
        cred_form.addRow("", self._credential_status_label)
        self._credential_box.setVisible(False)
        echos_form.addRow(self._credential_box)
        form.addRow(self._echos_group)

        # Internal state for the chain. Stage B reads it back unchanged
        # from `to_config`; M5 will make this list mutable through the
        # (currently disabled) editor button.
        self._chain_stages: list[object] = list(initial.dsp_chain) if initial is not None else []
        self._chain_summary_label.setText(_selector_summary(self._chain_stages))

        # Wiring -----------------------------------------------------
        self._name_edit.textChanged.connect(self._on_field_changed)
        self._host_edit.textChanged.connect(self._on_field_changed)
        self._port_spin.valueChanged.connect(self._on_field_changed)
        self._timeout_spin.valueChanged.connect(self._on_field_changed)
        self._selector_tree.itemChanged.connect(self._on_field_changed)
        self._response_path_edit.textChanged.connect(self._on_field_changed)
        self._add_row_button.clicked.connect(self._on_add_row)
        self._remove_row_button.clicked.connect(self._on_remove_row)
        self._use_channels_button.clicked.connect(self._on_use_device_channels)
        self._echos_group.toggled.connect(self._on_echos_toggled)
        self._pos_check.toggled.connect(self._on_pos_override_toggled)
        self._http_port_spin.valueChanged.connect(self._on_field_changed)
        self._poll_interval_spin.valueChanged.connect(self._on_field_changed)
        self._host_edit.textChanged.connect(lambda _text: self.connectionTargetChanged.emit())
        self._http_port_spin.valueChanged.connect(
            lambda _value: self.connectionTargetChanged.emit()
        )
        for spin in (self._lat_spin, self._lon_spin, self._elev_spin):
            spin.valueChanged.connect(self._on_field_changed)
        self._store_password_button.clicked.connect(self._on_store_password)
        self._on_pos_override_toggled(self._pos_check.isChecked())

        # Prefill -----------------------------------------------------
        if initial is not None:
            self._apply_initial(initial)
        else:
            # Seed the selectors with one wildcard row so the user has
            # somewhere to click — simpler UX than starting empty and
            # hitting the (currently empty) "remove" branch.
            self._append_selector_row(*_DEFAULT_SELECTOR_CELLS)

        # Initial validation pass so `is_valid()` is accurate before
        # the user types anything (notably for `edit` flows where the
        # form is valid out of the gate).
        self._revalidate()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def is_valid(self) -> bool:
        """Return whether the form's current values would build a valid
        :class:`DeviceConfig` AND not collide with another device's name."""
        return self._is_valid_cached

    def validation_error(self) -> str | None:
        """Human-readable description of the current validation failure.

        Returns ``None`` when :meth:`is_valid` is ``True``. The same
        string is what :meth:`to_config` includes in the ``ValueError``
        it raises if called while invalid.
        """
        return self._validation_error

    def to_config(self) -> DeviceConfig:
        """Build a :class:`DeviceConfig` from the current field values.

        Raises:
            ValueError: The form is not currently valid. Callers should
                check :meth:`is_valid` first; the dialog wrapper does.
        """
        if not self._is_valid_cached:
            raise ValueError(self._validation_error or "form is invalid")
        # Re-build via the public DeviceConfig constructor so the same
        # validators that would run on a YAML round-trip run here. The
        # internally-cached chain stages are already typed (we only
        # ever store DspStageConfig instances coming in from `initial`
        # in stage B), but pass them through `model_validate` would
        # require pydantic round-trip; the discriminated union
        # constructor accepts the BaseModel instances directly.
        return DeviceConfig(
            name=self._name_edit.text().strip(),
            host=self._host_edit.text().strip(),
            port=int(self._port_spin.value()),
            reconnect=ReconnectConfig(
                connect_timeout_s=float(self._timeout_spin.value()),
            ),
            selectors=self._read_selectors(),
            dsp_chain=list(self._chain_stages),
            response_metadata=self._read_response_metadata(),
            echos=self._read_echos(),
        )

    def _read_echos(self) -> EchosDeviceConfig | None:
        """Build the client-side ``echos:`` section (rule 15: no secrets)."""
        if not self._echos_group.isChecked():
            return None
        override = None
        if self._pos_check.isChecked():
            override = PositionOverride(
                lat=float(self._lat_spin.value()),
                lon=float(self._lon_spin.value()),
                elev_m=float(self._elev_spin.value()),
            )
        return EchosDeviceConfig(
            http_port=int(self._http_port_spin.value()),
            poll_interval_s=float(self._poll_interval_spin.value()),
            position_override=override,
        )

    def echos_enabled(self) -> bool:
        return self._echos_group.isChecked()

    def echos_target(self) -> EchosPollTarget | None:
        """Current REST target, or ``None`` when not an Echos device / no host.

        The target ``name`` doubles as the credentials-store key, so the
        device's CURRENT config name wins over an in-progress rename.
        """
        if not self._echos_group.isChecked():
            return None
        host = self._host_edit.text().strip()
        if not host:
            return None
        name = self._editing_name or self._name_edit.text().strip() or host
        return EchosPollTarget(
            name=name,
            host=host,
            http_port=int(self._http_port_spin.value()),
            poll_interval_s=float(self._poll_interval_spin.value()),
        )

    def set_device_channels(self, channels: tuple[str, ...]) -> None:
        """Record the device's StationXML NSLCs and surface them.

        Enables "Use device channels", updates the StationXML status label,
        and — crucially for Bug 1 — AUTO-DERIVES the selectors when the form
        currently has none. A device saved with empty selectors connects but
        subscribes to nothing ("No streams specified") and retry-loops forever,
        so a freshly-configured device must never be left selector-less when
        the device itself advertises its channels.
        """
        self._device_channels = channels
        self._use_channels_button.setEnabled(bool(channels))
        if not channels:
            self._stationxml_status.setText("Device StationXML: unavailable")
            return
        self._use_channels_button.setText(f"Use device channels ({len(channels)})")
        if self._selectors_are_default_or_empty():
            self._on_use_device_channels()
            self._stationxml_status.setText(
                f"Device StationXML: {len(channels)} channels — selectors auto-filled."
            )
        else:
            self._stationxml_status.setText(
                f"Device StationXML: {len(channels)} channels available."
            )

    def _selectors_are_default_or_empty(self) -> bool:
        """True when the selector grid still holds only the wildcard placeholder.

        Auto-derivation replaces the default ``*.*.*`` row (or an empty grid)
        with the device's exact channels, but never clobbers selectors the user
        has actually customised.
        """
        sels = self._read_selectors()
        if not sels:
            return True
        if len(sels) == 1:
            net, sta, loc, chan = _DEFAULT_SELECTOR_CELLS
            s = sels[0]
            return (s.network, s.station, s.location, s.channel) == (net, sta, loc, chan)
        return False

    def set_credential_status(self, text: str) -> None:
        self._credential_status_label.setText(text)

    def enable_credential_store(self) -> None:
        """Show the admin-password row.

        Called by hosts that actually wire ``credentialStoreRequested``
        to a sink (the DeviceDialog's worker). Embedders that don't
        (the first-run wizard) keep the row hidden so the Store button
        can never silently drop a password.
        """
        self._credential_box.setVisible(True)

    def clear_password_field(self) -> None:
        self._admin_password_edit.clear()

    def set_seedlink_port(self, port: int) -> None:
        """Sync the client-side SeedLink port after a server-side change."""
        self._port_spin.setValue(int(port))

    def _read_response_metadata(self) -> ResponseMetadataConfig:
        """Build the response-metadata config from the path + format fields.

        An empty path yields the default (no metadata — counts only). The
        format combo's userData is one of the ``ResponseMetadataConfig``
        literals (``auto`` / ``stationxml`` / ``dataless`` / ``resp``).
        """
        text = self._response_path_edit.text().strip()
        if not text:
            return ResponseMetadataConfig()
        fmt = self._response_format_combo.currentData()
        fmt_str = fmt if isinstance(fmt, str) else "auto"
        return ResponseMetadataConfig(path=Path(text), format=fmt_str)

    def validate_response_metadata(self) -> str | None:
        """Try to load the configured response file as an inventory.

        Returns ``None`` when no path is set or the file loads cleanly;
        otherwise a human-readable error string. Called at save time (not
        per keystroke) so the GUI thread isn't doing file I/O on every
        edit. A relative path is resolved against the current working
        directory here; the file picker yields absolute paths, which is
        the recommended way to set it.
        """
        meta = self._read_response_metadata()
        if meta.path is None:
            return None
        from echosmonitor.core.response import load_inventory

        try:
            load_inventory(Path(meta.path), meta.format)
        except ResponseError as exc:
            return str(exc)
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _apply_initial(self, cfg: DeviceConfig) -> None:
        """Populate fields from an existing :class:`DeviceConfig`.

        Blocks itemChanged/textChanged signals during bulk fill so the
        validation pipeline only runs once at the end.
        """
        self._name_edit.blockSignals(True)
        self._host_edit.blockSignals(True)
        self._port_spin.blockSignals(True)
        self._timeout_spin.blockSignals(True)
        self._selector_tree.blockSignals(True)
        try:
            self._name_edit.setText(cfg.name)
            self._host_edit.setText(cfg.host)
            self._port_spin.setValue(int(cfg.port))
            self._timeout_spin.setValue(float(cfg.reconnect.connect_timeout_s))
            meta = cfg.response_metadata
            self._response_path_edit.setText(str(meta.path) if meta.path is not None else "")
            fmt_idx = self._response_format_combo.findData(meta.format)
            if fmt_idx >= 0:
                self._response_format_combo.setCurrentIndex(fmt_idx)
            for sel in cfg.selectors:
                self._append_selector_row(sel.network, sel.station, sel.location, sel.channel)
            if not cfg.selectors:
                # Edit-mode round-trip of a device that genuinely has
                # zero selectors should still surface one editable row
                # so the user can see / change it. Validation later
                # will accept the empty list since the schema does.
                self._append_selector_row(*_DEFAULT_SELECTOR_CELLS)
            # Echos client-side section (M1-D).
            if cfg.echos is not None:
                self._echos_group.setChecked(True)
                self._http_port_spin.setValue(int(cfg.echos.http_port))
                self._poll_interval_spin.setValue(float(cfg.echos.poll_interval_s))
                override = cfg.echos.position_override
                if override is not None:
                    self._pos_check.setChecked(True)
                    self._lat_spin.setValue(float(override.lat))
                    self._lon_spin.setValue(float(override.lon))
                    self._elev_spin.setValue(float(override.elev_m))
        finally:
            self._name_edit.blockSignals(False)
            self._host_edit.blockSignals(False)
            self._port_spin.blockSignals(False)
            self._timeout_spin.blockSignals(False)
            self._selector_tree.blockSignals(False)

    def _append_selector_row(self, network: str, station: str, location: str, channel: str) -> None:
        item = QTreeWidgetItem([network, station, location, channel])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._selector_tree.addTopLevelItem(item)

    def _read_selectors(self) -> list[StreamSelectorConfig]:
        """Read every selector row, treating empty cells as wildcards.

        Empty NET/STA/CHA become ``"*"`` to match the schema default;
        empty LOC stays empty (SeedLink convention for primary location).
        Rows where every cell collapses to the wildcard pattern AND the
        user has another non-default row are NOT filtered out — that's
        the schema's job.
        """
        out: list[StreamSelectorConfig] = []
        for i in range(self._selector_tree.topLevelItemCount()):
            item = self._selector_tree.topLevelItem(i)
            if item is None:
                continue
            net = item.text(_SEL_COL_NET).strip() or "*"
            sta = item.text(_SEL_COL_STA).strip() or "*"
            loc = item.text(_SEL_COL_LOC).strip()
            cha = item.text(_SEL_COL_CHA).strip() or "*"
            out.append(StreamSelectorConfig(network=net, station=sta, location=loc, channel=cha))
        return out

    @Slot()
    def _on_browse_response(self) -> None:
        """Open a file picker for the response-metadata file (M11).

        Seeds the dialog at the current path's directory when one is set.
        The chosen path is absolute (the recommended form — see
        :meth:`validate_response_metadata`).
        """
        current = self._response_path_edit.text().strip()
        start_dir = str(Path(current).parent) if current else ""
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Select response metadata file",
            start_dir,
            _RESPONSE_FILE_FILTER,
        )
        if path:
            self._response_path_edit.setText(path)
            self._revalidate()

    @Slot(bool)
    def _on_echos_toggled(self, checked: bool) -> None:
        self.echosEnabledChanged.emit(checked)
        self._revalidate()

    @Slot(bool)
    def _on_pos_override_toggled(self, checked: bool) -> None:
        for spin in (self._lat_spin, self._lon_spin, self._elev_spin):
            spin.setEnabled(checked)
        self._revalidate()

    @Slot()
    def _on_use_device_channels(self) -> None:
        """Replace the selector rows with the device's exact NSLCs."""
        if not self._device_channels:
            return
        self._selector_tree.clear()
        for nslc in self._device_channels:
            parts = nslc.split(".")
            if len(parts) != 4:
                continue
            self._append_selector_row(*parts)
        self._revalidate()

    @Slot()
    def _on_store_password(self) -> None:
        password = self._admin_password_edit.text()
        name = self._editing_name or self._name_edit.text().strip()
        if not name:
            self.set_credential_status("Set a device name before storing the password.")
            return
        if not password:
            self.set_credential_status("Enter the device's admin password first.")
            return
        self.credentialStoreRequested.emit(name, password)
        self.set_credential_status("Storing…")

    @Slot()
    def _on_add_row(self) -> None:
        self._append_selector_row(*_DEFAULT_SELECTOR_CELLS)
        self._revalidate()

    @Slot()
    def _on_remove_row(self) -> None:
        for item in self._selector_tree.selectedItems():
            idx = self._selector_tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self._selector_tree.takeTopLevelItem(idx)
        self._revalidate()

    @Slot()
    def _on_field_changed(self, *_args: object) -> None:
        # PySide6's signal-slot machinery passes the new value through;
        # we don't care about it — we re-read every field from scratch.
        self._revalidate()

    @Slot()
    def _on_edit_chain_clicked(self) -> None:
        """Launch the DSP chain editor with the form's working stages.

        The editor never writes to ConfigStore directly — it returns
        the edited chain to us via its Accepted code, or pushes it
        through ``stagesApplied`` on Apply. Either way the form's
        working state is the single source of truth until the user
        clicks the device dialog's OK button.
        """
        if self._engine is None:
            return
        # Local import so DeviceForm has no hard dependency on the
        # editor at module import time (the editor imports pyqtgraph
        # and the spectrogram stack).
        from echosmonitor.gui.dialogs.chain_editor_dialog import (
            ChainEditorDialog,
        )

        device_name = self._editing_name or self._name_edit.text().strip() or "new"
        editor = ChainEditorDialog(
            stages=list(self._chain_stages),  # type: ignore[arg-type]
            device_name=device_name,
            engine=self._engine,
            parent=self,
        )
        editor.stagesApplied.connect(self._on_chain_stages_applied)
        code = editor.exec()
        if code == int(editor.DialogCode.Accepted):
            self._chain_stages = list(editor.stages())
            self._chain_summary_label.setText(_selector_summary(self._chain_stages))
            self._revalidate()

    @Slot(object)
    def _on_chain_stages_applied(self, stages: object) -> None:
        # ``stages`` is typed ``object`` because it crosses a Qt
        # signal; the editor only ever emits a tuple of pydantic stage
        # models. Reject anything that doesn't carry a ``type``
        # discriminator field so a future caller wiring a different
        # payload cannot silently corrupt the form's chain state.
        if not isinstance(stages, tuple):
            return
        if not all(hasattr(s, "type") for s in stages):
            return
        self._chain_stages = list(stages)
        self._chain_summary_label.setText(_selector_summary(self._chain_stages))
        self._revalidate()

    def _revalidate(self) -> None:
        """Run the full validation pipeline and emit :attr:`isValid`.

        Pipeline steps (each early-exits on first failure):
        1. Required scalar fields non-empty (name, host).
        2. ``DeviceConfig.model_validate`` — picks up bounded-range
           violations, malformed selectors, etc.
        3. Cross-check against ``existing_names`` (excluding
           ``editing_name`` so renaming-to-self stays valid).
        """
        error: str | None = None
        name = self._name_edit.text().strip()
        host = self._host_edit.text().strip()
        if not name:
            error = "name is required"
        elif not host:
            error = "host is required"
        else:
            try:
                draft = DeviceConfig(
                    name=name,
                    host=host,
                    port=int(self._port_spin.value()),
                    reconnect=ReconnectConfig(
                        connect_timeout_s=float(self._timeout_spin.value()),
                    ),
                    selectors=self._read_selectors(),
                    dsp_chain=list(self._chain_stages),
                )
                # NOTE: response_metadata is deliberately omitted from this
                # per-keystroke draft — its file is validated only at save
                # (``validate_response_metadata``), so we never do GUI-thread
                # file I/O on every character. Do not wire it in here.
                _ = draft  # constructed for validation only
            except Exception as exc:  # pydantic ValidationError or ValueError
                error = self._humanize_validation_error(exc)
            else:
                # Name uniqueness: the form itself has no opinion on
                # whether the name exists in the config — it relies on
                # the caller to pass the relevant set.
                effective_existing = self._existing_names - (
                    {self._editing_name} if self._editing_name else set()
                )
                if name in effective_existing:
                    error = f"a device named {name!r} already exists"
        new_valid = error is None
        self._validation_error = error
        # Fire the signal even when the boolean hasn't changed — the
        # design plan calls for "fires on every field change", and the
        # extra emit is cheap (one slot, no UI repaint cost).
        self._is_valid_cached = new_valid
        self.isValid.emit(new_valid)
        # Informational NSLC-collision notice. Runs on the same cheap
        # per-keystroke path so editing the name or selectors live-updates
        # the banner. It deliberately does NO file I/O (no response-metadata
        # checks) and NEVER touches ``new_valid`` — a collision must not
        # block save.
        self._update_collision_notice()

    def _draft_config(self) -> DeviceConfig | None:
        """Build a best-effort :class:`DeviceConfig` from the live fields.

        Returns ``None`` when the current fields can't form a valid config
        yet (e.g. blank name) — the collision notice simply hides in that
        case. Mirrors the per-keystroke draft built in :meth:`_revalidate`
        (name / host / port / reconnect / selectors / dsp_chain), without
        the ``response_metadata`` file path (kept off the cheap path).
        """
        name = self._name_edit.text().strip()
        host = self._host_edit.text().strip()
        if not name or not host:
            return None
        try:
            return DeviceConfig(
                name=name,
                host=host,
                port=int(self._port_spin.value()),
                reconnect=ReconnectConfig(
                    connect_timeout_s=float(self._timeout_spin.value()),
                ),
                selectors=self._read_selectors(),
                dsp_chain=list(self._chain_stages),
            )
        except Exception:
            return None

    def _update_collision_notice(self) -> None:
        """Refresh the informational NSLC-collision banner.

        Assembles the draft config plus every OTHER configured device
        (excluding the one being edited and any same-named entry), runs
        :func:`find_nslc_collisions`, and surfaces a non-blocking amber
        banner naming the colliding device(s) and shared NSLC(s). Hides
        the banner when there is no overlap. Never affects validity.
        """
        draft = self._draft_config()
        if draft is None:
            self._collision_label.setVisible(False)
            return
        # Compare the draft against the other devices, excluding the
        # device being edited (so it can't "collide with itself") and any
        # entry that shares the draft's current name (the rename case).
        others = [
            d
            for d in self._existing_devices
            if d.name != draft.name and d.name != self._editing_name
        ]
        collisions = find_nslc_collisions([draft, *others])
        relevant = [c for c in collisions if draft.name in c.devices]
        if not relevant:
            self._collision_label.setVisible(False)
            return
        self._collision_label.setText(self._format_collision_notice(draft.name, relevant))
        self._collision_label.setVisible(True)

    @staticmethod
    def _format_collision_notice(this_name: str, collisions: list[NslcCollision]) -> str:
        """Render the warning text for one or more shared NSLCs.

        Single collision reads as a sentence; multiple are summarised as a
        bulleted-style list so the operator sees every overlap at a glance.
        """
        suffix = (
            "Archived data is now separated by device, but identical NSLCs may "
            "be confusing — consider distinct station codes."
        )
        if len(collisions) == 1:
            c = collisions[0]
            others = [n for n in c.devices if n != this_name]
            other_str = ", ".join(f"'{n}'" for n in others) or "another device"
            return f"⚠ '{this_name}' and {other_str} both produce {c.nslc}. {suffix}"
        lines = [f"⚠ '{this_name}' shares NSLCs with other devices:"]
        for c in collisions:
            others = [n for n in c.devices if n != this_name]
            other_str = ", ".join(f"'{n}'" for n in others) or "another device"
            lines.append(f"  • {c.nslc} with {other_str}")
        lines.append(suffix)
        return "\n".join(lines)

    @staticmethod
    def _humanize_validation_error(exc: Exception) -> str:
        """Strip pydantic's verbose error envelope to a one-line summary.

        Pydantic v2's default repr is multi-line and includes the URL
        to the docs — fine for log output, terrible for a dialog
        warning. We pluck the first error's ``msg`` field when
        available; otherwise we fall back to ``str(exc)``.
        """
        errors_attr = getattr(exc, "errors", None)
        if callable(errors_attr):
            try:
                errs = errors_attr()
            except Exception:
                errs = []
            if errs:
                first = errs[0]
                loc = ".".join(str(p) for p in first.get("loc", ()))
                msg = first.get("msg", "validation failed")
                return f"{loc}: {msg}" if loc else str(msg)
        return str(exc)


class DeviceDialog(QDialog):
    """Modal wrapper around :class:`DeviceForm` with OK/Cancel + store wiring.

    Two factory class methods — :meth:`add` and :meth:`edit` — cover
    the two flows. Each constructs the dialog, runs the modal loop,
    and returns the dialog code (Accepted / Rejected). On Accept the
    dialog calls the matching ConfigStore mutation, catches
    :class:`ConfigError`, surfaces it via ``QMessageBox.critical``,
    and KEEPS the dialog open so the user can correct the issue.

    M1-D adds the tabbed Echos surface: tab 0 ("Connection") is the
    client-side :class:`DeviceForm` saved through OK as before; tabs
    1-4 (Acquisition / SeedLink server / Network / Maintenance) edit
    the DEVICE's own config through an :class:`EchosDeviceWorker` on a
    per-dialog QThread, applied immediately per tab (read-modify-write
    with confirmation, rule 15) — independent of OK/Cancel. A 429
    lockout disables every server write with a countdown banner.
    """

    # Worker request signals — emitted on the GUI thread, delivered to
    # the per-dialog EchosDeviceWorker via QueuedConnection.
    _loadRequested = Signal(object)  # noqa: N815
    _stationxmlRequested = Signal(object)  # noqa: N815
    _acqApplyRequested = Signal(object, object)  # noqa: N815
    _slApplyRequested = Signal(object, object)  # noqa: N815
    _calStartRequested = Signal(object)  # noqa: N815
    _calPollRequested = Signal(object)  # noqa: N815
    _pwChangeRequested = Signal(object, str)  # noqa: N815
    _credStoreRequested = Signal(str, str)  # noqa: N815
    _rebootRequested = Signal(object)  # noqa: N815

    def __init__(
        self,
        *,
        title: str,
        store: ConfigStore,
        form: DeviceForm,
        on_accept: Callable[[DeviceConfig], None],
        parent: QWidget | None = None,
        credentials: CredentialsStore | None = None,
        client_factory: Callable[[EchosPollTarget, str | None], EchosApiClient] | None = None,
        restart_poll_interval_s: float = 0.5,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._store = store
        self._form = form
        self._on_accept = on_accept
        self._credentials = credentials if credentials is not None else CredentialsStore()
        self._client_factory = client_factory
        self._restart_poll_interval_s = restart_poll_interval_s
        self._worker: EchosDeviceWorker | None = None
        self._worker_thread: QThread | None = None
        # Teardown latch: once done() ran, late queued worker results
        # (e.g. a calibrationStatus posted before teardown) must never
        # resurrect a worker thread on a closed dialog (audit finding).
        self._torn_down = False
        self._state_loaded = False
        self._lockout_until = 0.0

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        self._tabs.addTab(form, "Connection")
        self._acq_tab = AcquisitionTab(self)
        self._sl_tab = SeedlinkTab(self)
        self._net_tab = NetworkTab(self)
        self._maint_tab = MaintenanceTab(self)
        self._server_tabs: tuple[AcquisitionTab | SeedlinkTab | NetworkTab | MaintenanceTab, ...]
        self._server_tabs = (self._acq_tab, self._sl_tab, self._net_tab, self._maint_tab)
        for tab, label in zip(
            self._server_tabs,
            ("Acquisition", "SeedLink server", "Network", "Maintenance"),
            strict=True,
        ):
            self._tabs.addTab(tab, label)
        layout.addWidget(self._tabs)

        # rule 15: surface a device lockout honestly across every tab.
        self._lockout_label = QLabel("", self)
        self._lockout_label.setWordWrap(True)
        self._lockout_label.setStyleSheet(_LOCKOUT_BANNER_STYLE)
        self._lockout_label.setVisible(False)
        layout.addWidget(self._lockout_label)
        self._lockout_timer = QTimer(self)
        self._lockout_timer.setInterval(1000)
        self._lockout_timer.timeout.connect(self._tick_lockout)

        # Standard OK/Cancel button row.
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        layout.addWidget(self._buttons)

        # Wire OK/Cancel and the form's validity signal.
        self._buttons.accepted.connect(self._on_ok_clicked)
        self._buttons.rejected.connect(self.reject)
        form.isValid.connect(self._on_form_validity)
        self._on_form_validity(form.is_valid())

        # Echos wiring: tabs ↔ worker routing (all worker traffic goes
        # through _emit_to_worker so the thread spins up lazily).
        form.echosEnabledChanged.connect(self._update_server_tabs_enabled)
        form.credentialStoreRequested.connect(self._on_store_credential)
        form.connectionTargetChanged.connect(self._invalidate_device_state)
        # StationXML auto-fetch (Bug 1+3): pull the device's PUBLIC StationXML
        # — debounced after host edits — so selectors are auto-derived without
        # the user hunting through server tabs (an empty-selectors device
        # connects but subscribes to nothing and retry-loops forever). Needs no
        # credentials, so it works on a brand-new device before a password is
        # stored.
        self._stationxml_timer = QTimer(self)
        self._stationxml_timer.setSingleShot(True)
        self._stationxml_timer.setInterval(_STATIONXML_DEBOUNCE_MS)
        self._stationxml_timer.timeout.connect(self._request_stationxml)
        form.connectionTargetChanged.connect(self._stationxml_timer.start)
        # Initial fetch on open for an already-addressed device (edit flow, or a
        # discovery-prefilled add) without waiting for a host edit.
        QTimer.singleShot(0, self._request_stationxml)
        # This dialog wires the credential sink, so the password row is live.
        form.enable_credential_store()
        self._tabs.currentChanged.connect(self._on_tab_changed)
        for tab in self._server_tabs:
            tab.reloadRequested.connect(self._request_load)
        self._acq_tab.applyRequested.connect(
            lambda cfg: self._emit_to_worker(self._acqApplyRequested, cfg)
        )
        self._sl_tab.applyRequested.connect(
            lambda cfg: self._emit_to_worker(self._slApplyRequested, cfg)
        )
        self._sl_tab.portChanged.connect(self._on_server_port_changed)
        # NetworkTab is read-only (write schema unpinned) — no apply wiring.
        self._maint_tab.calibrateRequested.connect(
            lambda: self._emit_to_worker(self._calStartRequested)
        )
        self._maint_tab.calibrationPollRequested.connect(
            lambda: self._emit_to_worker(self._calPollRequested)
        )
        self._maint_tab.passwordChangeRequested.connect(
            lambda pw: self._emit_to_worker(self._pwChangeRequested, pw)
        )
        self._maint_tab.rebootRequested.connect(
            lambda: self._emit_to_worker(self._rebootRequested)
        )
        self._update_server_tabs_enabled(form.echos_enabled())

    # ------------------------------------------------------------------
    # Factory class methods
    # ------------------------------------------------------------------
    @classmethod
    def add(
        cls,
        parent: QWidget,
        store: ConfigStore,
        *,
        prefill: DeviceConfig | None = None,
        engine: StreamingEngine | None = None,
        credentials: CredentialsStore | None = None,
    ) -> int:
        """Run the modal "Add device" dialog. Returns the dialog code.

        Args:
            parent: Owner widget — modal-parent for centering and
                Z-order.
            store: ConfigStore to call ``add_device`` on when the user
                accepts.
            prefill: Optional :class:`DeviceConfig` used to seed the
                form. The wizard / station-browser uses this to pre-fill
                host/port/selectors so the user only needs to type the
                name.
            engine: Live :class:`StreamingEngine` reference for the
                DSP chain editor's preview. The editor falls back to
                disabled when ``None``.
        """
        existing_devices = list(store.root.devices)
        existing = {d.name for d in existing_devices}
        form = DeviceForm(
            initial=prefill,
            existing_names=existing,
            existing_devices=existing_devices,
            editing_name=None,
            engine=engine,
            parent=None,
        )
        dialog = cls(
            title="Add device",
            store=store,
            form=form,
            on_accept=store.add_device,
            parent=parent,
            credentials=credentials,
        )
        return dialog.exec()

    @classmethod
    def edit(
        cls,
        parent: QWidget,
        store: ConfigStore,
        device_name: str,
        *,
        engine: StreamingEngine | None = None,
        credentials: CredentialsStore | None = None,
    ) -> int:
        """Run the modal "Edit device" dialog for an existing device.

        Args:
            parent: Owner widget.
            store: ConfigStore to call ``update_device`` on when the
                user accepts.
            device_name: Name of the device being edited. Must exist
                in ``store.root.devices`` — otherwise this method raises
                :class:`ConfigError`.
            engine: Live :class:`StreamingEngine` reference for the
                DSP chain editor's preview. The editor falls back to
                disabled when ``None``.

        Raises:
            ConfigError: ``device_name`` is unknown to the store.
        """
        existing_devices = list(store.root.devices)
        existing = {d.name for d in existing_devices}
        current = next(
            (d for d in existing_devices if d.name == device_name),
            None,
        )
        if current is None:
            raise ConfigError(f"unknown device {device_name!r}")
        form = DeviceForm(
            initial=current,
            existing_names=existing,
            existing_devices=existing_devices,
            editing_name=device_name,
            engine=engine,
            parent=None,
        )
        dialog = cls(
            title=f"Edit device — {device_name}",
            store=store,
            form=form,
            on_accept=lambda cfg: store.update_device(device_name, cfg),
            parent=parent,
            credentials=credentials,
        )
        return dialog.exec()

    # ------------------------------------------------------------------
    # Echos worker plumbing (M1-D)
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> EchosDeviceWorker | None:
        """Lazily spin up the per-dialog REST worker thread.

        Lazy because generic SeedLink devices never need it. All eight
        request signals and all result signals are wired QueuedConnection
        (cross-thread both ways); teardown happens in :meth:`done`.
        Returns ``None`` after teardown — a late queued event must never
        resurrect a thread on a closed dialog.
        """
        if self._torn_down:
            return None
        if self._worker is not None:
            return self._worker
        self._worker_thread = QThread(self)
        self._worker_thread.setObjectName("echos-device-dialog")
        self._worker = EchosDeviceWorker(
            self._credentials,
            client_factory=self._client_factory,
            restart_poll_interval_s=self._restart_poll_interval_s,
        )
        self._worker.moveToThread(self._worker_thread)
        queued = Qt.ConnectionType.QueuedConnection
        self._loadRequested.connect(self._worker.requestLoad, type=queued)
        self._stationxmlRequested.connect(self._worker.requestStationxml, type=queued)
        self._acqApplyRequested.connect(self._worker.applyAcquisition, type=queued)
        self._slApplyRequested.connect(self._worker.applySeedlink, type=queued)
        self._calStartRequested.connect(self._worker.startCalibration, type=queued)
        self._calPollRequested.connect(self._worker.pollCalibration, type=queued)
        self._pwChangeRequested.connect(self._worker.changePassword, type=queued)
        self._credStoreRequested.connect(self._worker.storeCredential, type=queued)
        self._rebootRequested.connect(self._worker.requestReboot, type=queued)
        self._worker.loaded.connect(self._on_loaded, type=queued)
        self._worker.stationxmlLoaded.connect(self._on_stationxml_loaded, type=queued)
        self._worker.applied.connect(self._on_applied, type=queued)
        self._worker.restartProgress.connect(self._sl_tab.on_restart_progress, type=queued)
        self._worker.seedlinkApplied.connect(self._sl_tab.on_seedlink_applied, type=queued)
        self._worker.calibrationStatus.connect(
            self._maint_tab.on_calibration_status, type=queued
        )
        self._worker.credentialStored.connect(self._on_credential_stored, type=queued)
        self._worker.passwordChanged.connect(self._maint_tab.on_password_changed, type=queued)
        self._worker.failed.connect(self._on_worker_failed, type=queued)
        self._worker_thread.start()
        return self._worker

    def _emit_to_worker(self, signal: SignalInstance, *args: object) -> None:
        """Attach the current target and route one request to the worker."""
        target = self._form.echos_target()
        if target is None or self._ensure_worker() is None:
            return
        signal.emit(target, *args)

    @Slot()
    def _request_load(self) -> None:
        target = self._form.echos_target()
        if target is None or self._ensure_worker() is None:
            return
        self._loadRequested.emit(target)

    def _request_stationxml(self) -> None:
        """Ask the worker for the device's PUBLIC StationXML (no credentials)."""
        target = self._form.echos_target()
        if target is None or self._ensure_worker() is None:
            return
        self._stationxmlRequested.emit(target)

    @Slot(object)
    def _on_stationxml_loaded(self, channels: object) -> None:
        if not isinstance(channels, tuple):
            return
        # The form derives selectors from these when it currently has none and
        # surfaces a status (Bug 1+3).
        self._form.set_device_channels(channels)

    @Slot()
    def _invalidate_device_state(self) -> None:
        """Host/HTTP-port changed: drop every server-side baseline.

        The loaded configs belong to the previous endpoint; applying
        them to a new host would write another device's settings.
        """
        if not self._state_loaded:
            return
        self._state_loaded = False
        for tab in self._server_tabs:
            tab.reset_loaded()
        self._form.set_device_channels(())

    @Slot(int)
    def _on_tab_changed(self, index: int) -> None:
        # First visit to any server tab triggers the initial device load.
        if index != _TAB_CONNECTION and not self._state_loaded:
            self._request_load()

    @Slot(bool)
    def _update_server_tabs_enabled(self, enabled: bool) -> None:
        for offset in range(len(self._server_tabs)):
            self._tabs.setTabEnabled(_TAB_CONNECTION + 1 + offset, enabled)

    @Slot(str, str)
    def _on_store_credential(self, device_key: str, password: str) -> None:
        if self._ensure_worker() is None:
            return
        self._credStoreRequested.emit(device_key, password)

    @Slot(object)
    def _on_loaded(self, state: object) -> None:
        if not isinstance(state, EchosDeviceState):
            return
        self._state_loaded = True
        for tab in self._server_tabs:
            tab.apply_state(state)
        self._form.set_device_channels(state.channels)
        self._form.set_credential_status(
            "An admin password is stored for this device."
            if state.has_credentials
            else "No admin password stored — server writes will fail until one is set."
        )

    @Slot(str)
    def _on_applied(self, op: str) -> None:
        if op == "acquisition":
            self._acq_tab.on_applied()
        elif op == "calibrate_start":
            self._maint_tab.on_calibration_started()
        elif op == "reboot":
            self._maint_tab.on_reboot_requested()

    @Slot(str)
    def _on_credential_stored(self, device_key: str) -> None:
        self._form.clear_password_field()
        self._form.set_credential_status(f"Password stored for {device_key!r}.")

    @Slot(int)
    def _on_server_port_changed(self, port: int) -> None:
        # Skill: a SeedLink port change must reach the client config so
        # the worker reconnects on the right endpoint — sync the
        # Connection tab; OK saves it through the normal store path.
        self._form.set_seedlink_port(port)

    @Slot(str, str, str, float)
    def _on_worker_failed(self, op: str, kind: str, message: str, retry_after_s: float) -> None:
        if self._torn_down:
            return  # late queued delivery on a closed dialog
        if kind == "locked_out":
            self._enter_lockout(retry_after_s)
        owner: dict[str, tuple[AcquisitionTab | SeedlinkTab | NetworkTab | MaintenanceTab, ...]]
        owner = {
            "acquisition": (self._acq_tab,),
            "seedlink": (self._sl_tab,),
            "calibrate_start": (self._maint_tab,),
            "calibrate_poll": (self._maint_tab,),
            "password": (self._maint_tab,),
            "reboot": (self._maint_tab,),
            "load": self._server_tabs,
        }
        for tab in owner.get(op, ()):
            tab.on_failed(op, kind, message)

    def _enter_lockout(self, retry_after_s: float) -> None:
        self._lockout_until = time.monotonic() + max(1.0, retry_after_s)
        for tab in self._server_tabs:
            tab.set_write_enabled(False, reason="Device auth lockout active.")
        self._lockout_timer.start()
        self._tick_lockout()

    @Slot()
    def _tick_lockout(self) -> None:
        remaining = self._lockout_until - time.monotonic()
        if remaining > 0:
            self._lockout_label.setText(
                f"Device auth lockout active — too many failed logins. Server writes "
                f"re-enable in {remaining:.0f} s (the app will not retry before then)."
            )
            self._lockout_label.setVisible(True)
            return
        self._lockout_timer.stop()
        self._lockout_label.setVisible(False)
        for tab in self._server_tabs:
            tab.set_write_enabled(True)

    def _teardown_worker(self) -> None:
        """Stop the REST worker before the dialog dies (bounded, rule 7).

        Idempotent. References are dropped only AFTER a successful join:
        deleting a worker whose thread is still inside a slot (e.g. a
        keyring D-Bus call the task-cancel cannot interrupt) would be a
        use-after-free — on timeout we warn and deliberately leak, the
        same policy MainWindow applies to its worker threads.
        """
        self._torn_down = True
        self._maint_tab.stop_polling()
        self._lockout_timer.stop()
        # Quiesce the StationXML debounce in the same teardown step as its
        # sibling: a host edit can leave it armed, and a late fire would only
        # be a no-op via the _torn_down guard — but a complete stop beats
        # relying on the downstream guard alone.
        self._stationxml_timer.stop()
        worker, thread = self._worker, self._worker_thread
        if worker is None or thread is None:
            return
        worker.stop()
        thread.quit()
        if not thread.wait(_ECHOS_WORKER_JOIN_MS):
            _log.warning("echos_device_worker_join_timeout")
            return
        self._worker = None
        self._worker_thread = None

    def done(self, result: int) -> None:
        # Single teardown point: accept(), reject() and window-close all
        # funnel through QDialog.done().
        self._teardown_worker()
        super().done(result)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    @Slot(bool)
    def _on_form_validity(self, valid: bool) -> None:
        ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(valid)

    @Slot()
    def _on_ok_clicked(self) -> None:
        if not self._form.is_valid():
            # Defensive: button should already be disabled. Not raising
            # — just refusing the accept silently keeps the dialog open.
            return
        try:
            cfg = self._form.to_config()
        except ValueError as exc:
            QMessageBox.critical(self, "Invalid input", str(exc))
            return
        # M11: validate the response-metadata file loads as an inventory
        # before saving, so a typo'd path is caught at the dialog (not
        # silently at first physical-unit use). Empty path → no-op.
        response_error = self._form.validate_response_metadata()
        if response_error is not None:
            QMessageBox.critical(self, "Invalid response metadata", response_error)
            return
        try:
            self._on_accept(cfg)
        except ConfigError as exc:
            # Stage-B contract: ConfigError keeps the dialog open so the
            # user can correct the issue without losing their other input.
            QMessageBox.critical(self, "Save failed", str(exc))
            _log.warning("device_dialog_save_failed", error=str(exc))
            return
        self.accept()


class ConfirmRemoveDialog(QDialog):
    """Modal "Remove device 'X'?" confirmation popup.

    Carries a permanently-disabled "Also delete this device's archived
    data" checkbox so the user understands archive deletion is a
    separate (M6) feature. Returns Accepted on OK, Rejected on Cancel.
    """

    def __init__(self, device_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remove device")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Remove device '{device_name}'?", self))

        self._delete_archive_checkbox = QCheckBox("Also delete this device's archived data", self)
        self._delete_archive_checkbox.setEnabled(False)
        self._delete_archive_checkbox.setToolTip(_REMOVE_ARCHIVE_TOOLTIP)
        layout.addWidget(self._delete_archive_checkbox)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        layout.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)


__all__ = ["ConfirmRemoveDialog", "DeviceDialog", "DeviceForm"]
