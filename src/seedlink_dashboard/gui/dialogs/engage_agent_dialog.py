"""Engage-AI-agent modal dialog (M9 Stage B GUI; M10 registry-driven).

Mirrors the structure/validation pattern of
:class:`~seedlink_dashboard.gui.dialogs.device_dialog.DeviceDialog`: a
QDialog with OK/Cancel chrome around a form. It is constructed with plain
data (``streams_by_device`` + an ``stream_fs`` callable) rather than a live
engine so it is unit-testable without acquisition.

**Registry- and agent-driven.** The agent dropdown is populated directly
from :data:`seedlink_dashboard.ai.agents.AGENTS` (every registered agent,
sorted), so a future agent appears with NO change here. The per-agent
parameter fields are NOT hardcoded: each agent declares its engage-time
parameters via :meth:`~seedlink_dashboard.ai.base.AIAgent.engage_params`,
and the dialog renders one widget per :class:`~seedlink_dashboard.ai.base.
AgentParam`, rebuilding the section whenever the selected agent changes.
The domain banner reads the SELECTED agent's
:attr:`~seedlink_dashboard.ai.base.AIAgent.domain_spec`, so the picker /
detector show an amber warning on an accelerometer while the
agnostic learning agents (heuristic / autoencoder) stay green.

The check INFORMS, never blocks: Engage stays enabled on any severity (the
project scope is explicitly mixed/experimental).

This module is torch-free. Constructing any of the four agents purely to
read ``name`` / ``requires_fit`` / ``domain_spec`` / ``engage_params`` does
not import torch/seisbench, so the dialog renders even without the ``ai``
extra installed (an unavailable agent is shown but disabled).
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from PySide6.QtCore import QDateTime, Qt, QTimeZone
from PySide6.QtGui import QStandardItem
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from seedlink_dashboard.ai.agents import AGENTS, available_agents
from seedlink_dashboard.ai.base import AgentParam, AIAgent
from seedlink_dashboard.ai.domain import (
    DomainCheck,
    DomainSpec,
    Severity,
    StreamMeta,
    compatibility,
    stream_meta_from_nslc,
)

_log = structlog.get_logger(__name__)

# SEED orientation letters that form a 3-component group.
_COMPONENT_ORDER = ("Z", "N", "E")

# Default archive range when opening in archive mode: the last hour.
_DEFAULT_ARCHIVE_SPAN_S = 3600

# Banner styling per severity. Distinct background colours so RED reads as
# prominent (rule: the check must be honest and obvious) without blocking.
_BANNER_STYLES: dict[Severity, str] = {
    Severity.OK: "QLabel#EngageBanner { background: #1e5631; color: #eaffea; padding: 8px; }",
    Severity.WARNING: "QLabel#EngageBanner { background: #7a5c00; color: #fff7e0; padding: 8px; }",
    Severity.LIKELY_INVALID: (
        "QLabel#EngageBanner { background: #7a1f1f; color: #ffecec; "
        "padding: 8px; font-weight: bold; }"
    ),
}

# Styled note shown for a requires_fit agent in LIVE mode.
_FIT_LIVE_NOTE = "This agent learns a baseline from recent live data before it starts inferring."
# Note shown when a requires_fit agent is disabled in ARCHIVE mode.
_FIT_ARCHIVE_NOTE = "Learning agents are not yet supported on past data."

_FIT_NOTE_STYLE = "QLabel#EngageFitNote { color: #58a6ff; font-style: italic; padding: 4px; }"


def _utc_fields(epoch: float) -> QDateTime:
    """A field-preserving ``QDateTime`` whose displayed wall-clock is the UTC
    of ``epoch`` (matching the dialog's UTC-fields convention so ``_iso_of``
    formats it back verbatim with a literal ``Z``)."""
    u = QDateTime.fromSecsSinceEpoch(int(epoch), QTimeZone.utc())
    return QDateTime(u.date(), u.time())


def _station_of(nslc: str) -> str:
    """The station-grouping key of an NSLC: everything minus the orientation.

    A Z/N/E group shares NET.STA.LOC + the band/instrument letters of the
    channel, differing only in ``channel[2]`` (the orientation). So we key
    by NSLC with the orientation letter stripped.
    """
    parts = nslc.split(".")
    if len(parts) != 4:
        return nslc
    net, sta, loc, cha = parts
    base_cha = cha[:2] if len(cha) >= 3 else cha
    return f"{net}.{sta}.{loc}.{base_cha}"


def _orientation_of(nslc: str) -> str:
    parts = nslc.split(".")
    if len(parts) != 4:
        return ""
    cha = parts[3]
    return cha[2] if len(cha) >= 3 else ""


def _as_float(value: object) -> float:
    """Coerce an :class:`AgentParam.default` (typed ``object``) to ``float``.

    The defaults are always numeric for float/int params; this localises the
    one cast so the widget builders stay type-clean.
    """
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def _construct_agent(agent_id: str, kwargs: dict[str, object]) -> AIAgent | None:
    """Construct an agent via the registry, torch-free, for metadata reads.

    Returns ``None`` if the factory raises (e.g. an invalid kwarg combination
    while the user is mid-edit) so the banner degrades gracefully rather than
    crashing the dialog.
    """
    factory = AGENTS.get(agent_id)
    if factory is None:
        return None
    try:
        return factory(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive, mid-edit kwargs
        _log.debug("engage_agent_construct_failed", agent_id=agent_id, error=str(exc))
        return None


class EngageAgentDialog(QDialog):
    """Modal dialog to engage an AI agent on a live channel group.

    Args:
        streams_by_device: device name -> list of live NSLCs (from
            :meth:`StreamingEngine.live_streams`).
        stream_fs: callable ``(device, nslc) -> fs`` used to build the
            domain check's :class:`StreamMeta`s. May raise / return 0 for
            an unknown stream; the dialog tolerates that.
        archive: when ``True`` the dialog runs in "past data" mode: it adds
            Start/End UTC range pickers and :meth:`result_params` also
            returns ``t_start`` / ``t_end`` as ISO-8601 strings. The accept
            button reads "Run". A ``requires_fit`` agent cannot run on past
            data, so such agents are disabled in the dropdown.
        parent: standard Qt parent.

    The agent dropdown is the registry (:data:`AGENTS`); an agent whose
    runtime extra is missing (e.g. the autoencoder without ``torch``) is
    shown but disabled. The per-agent fields and the domain banner are both
    driven by the selected agent, not hardcoded.
    """

    def __init__(
        self,
        streams_by_device: dict[str, list[str]],
        stream_fs: Callable[[str, str], float],
        archive: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._archive = bool(archive)
        self.setWindowTitle("Run AI agent on past data" if self._archive else "Engage AI agent")
        self.setModal(True)
        self._streams_by_device = {d: list(n) for d, n in streams_by_device.items()}
        self._stream_fs = stream_fs
        self._availability = available_agents()
        # Cached component->nslc group for the current device/station.
        self._group: dict[str, str] = {}
        # Archive range pickers (only created in archive mode).
        self._start_edit: QDateTimeEdit | None = None
        self._end_edit: QDateTimeEdit | None = None
        # Dynamic per-agent parameter widgets: param.name -> widget.
        self._param_widgets: dict[str, QWidget] = {}
        self._param_specs: dict[str, AgentParam] = {}

        self._form = QFormLayout()
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Agent ----------------------------------------------------------
        self._agent_combo = QComboBox(self)
        self._populate_agent_combo()
        self._agent_combo.currentIndexChanged.connect(self._on_agent_changed)
        self._form.addRow("Agent:", self._agent_combo)

        # Per-agent dynamic parameter section: a placeholder row anchor we
        # insert/remove rows BEFORE. We track the inserted rows so we can
        # rebuild cleanly on agent change.
        self._params_anchor_row = self._form.rowCount()

        # Device + component group --------------------------------------
        self._device_combo = QComboBox(self)
        for device in sorted(self._streams_by_device):
            self._device_combo.addItem(device, userData=device)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._form.addRow("Device:", self._device_combo)

        self._station_combo = QComboBox(self)
        self._station_combo.currentIndexChanged.connect(self._on_station_changed)
        self._form.addRow("Station:", self._station_combo)

        self._components_label = QLabel("", self)
        self._components_label.setWordWrap(True)
        self._form.addRow("Components:", self._components_label)

        # Archive range (only in archive mode) ---------------------------
        if self._archive:
            # Prefill with the current UTC wall-clock as field-preserving
            # LocalTime values: the widget displays them verbatim under the
            # "(UTC)" label and the read path stamps the UTC spec, so what
            # the user sees and what is sent agree without a local shift.
            utc_now = QDateTime.currentDateTimeUtc()
            now_fields = QDateTime(utc_now.date(), utc_now.time())
            self._end_edit = self._make_datetime_edit(now_fields)
            self._start_edit = self._make_datetime_edit(
                now_fields.addSecs(-_DEFAULT_ARCHIVE_SPAN_S)
            )
            self._form.addRow("Start (UTC):", self._start_edit)
            self._form.addRow("End (UTC):", self._end_edit)

        # Domain banner --------------------------------------------------
        self._banner = QLabel("", self)
        self._banner.setObjectName("EngageBanner")
        self._banner.setWordWrap(True)

        # requires_fit note (shown only for learning agents).
        self._fit_note = QLabel("", self)
        self._fit_note.setObjectName("EngageFitNote")
        self._fit_note.setWordWrap(True)
        self._fit_note.setStyleSheet(_FIT_NOTE_STYLE)
        self._fit_note.setVisible(False)

        # Install hint (shown when the selected agent's extra is absent).
        self._hint = QLabel("", self)
        self._hint.setObjectName("EngageInstallHint")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("QLabel#EngageInstallHint { color: #b58900; font-style: italic; }")
        self._hint.setVisible(False)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setText("Run" if self._archive else "Engage")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(self._form)
        root.addWidget(self._banner)
        root.addWidget(self._fit_note)
        root.addWidget(self._hint)
        root.addWidget(self._buttons)

        # Build the initial agent's fields, then populate the station list
        # for the initial device, which cascades into the group + banner.
        self._rebuild_param_fields()
        self._on_device_changed()
        self._refresh_fit_note()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def result_params(self) -> dict[str, object]:
        """Return the engagement parameters chosen by the user.

        Keys: ``agent_id``, ``agent_kwargs`` (a ``{param.name: value}`` dict
        for the SELECTED agent only — so the picker carries
        ``model``/``weights``/``threshold_p``/``threshold_s`` while the
        heuristic carries only ``analysis_seconds``), ``device`` and
        ``group`` (component letter -> NSLC). In archive mode the dict
        additionally carries ``t_start`` and ``t_end`` as ISO-8601 strings
        (the caller converts to obspy ``UTCDateTime``).
        """
        params: dict[str, object] = {
            "agent_id": self._current_agent_id(),
            "agent_kwargs": self._collect_agent_kwargs(),
            "device": str(self._device_combo.currentData() or ""),
            "group": dict(self._group),
        }
        if self._archive and self._start_edit is not None and self._end_edit is not None:
            params["t_start"] = self._iso_of(self._start_edit)
            params["t_end"] = self._iso_of(self._end_edit)
        return params

    def apply_prefill(
        self,
        device: str,
        group: dict[str, str],
        t_start_epoch: float,
        t_end_epoch: float,
    ) -> None:
        """Pre-select the device/station + archive interval (Archive tab hand-off).

        Selects the device combo, cascades the station list, picks the station
        whose Z/N/E group matches ``group``, and sets the archive datetime
        fields so the run targets EXACTLY the interval the user selected in the
        Archive tab — no silent re-interpretation. No-op outside archive mode.
        """
        idx = self._device_combo.findData(device)
        if idx >= 0:
            self._device_combo.setCurrentIndex(idx)  # cascades _on_device_changed
        # Pick the station whose grouped components match the handed-off group.
        z_nslc = group.get("Z")
        target_station = _station_of(z_nslc) if z_nslc else None
        if target_station is not None:
            sidx = self._station_combo.findData(target_station)
            if sidx >= 0:
                self._station_combo.setCurrentIndex(sidx)
        if self._archive and self._start_edit is not None and self._end_edit is not None:
            self._start_edit.setDateTime(_utc_fields(t_start_epoch))
            self._end_edit.setDateTime(_utc_fields(t_end_epoch))

    # ------------------------------------------------------------------
    # Agent dropdown (registry-driven)
    # ------------------------------------------------------------------
    def _populate_agent_combo(self) -> None:
        """Fill the combo from the registry; disable unavailable / (archive)
        learning agents with an explanatory item tooltip."""
        for agent_id in sorted(AGENTS):
            label, enabled, reason = self._agent_item_state(agent_id)
            self._agent_combo.addItem(label, userData=agent_id)
            idx = self._agent_combo.count() - 1
            if not enabled:
                self._disable_combo_item(self._agent_combo, idx, reason)
        # Select the first ENABLED agent so the dialog opens on a usable one.
        for i in range(self._agent_combo.count()):
            if self._combo_item_enabled(self._agent_combo, i):
                self._agent_combo.setCurrentIndex(i)
                break

    def _agent_item_state(self, agent_id: str) -> tuple[str, bool, str]:
        """``(display label, enabled, disabled-reason)`` for an agent id."""
        agent = _construct_agent(agent_id, {})
        name = agent.name if agent is not None else agent_id
        available = self._availability.get(agent_id, True)
        requires_fit = bool(agent.requires_fit) if agent is not None else False
        if not available:
            return (
                f"{name} (needs 'ai' extra)",
                False,
                "Install the 'ai' extra to enable this agent",
            )
        if self._archive and requires_fit:
            return f"{name} (live only)", False, _FIT_ARCHIVE_NOTE
        return name, True, ""

    @staticmethod
    def _disable_combo_item(combo: QComboBox, index: int, reason: str) -> None:
        """Grey out a combo item (still visible, not selectable)."""
        model = combo.model()
        item = model.item(index) if hasattr(model, "item") else None
        if isinstance(item, QStandardItem):
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            if reason:
                item.setToolTip(reason)

    @staticmethod
    def _combo_item_enabled(combo: QComboBox, index: int) -> bool:
        model = combo.model()
        item = model.item(index) if hasattr(model, "item") else None
        if isinstance(item, QStandardItem):
            return bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)
        return True

    def _current_agent_id(self) -> str:
        data = self._agent_combo.currentData()
        return str(data) if data else next(iter(sorted(AGENTS)))

    # ------------------------------------------------------------------
    # Dynamic per-agent parameter fields
    # ------------------------------------------------------------------
    def _rebuild_param_fields(self) -> None:
        """Tear down the previous agent's field rows and build the current
        agent's, driven entirely by its :meth:`engage_params`."""
        # Remove old dynamic rows (each was inserted at the params anchor).
        for name, widget in list(self._param_widgets.items()):
            self._form.removeRow(widget)
            self._param_widgets.pop(name, None)
        self._param_specs.clear()

        agent = _construct_agent(self._current_agent_id(), {})
        specs = agent.engage_params() if agent is not None else []
        insert_at = self._params_anchor_row
        for spec in specs:
            widget = self._make_param_widget(spec)
            self._form.insertRow(insert_at, f"{spec.label}:", widget)
            insert_at += 1
            self._param_widgets[spec.name] = widget
            self._param_specs[spec.name] = spec

    def _make_param_widget(self, spec: AgentParam) -> QWidget:
        """Build the widget for one :class:`AgentParam` and wire it to the
        banner refresh (a field change can flip the domain verdict)."""
        if spec.kind == "choice":
            combo = QComboBox(self)
            for choice in spec.choices:
                combo.addItem(choice, userData=choice)
            idx = combo.findData(spec.default)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            combo.currentIndexChanged.connect(self._refresh_banner)
            return combo
        if spec.kind == "float":
            dspin = QDoubleSpinBox(self)
            dspin.setRange(spec.minimum, spec.maximum)
            dspin.setSingleStep(spec.step)
            dspin.setDecimals(spec.decimals)
            dspin.setValue(_as_float(spec.default))
            dspin.valueChanged.connect(self._refresh_banner)
            return dspin
        if spec.kind == "int":
            ispin = QSpinBox(self)
            ispin.setRange(int(spec.minimum), int(spec.maximum))
            ispin.setSingleStep(int(spec.step))
            ispin.setValue(int(_as_float(spec.default)))
            ispin.valueChanged.connect(self._refresh_banner)
            return ispin
        # text
        edit = QLineEdit(str(spec.default), self)
        edit.textChanged.connect(self._refresh_banner)
        return edit

    def _collect_agent_kwargs(self) -> dict[str, object]:
        """Read the dynamic field widgets into a ``{param.name: value}`` dict."""
        out: dict[str, object] = {}
        for name, widget in self._param_widgets.items():
            spec = self._param_specs.get(name)
            if spec is None:
                continue
            if isinstance(widget, QComboBox):
                out[name] = str(widget.currentData() or spec.default)
            elif isinstance(widget, QDoubleSpinBox):
                out[name] = float(widget.value())
            elif isinstance(widget, QSpinBox):
                out[name] = int(widget.value())
            elif isinstance(widget, QLineEdit):
                text = widget.text().strip()
                out[name] = text or str(spec.default)
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _make_datetime_edit(self, value: QDateTime) -> QDateTimeEdit:
        # The displayed value is interpreted as wall-clock UTC. We do NOT
        # call the (deprecated) QDateTimeEdit.setTimeSpec; instead the
        # read path stamps the UTC spec on the returned QDateTime so the
        # entered fields are taken as UTC with no local-offset shift.
        edit = QDateTimeEdit(self)
        edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        edit.setCalendarPopup(True)
        edit.setDateTime(value)
        return edit

    @staticmethod
    def _iso_of(edit: QDateTimeEdit) -> str:
        """ISO-8601 UTC string for the entered datetime (e.g.
        ``2026-06-01T12:00:00Z``). The entered wall-clock fields are
        already UTC (see the prefill), so we format them verbatim with a
        literal ``Z`` — no timezone conversion, no deprecated setTimeSpec."""
        return edit.dateTime().toString("yyyy-MM-dd'T'HH:mm:ss'Z'")

    def _current_device(self) -> str:
        return str(self._device_combo.currentData() or "")

    def _stations_for(self, device: str) -> dict[str, dict[str, str]]:
        """Group the device's NSLCs by station key into component maps."""
        stations: dict[str, dict[str, str]] = {}
        for nslc in self._streams_by_device.get(device, []):
            station = _station_of(nslc)
            orientation = _orientation_of(nslc)
            # Key by the orientation letter when present, else the full NSLC
            # (so a non-oriented channel still groups under its station).
            comp = orientation or nslc
            stations.setdefault(station, {})[comp] = nslc
        return stations

    def _on_agent_changed(self, *_args: object) -> None:
        """Selected agent changed: rebuild its fields, banner and fit note."""
        self._rebuild_param_fields()
        self._refresh_fit_note()
        self._refresh_banner()
        self._refresh_install_hint()

    def _on_device_changed(self, *_args: object) -> None:
        device = self._current_device()
        self._station_combo.blockSignals(True)
        self._station_combo.clear()
        for station in sorted(self._stations_for(device)):
            self._station_combo.addItem(station, userData=station)
        self._station_combo.blockSignals(False)
        self._on_station_changed()

    def _on_station_changed(self, *_args: object) -> None:
        device = self._current_device()
        station = str(self._station_combo.currentData() or "")
        comp_map = self._stations_for(device).get(station, {})
        # Keep only real Z/N/E orientation letters in the engaged group.
        self._group = {c: n for c, n in comp_map.items() if c in _COMPONENT_ORDER}
        if self._group:
            text = ", ".join(f"{c}={self._group[c]}" for c in _COMPONENT_ORDER if c in self._group)
        else:
            text = "(no Z/N/E components detected for this station)"
        self._components_label.setText(text)
        self._refresh_banner()

    def _refresh_fit_note(self) -> None:
        """Show the learning-agent note in live mode for a requires_fit agent."""
        agent = _construct_agent(self._current_agent_id(), {})
        requires_fit = bool(agent.requires_fit) if agent is not None else False
        if requires_fit and not self._archive:
            self._fit_note.setText(_FIT_LIVE_NOTE)
            self._fit_note.setVisible(True)
        else:
            self._fit_note.setVisible(False)

    def _refresh_install_hint(self) -> None:
        """Show an install hint when the selected agent's extra is missing."""
        agent_id = self._current_agent_id()
        available = self._availability.get(agent_id, True)
        if available:
            self._hint.setVisible(False)
            return
        self._hint.setText("Install the 'ai' extra to enable this agent (uv sync --extra ai)")
        self._hint.setVisible(True)

    def _refresh_banner(self, *_args: object) -> None:
        check = self._compute_check()
        self._banner.setText(check.message)
        self._banner.setStyleSheet(_BANNER_STYLES.get(check.severity, ""))

    def _compute_check(self) -> DomainCheck:
        if not self._group:
            return DomainCheck(
                Severity.LIKELY_INVALID,
                "No Z/N/E components selected — nothing to run the agent on.",
                ("no_streams",),
            )
        n_components = len(self._group)
        metas: list[StreamMeta] = []
        for nslc in self._group.values():
            try:
                fs = float(self._stream_fs(self._current_device(), nslc))
            except Exception:
                fs = 0.0
            metas.append(stream_meta_from_nslc(nslc, fs, n_components))
        spec = self._domain_spec()
        return compatibility(metas, spec)

    def _domain_spec(self) -> DomainSpec:
        """Build the SELECTED agent's domain spec (torch-free construction).

        Constructs the chosen agent with the current field values so the
        banner reflects the real engagement parameters; falls back to a
        neutral OK spec if construction fails mid-edit.
        """
        agent = _construct_agent(self._current_agent_id(), self._collect_agent_kwargs())
        if agent is None:
            # Neutral spec → OK banner; construction will be retried on the
            # next field change.
            return DomainSpec(
                expected_instrument="any waveform",
                expected_band_hz=(0.0, 0.0),
                expected_event_type="(agent unavailable)",
                trained_sampling_rate=0.0,
                required_components=1,
                allow_single_component=True,
                instrument_agnostic=True,
                rate_agnostic=True,
            )
        return agent.domain_spec

    # ----- test-only accessors -----
    def _banner_for_test(self) -> QLabel:
        return self._banner

    def _datetime_edits_for_test(self) -> tuple[QDateTimeEdit | None, QDateTimeEdit | None]:
        return self._start_edit, self._end_edit

    def _set_device_for_test(self, device: str) -> None:
        idx = self._device_combo.findData(device)
        if idx >= 0:
            self._device_combo.setCurrentIndex(idx)

    def _agent_ids_for_test(self) -> list[str]:
        return [str(self._agent_combo.itemData(i)) for i in range(self._agent_combo.count())]

    def _select_agent_for_test(self, agent_id: str) -> None:
        idx = self._agent_combo.findData(agent_id)
        if idx >= 0:
            self._agent_combo.setCurrentIndex(idx)

    def _param_field_names_for_test(self) -> set[str]:
        return set(self._param_widgets)

    def _agent_item_enabled_for_test(self, agent_id: str) -> bool:
        idx = self._agent_combo.findData(agent_id)
        return idx >= 0 and self._combo_item_enabled(self._agent_combo, idx)

    def _fit_note_visible_for_test(self) -> bool:
        # The dialog is never shown in tests, so a child's isVisible() is
        # False regardless; assert the intended visibility state instead
        # (isVisibleTo(parent) reflects the setVisible call, not show state).
        return self._fit_note.isVisibleTo(self)


__all__ = ["EngageAgentDialog"]
