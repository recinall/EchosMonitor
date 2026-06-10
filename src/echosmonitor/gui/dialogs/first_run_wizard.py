"""First-run wizard (M4 stage C).

Triggered when ``is_first_run(...)`` returns True at app startup
(see :mod:`echosmonitor.core.firstrun`). Three short pages:

1. **Welcome** — three-way radio: recommended public server (default),
   configure my own, or skip. The wizard fires a best-effort INFO
   probe at GFZ and IRIS in the background while the user reads the
   blurb so the recommended path lights up the moment a server has
   confirmed itself reachable. If neither responds within
   :data:`_PROBE_TIMEOUT_S`, the recommended path stays available
   but uses GFZ as the deterministic default — the user can edit
   later from the Devices dock.

2. **Configure** — only reached when "configure my own" is selected.
   Embeds the same :class:`DeviceForm` the regular Add Device dialog
   uses, plus a "Test connection" button that fires
   :meth:`InfoWorker.requestId` and renders the result inline.

3. **Confirmation** — summary of the device that will be created
   (name, host:port, selectors), the path the YAML will live at,
   and a one-sentence "you can edit this file at any time" hint.
   ``[Finish]`` writes via :class:`ConfigStore`.

The wizard never bypasses :class:`ConfigStore`. Every write goes
through the same validation + atomic-write pipeline as a runtime
mutation, so a wizard-induced bad config is impossible.

Skip path: no device is written; the main window displays the
DevicePanel's empty-state CTA + the status-bar tip (Stage C2),
so the user has two visible affordances to add their first server.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from echosmonitor.config.schema import (
    BandpassStage,
    DetrendStage,
    DeviceConfig,
    ReconnectConfig,
    StaLtaStage,
    StreamSelectorConfig,
)
from echosmonitor.core.exceptions import ConfigError
from echosmonitor.gui.dialogs.device_dialog import DeviceForm

if TYPE_CHECKING:
    from echosmonitor.core.config_store import ConfigStore
    from echosmonitor.core.info_worker import InfoWorker

_log = structlog.get_logger(__name__)


# Two well-known public SeedLink servers used as the recommended
# defaults. GFZ replaced INGV in M4 prep (H1) because the latter became
# unroutable from EU consumer ISPs in May 2026. IRIS is the canonical
# US-based alternative; the wizard prefers whichever answered first.
_RECOMMENDED_GFZ = ("gfz-de", "geofon.gfz-potsdam.de", 18000, "GE", "WLF", "", "BHZ", "GFZ Potsdam")
_RECOMMENDED_IRIS = (
    "iris-iu-anmo",
    "rtserve.iris.washington.edu",
    18000,
    "IU",
    "ANMO",
    "00",
    "BHZ",
    "IRIS DMC",
)

# Wall-clock budget for the welcome-page background probe. After this
# the recommended path stays selectable but uses GFZ deterministically.
# Generous enough for transcontinental round-trips; short enough that a
# user clicking Next quickly does not wait long.
_PROBE_TIMEOUT_S = 5.0
# How often the wizard polls its in-flight probe state when the user
# clicks Next before the probe completes. 100 ms keeps cancel latency
# (in the unusual case where the user cancels the wizard mid-probe)
# under one frame at 60 Hz.
_PROBE_POLL_MS = 100

# Page indices — wizard supports non-linear navigation via nextId().
_PAGE_WELCOME = 0
_PAGE_CONFIGURE = 1
_PAGE_CONFIRM = 2


def _make_recommended_device(name: str, host: str, port: int) -> DeviceConfig:
    """Build the recommended-server config with a sensible default chain.

    Mirrors the IRIS / GFZ blocks commented into ``config/default.yaml``:
    a detrend → bandpass(0.5-8 Hz) → STA/LTA(1/30 s, 3.5/1.5) chain
    that produces visibly filtered output on a global-scale broadband
    instrument without firing detections on quiet background.
    """
    network, station, location, channel = {
        _RECOMMENDED_GFZ[1]: ("GE", "WLF", "", "BHZ"),
        _RECOMMENDED_IRIS[1]: ("IU", "ANMO", "00", "BHZ"),
    }.get(host, ("*", "*", "", "*"))
    return DeviceConfig(
        name=name,
        host=host,
        port=port,
        reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0, connect_timeout_s=10.0),
        selectors=[
            StreamSelectorConfig(
                network=network,
                station=station,
                location=location,
                channel=channel,
            )
        ],
        dsp_chain=[
            DetrendStage(type="detrend", kind="constant"),
            BandpassStage(type="bandpass", freqmin=0.5, freqmax=8.0, corners=4, zerophase=False),
            StaLtaStage(
                type="sta_lta",
                sta=1.0,
                lta=30.0,
                on_threshold=3.5,
                off_threshold=1.5,
            ),
        ],
    )


class _WelcomePage(QWizardPage):
    """First page: pick a path forward (recommended / configure / skip).

    Fires a background INFO ID probe at both recommended servers so
    the time the user spends reading the blurb is also probe-warmup
    time. Whichever server replies first is locked in; if neither
    replies within :data:`_PROBE_TIMEOUT_S` (enforced by a
    ``QTimer.singleShot`` started from :meth:`initializePage`), the
    wizard falls back to GFZ deterministically.

    Probe dispatch goes through an internal ``_idRequested`` signal
    connected to ``InfoWorker.requestId`` via
    ``Qt.ConnectionType.QueuedConnection``. Calling the slot directly
    from the GUI thread would NOT route through Qt's event queue —
    ``@Slot`` decorators do not change Python attribute-call semantics
    — and the slot's body does blocking TCP I/O for up to 30 s, which
    would freeze the welcome page. Mirrors the
    ``station_browser.py::_stationsRequested`` / ``_streamsRequested``
    pattern. The original Stage C draft called the slot directly and
    code-reviewer caught the regression in the M4 stage C pass.
    """

    # Cross-thread request emission. The QueuedConnection ensures the
    # request body runs on the InfoWorker's thread, not ours.
    _idRequested = Signal(str, str, str, int)  # noqa: N815

    def __init__(self, info_worker: InfoWorker, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info_worker = info_worker
        self._probe_winner: tuple[str, str, int, str] | None = None  # (label, host, port, name)
        self._gfz_request_id = uuid.uuid4().hex
        self._iris_request_id = uuid.uuid4().hex
        self._gfz_responded = False
        self._iris_responded = False
        # Single-shot timer enforces ``_PROBE_TIMEOUT_S``. If neither
        # probe responds by the deadline, ``_force_fallback`` locks
        # GFZ in deterministically and updates the probe label.
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_probe_timeout)

        self.setTitle("Welcome to EchosMonitor")
        self.setSubTitle("Choose how you'd like to get started.")

        layout = QVBoxLayout(self)
        blurb = QLabel(
            "EchosMonitor streams real-time seismic data from one or more "
            "SeedLink servers. To start, pick where to get your data from."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self._radio_recommended = QRadioButton("Start with a recommended public server")
        self._radio_recommended.setChecked(True)
        self._radio_configure = QRadioButton("Configure my own server now")
        self._radio_skip = QRadioButton("Skip — I'll add a device later")

        self._group = QButtonGroup(self)
        for radio in (self._radio_recommended, self._radio_configure, self._radio_skip):
            self._group.addButton(radio)
            layout.addWidget(radio)

        self._probe_label = QLabel("Probing recommended servers…")
        self._probe_label.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(self._probe_label)

        layout.addStretch(1)

        # Cross-thread request signal: routes ``_idRequested.emit(...)``
        # to ``InfoWorker.requestId`` on the worker thread. Without
        # the QueuedConnection, a direct call would block the GUI
        # thread inside the slot body for the duration of the fetch.
        self._idRequested.connect(
            self._info_worker.requestId, type=Qt.ConnectionType.QueuedConnection
        )

        # Wire probe replies. The wizard subscribes to the worker's
        # public reply signals; request_id filtering ensures stale
        # responses (from any other code path that might issue probes)
        # are ignored.
        self._info_worker.identityReceived.connect(
            self._on_identity, type=Qt.ConnectionType.QueuedConnection
        )
        self._info_worker.infoFailed.connect(
            self._on_failed, type=Qt.ConnectionType.QueuedConnection
        )

    def initializePage(self) -> None:  # noqa: N802 — Qt override
        """Kick off the probe lazily on first display.

        Avoids firing the probe in tests that never reach this page,
        and starts the wall-clock timer so a hung probe cannot delay
        the wizard past :data:`_PROBE_TIMEOUT_S`.
        """
        gfz_host = _RECOMMENDED_GFZ[1]
        gfz_port = _RECOMMENDED_GFZ[2]
        iris_host = _RECOMMENDED_IRIS[1]
        iris_port = _RECOMMENDED_IRIS[2]
        # Emit through the queued-connection signal so the work runs
        # on the InfoWorker thread. ``requestId`` echoes the second
        # arg as the device_id label; we use the host:port string so
        # the reply slot can identify which server answered.
        self._idRequested.emit(self._gfz_request_id, f"{gfz_host}:{gfz_port}", gfz_host, gfz_port)
        self._idRequested.emit(
            self._iris_request_id, f"{iris_host}:{iris_port}", iris_host, iris_port
        )
        # Start the wall-clock budget. If neither probe replies in
        # ``_PROBE_TIMEOUT_S``, ``_on_probe_timeout`` locks GFZ in.
        self._timeout_timer.start(int(_PROBE_TIMEOUT_S * 1000))

    @Slot(str, str, object)
    def _on_identity(self, request_id: str, label: str, _identity: object) -> None:
        if request_id == self._gfz_request_id:
            self._gfz_responded = True
            if self._probe_winner is None:
                self._probe_winner = (
                    _RECOMMENDED_GFZ[7],  # display label
                    _RECOMMENDED_GFZ[1],
                    _RECOMMENDED_GFZ[2],
                    _RECOMMENDED_GFZ[0],  # device name
                )
                self._probe_label.setText(f"Recommended: {label} (responded ✓)")
                self._timeout_timer.stop()
        elif request_id == self._iris_request_id:
            self._iris_responded = True
            if self._probe_winner is None:
                self._probe_winner = (
                    _RECOMMENDED_IRIS[7],
                    _RECOMMENDED_IRIS[1],
                    _RECOMMENDED_IRIS[2],
                    _RECOMMENDED_IRIS[0],
                )
                self._probe_label.setText(f"Recommended: {label} (responded ✓)")
                self._timeout_timer.stop()

    @Slot(str, str, str, str)
    def _on_failed(self, request_id: str, _label: str, _kind: str, _reason: str) -> None:
        if request_id == self._gfz_request_id:
            self._gfz_responded = True
        elif request_id == self._iris_request_id:
            self._iris_responded = True
        if self._gfz_responded and self._iris_responded and self._probe_winner is None:
            # Both probes failed — fall back to GFZ deterministically.
            # The user can still proceed; if GFZ is unreachable from
            # their network they'll see WAITING_RETRY in the Devices
            # dock and can fix it from there.
            self._probe_winner = (
                _RECOMMENDED_GFZ[7],
                _RECOMMENDED_GFZ[1],
                _RECOMMENDED_GFZ[2],
                _RECOMMENDED_GFZ[0],
            )
            self._probe_label.setText(
                "Recommended: GFZ Potsdam (probe failed; using as default — you can edit it later)."
            )
            self._timeout_timer.stop()

    @Slot()
    def _on_probe_timeout(self) -> None:
        """Fire after :data:`_PROBE_TIMEOUT_S` if neither probe locked in.

        ``_on_identity`` and ``_on_failed`` stop the timer the moment a
        winner is set, so this only runs when both probes are still
        outstanding past the budget — i.e. the in-flight ``info.fetch``
        is still blocking on the worker thread inside its own deadline.
        We DO NOT cancel the in-flight fetch (the InfoWorker tracks
        only one ``_in_flight`` token across all clients, and other
        code paths may also be using it); we just commit to the
        deterministic GFZ default so the wizard's UX is bounded.
        """
        if self._probe_winner is not None:
            return
        self._probe_winner = (
            _RECOMMENDED_GFZ[7],
            _RECOMMENDED_GFZ[1],
            _RECOMMENDED_GFZ[2],
            _RECOMMENDED_GFZ[0],
        )
        self._probe_label.setText(
            f"Recommended: GFZ Potsdam (no response within {int(_PROBE_TIMEOUT_S)}s — "
            "using as default; you can edit it later)."
        )

    def winner(self) -> tuple[str, str, int, str] | None:
        """Return the locked-in recommended server, or ``None`` if no probe answered yet.

        Tuple shape: ``(display_label, host, port, device_name)``.
        """
        return self._probe_winner

    def selected_path(self) -> str:
        """Return ``"recommended" | "configure" | "skip"`` based on the radios."""
        if self._radio_recommended.isChecked():
            return "recommended"
        if self._radio_configure.isChecked():
            return "configure"
        return "skip"

    def nextId(self) -> int:  # noqa: N802 — Qt override
        path = self.selected_path()
        if path == "configure":
            return _PAGE_CONFIGURE
        # Recommended and skip both go straight to confirm.
        return _PAGE_CONFIRM


class _ConfigurePage(QWizardPage):
    """Second page: embedded :class:`DeviceForm` + Test connection button.

    Uses the same internal-signal pattern as :class:`_WelcomePage` for
    cross-thread INFO requests: ``_idRequested`` is wired to
    ``InfoWorker.requestId`` via ``Qt.ConnectionType.QueuedConnection``
    so the slot runs on the worker thread, not the GUI thread.
    Calling the slot directly from the GUI thread would block for up
    to 30 s on an unreachable host (the slot's body does blocking TCP
    I/O — the ``@Slot`` decorator is a Qt-side annotation, not a
    cross-thread dispatcher).
    """

    _idRequested = Signal(str, str, str, int)  # noqa: N815

    def __init__(
        self,
        info_worker: InfoWorker,
        existing_names: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._info_worker = info_worker
        self.setTitle("Configure your server")
        self.setSubTitle(
            "Enter your SeedLink server details. The Test connection button "
            "performs an INFO ID query."
        )

        outer = QVBoxLayout(self)
        self._form = DeviceForm(existing_names=existing_names, parent=self)
        outer.addWidget(self._form)

        test_row = QHBoxLayout()
        self._test_button = QPushButton("Test connection")
        self._test_label = QLabel("")
        self._test_label.setStyleSheet("color: #888; font-style: italic;")
        test_row.addWidget(self._test_button)
        test_row.addWidget(self._test_label, stretch=1)
        outer.addLayout(test_row)

        self._test_request_id: str | None = None
        self._test_button.clicked.connect(self._on_test_clicked)
        self._idRequested.connect(
            self._info_worker.requestId, type=Qt.ConnectionType.QueuedConnection
        )
        self._info_worker.identityReceived.connect(
            self._on_test_identity, type=Qt.ConnectionType.QueuedConnection
        )
        self._info_worker.infoFailed.connect(
            self._on_test_failed, type=Qt.ConnectionType.QueuedConnection
        )

        # Forward the form's validity state into the wizard's Next/Finish
        # button machinery via ``isComplete`` overrides.
        self._form.isValid.connect(self._on_form_valid)

    def isComplete(self) -> bool:  # noqa: N802 — Qt override
        return self._form.is_valid()

    @Slot(bool)
    def _on_form_valid(self, _valid: bool) -> None:
        # Notify QWizard that ``isComplete`` may have changed.
        self.completeChanged.emit()

    @Slot()
    def _on_test_clicked(self) -> None:
        if not self._form.is_valid():
            self._test_label.setText("Fix validation errors first.")
            return
        cfg = self._form.to_config()
        self._test_request_id = uuid.uuid4().hex
        self._test_label.setText("Probing…")
        label = f"{cfg.host}:{cfg.port}"
        # Emit through the queued-connection signal so the fetch runs
        # on the InfoWorker thread, not ours. See class docstring.
        self._idRequested.emit(self._test_request_id, label, cfg.host, cfg.port)

    @Slot(str, str, object)
    def _on_test_identity(self, request_id: str, label: str, identity: object) -> None:
        if request_id != self._test_request_id:
            return
        version = getattr(identity, "version", "?")
        organization = getattr(identity, "organization", "") or ""
        suffix = f" ({organization})" if organization else ""
        self._test_label.setText(f"✓ Connected to {label} v{version}{suffix}")

    @Slot(str, str, str, str)
    def _on_test_failed(self, request_id: str, label: str, _kind: str, reason: str) -> None:
        if request_id != self._test_request_id:
            return
        self._test_label.setText(f"✗ {label}: {reason}")

    def to_config(self) -> DeviceConfig:
        return self._form.to_config()

    def nextId(self) -> int:  # noqa: N802 — Qt override
        return _PAGE_CONFIRM


class _ConfirmPage(QWizardPage):
    """Third page: summary + Finish.

    Pulls the chosen device from whichever upstream page is active
    (welcome's recommended winner or configure's form). Renders a
    short summary plus the resolved config path.
    """

    def __init__(
        self,
        store: ConfigStore,
        welcome: _WelcomePage,
        configure: _ConfigurePage,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._welcome = welcome
        self._configure = configure
        self.setTitle("Confirm")

        layout = QVBoxLayout(self)
        self._summary_label = QLabel("")
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        self._path_label = QLabel("")
        self._path_label.setWordWrap(True)
        self._path_label.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(self._path_label)

        layout.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802 — Qt override
        path = self._welcome.selected_path()
        cfg: DeviceConfig | None = None
        if path == "recommended":
            winner = self._welcome.winner()
            if winner is None:
                # Fallback (should be rare — hit only if the user
                # clicked Next before either probe completed AND
                # before the timeout fallback fired). Use GFZ.
                winner = (
                    _RECOMMENDED_GFZ[7],
                    _RECOMMENDED_GFZ[1],
                    _RECOMMENDED_GFZ[2],
                    _RECOMMENDED_GFZ[0],
                )
            display_label, host, port, name = winner
            cfg = _make_recommended_device(name, host, port)
            self._summary_label.setText(
                f"<b>Recommended server:</b> {display_label}<br>"
                f"<b>Name:</b> {name}<br>"
                f"<b>Host:</b> {host}:{port}<br>"
                f"<b>Selectors:</b> {cfg.selectors[0].network}.{cfg.selectors[0].station}."
                f"{cfg.selectors[0].location}.{cfg.selectors[0].channel}<br>"
                f"<b>DSP chain:</b> {len(cfg.dsp_chain)} stages"
            )
        elif path == "configure":
            cfg = self._configure.to_config()
            sel = cfg.selectors[0] if cfg.selectors else None
            sel_text = (
                f"{sel.network}.{sel.station}.{sel.location}.{sel.channel}"
                if sel is not None
                else "(none)"
            )
            self._summary_label.setText(
                f"<b>Custom server:</b><br>"
                f"<b>Name:</b> {cfg.name}<br>"
                f"<b>Host:</b> {cfg.host}:{cfg.port}<br>"
                f"<b>Selectors:</b> {sel_text}<br>"
                f"<b>DSP chain:</b> {len(cfg.dsp_chain)} stages"
            )
        else:
            self._summary_label.setText(
                "<b>No device will be created.</b><br>"
                "You can add one any time from the Devices dock."
            )
        self._pending_cfg = cfg
        self._path_label.setText(
            f"Configuration will be saved to {self._store.path}. "
            "You can edit this file directly at any time."
        )

    def commit(self) -> None:
        """Apply the chosen device to the store. Called from the wizard's accept path.

        Raises:
            ConfigError: If the store rejects the config (e.g.
                duplicate name in an unusual race).
        """
        cfg = getattr(self, "_pending_cfg", None)
        if cfg is None:
            return
        self._store.add_device(cfg)


class FirstRunWizard(QWizard):
    """The 3-page first-run wizard (M4 stage C).

    Constructed only when :func:`is_first_run` returns True at app
    startup — the regression test
    ``tests/gui/test_first_run_no_trigger.py`` pins the
    not-shown-on-populated-config invariant.
    """

    def __init__(
        self,
        *,
        store: ConfigStore,
        info_worker: InfoWorker,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("EchosMonitor — First Run")
        self.setModal(True)
        # Don't show the "<" Back button on the welcome page — the
        # wizard's flow is mostly forward.
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)

        self._store = store

        existing_names = [d.name for d in store.root.devices]
        self._welcome = _WelcomePage(info_worker, parent=self)
        self._configure = _ConfigurePage(info_worker, existing_names, parent=self)
        self._confirm = _ConfirmPage(store, self._welcome, self._configure, parent=self)

        self.setPage(_PAGE_WELCOME, self._welcome)
        self.setPage(_PAGE_CONFIGURE, self._configure)
        self.setPage(_PAGE_CONFIRM, self._confirm)
        self.setStartId(_PAGE_WELCOME)

    def accept(self) -> None:
        try:
            self._confirm.commit()
        except ConfigError as exc:
            # Surface the error to the user and KEEP the wizard open
            # (do NOT fall through to ``super().accept()``) so they
            # can fix duplicate-name conflicts and retry. Silent
            # reject was the original M4 stage C behaviour; code-
            # reviewer caught it in the stage C pass.
            _log.warning("first_run_wizard_commit_failed", error=str(exc))
            QMessageBox.warning(
                self,
                "Could not save device",
                f"The device could not be saved:\n\n{exc}\n\n"
                "Use the Back button to fix the form and try Finish again.",
            )
            return
        super().accept()


__all__ = ["FirstRunWizard"]
