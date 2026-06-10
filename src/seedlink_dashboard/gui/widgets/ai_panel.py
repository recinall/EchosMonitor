"""AI dock contents — the active-engagement panel (M9 Stage B GUI).

Shows the single active AI engagement: agent name, a colour-coded state
chip (LOADING amber / RUNNING green / IDLE grey / ERROR red), the engaged
device + component group, and throughput (windows done, last inference ms,
drops). Hosts the "Engage agent..." and "Disengage" buttons.

The panel is a thin view over :class:`~seedlink_dashboard.core.ai_engine.
AIEngine`: it connects the engine's signals (same-thread Auto connections —
the engine lives on the GUI thread) and refreshes from
:meth:`AIEngine.active_engagement`. It never runs inference and never
touches the data path.

When the ``ai`` extra is absent the panel shows a prominent install notice
and the dialog's agent control is disabled — but the panel still accepts
engagements initiated programmatically by the backend (the fake-agent test
path), because it refreshes purely from the engine's signals/snapshot.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from seedlink_dashboard.ai.agents import seisbench_available
from seedlink_dashboard.core.ai_engine import AgentState, AIEngine

_log = structlog.get_logger(__name__)

# State-chip colours by agent state. Mirrors the connection-state palette
# convention used elsewhere (grey idle, green running, amber loading, red
# error).
_STATE_CHIP_STYLES: dict[AgentState, str] = {
    AgentState.IDLE: "background: #555; color: #ddd;",
    AgentState.LOADING: "background: #b58900; color: #1a1a1a;",
    AgentState.RUNNING: "background: #2e7d32; color: #eaffea;",
    AgentState.STOPPING: "background: #b58900; color: #1a1a1a;",
    AgentState.ERROR: "background: #b71c1c; color: #ffecec;",
}

_NO_ENGAGEMENT_HINT = (
    "No AI agent engaged. Click “Engage agent…” to run a phase picker on a live channel group."
)
_NO_EXTRA_NOTICE = "Install the 'ai' extra to enable AI agents (uv sync --extra ai)"


class AiPanel(QWidget):
    """Active-engagement view + engage/disengage controls for the AI dock.

    Args:
        engine: the live streaming engine (source of ``live_streams`` /
            ``read_recent`` for the engage dialog).
        ai_engine: the AI engagement engine whose signals drive this panel.
        parent: standard Qt parent.
    """

    def __init__(
        self,
        engine: object,
        ai_engine: AIEngine,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._ai_engine = ai_engine
        # Callback the host (MainWindow) installs to open the engage dialog.
        # Kept injectable so the panel stays testable without the dialog /
        # a live engine.
        self._engage_request: Callable[[], None] | None = None
        # Callback the host installs to open the "run on past data" dialog.
        self._archive_request: Callable[[], None] | None = None

        # ---- header: agent name + state chip ----
        self._agent_label = QLabel("—", self)
        self._agent_label.setObjectName("AiAgentName")
        self._agent_label.setStyleSheet("QLabel#AiAgentName { font-weight: bold; }")
        self._state_chip = QLabel("idle", self)
        self._state_chip.setObjectName("AiStateChip")
        self._state_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)

        header = QHBoxLayout()
        header.addWidget(self._agent_label, stretch=1)
        header.addWidget(self._state_chip)

        # ---- detail: device/components + throughput ----
        self._target_label = QLabel("", self)
        self._target_label.setWordWrap(True)
        self._throughput_label = QLabel("", self)
        self._throughput_label.setObjectName("AiThroughput")
        self._throughput_label.setStyleSheet("QLabel#AiThroughput { color: #aaa; }")
        self._error_label = QLabel("", self)
        self._error_label.setObjectName("AiError")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("QLabel#AiError { color: #e57373; }")

        self._hint_label = QLabel(_NO_ENGAGEMENT_HINT, self)
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet("QLabel { color: #888; font-style: italic; }")

        # ---- install notice (only without the ai extra) ----
        self._notice_label = QLabel(_NO_EXTRA_NOTICE, self)
        self._notice_label.setObjectName("AiInstallNotice")
        self._notice_label.setWordWrap(True)
        self._notice_label.setStyleSheet(
            "QLabel#AiInstallNotice { background: #7a5c00; color: #fff7e0; padding: 6px; }"
        )
        self._notice_label.setVisible(not seisbench_available())

        # ---- buttons ----
        self._engage_btn = QPushButton("Engage agent…", self)
        self._engage_btn.clicked.connect(self._on_engage_clicked)
        self._archive_btn = QPushButton("Run on past data…", self)
        self._archive_btn.clicked.connect(self._on_archive_clicked)
        self._disengage_btn = QPushButton("Disengage", self)
        self._disengage_btn.setEnabled(False)
        self._disengage_btn.clicked.connect(self._on_disengage_clicked)
        buttons = QHBoxLayout()
        buttons.addWidget(self._engage_btn)
        buttons.addWidget(self._archive_btn)
        buttons.addWidget(self._disengage_btn)
        buttons.addStretch(1)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addLayout(header)
        root.addWidget(self._target_label)
        root.addWidget(self._throughput_label)
        root.addWidget(self._error_label)
        root.addWidget(self._hint_label)
        root.addWidget(self._notice_label)
        root.addLayout(buttons)
        root.addStretch(1)

        # Same-thread Auto connections — AIEngine lives on the GUI thread.
        self._ai_engine.agentEngaged.connect(self._on_engaged)
        self._ai_engine.agentStateChanged.connect(self._on_state_changed)
        self._ai_engine.agentBackpressure.connect(self._on_backpressure)

        self._refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_engage_request_handler(self, handler: Callable[[], None]) -> None:
        """Install the callback that opens the engage dialog (MainWindow)."""
        self._engage_request = handler

    def set_archive_request_handler(self, handler: Callable[[], None]) -> None:
        """Install the callback that opens the "run on past data" dialog."""
        self._archive_request = handler

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    @Slot(str, object)
    def _on_engaged(self, _engagement_id: str, _summary: object) -> None:
        self._refresh()

    @Slot(str, str)
    def _on_state_changed(self, _engagement_id: str, _state_value: str) -> None:
        self._refresh()

    @Slot(str, int)
    def _on_backpressure(self, _engagement_id: str, _dropped: int) -> None:
        self._refresh()

    def _on_engage_clicked(self) -> None:
        if self._engage_request is not None:
            self._engage_request()
        else:
            _log.debug("ai_panel_engage_no_handler")

    def _on_archive_clicked(self) -> None:
        if self._archive_request is not None:
            self._archive_request()
        else:
            _log.debug("ai_panel_archive_no_handler")

    def _on_disengage_clicked(self) -> None:
        summary = self._ai_engine.active_engagement()
        if summary is not None:
            self._ai_engine.disengage(summary.engagement_id)
        self._refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        summary = self._ai_engine.active_engagement()
        engaged = summary is not None and summary.state is not AgentState.IDLE
        self._hint_label.setVisible(not engaged)
        self._target_label.setVisible(engaged)
        self._throughput_label.setVisible(engaged)
        self._disengage_btn.setEnabled(engaged)
        # Only one engagement at a time: both launch buttons are disabled
        # while an engagement (live or archive) is active.
        self._engage_btn.setEnabled(not engaged)
        self._archive_btn.setEnabled(not engaged)

        if summary is None:
            self._agent_label.setText("—")
            self._set_state_chip(AgentState.IDLE)
            self._target_label.setText("")
            self._throughput_label.setText("")
            self._error_label.setText("")
            self._error_label.setVisible(False)
            return

        self._agent_label.setText(summary.agent_name)
        self._set_state_chip(summary.state)
        comps = ", ".join(f"{c}={n}" for c, n in summary.nslc_by_component.items())
        self._target_label.setText(f"{summary.device}  ·  {comps}")
        self._throughput_label.setText(
            f"windows: {summary.windows_done}  ·  "
            f"last {summary.last_infer_ms:.0f} ms  ·  "
            f"drops: {summary.dropped}"
        )
        has_error = summary.state is AgentState.ERROR and bool(summary.last_error)
        self._error_label.setVisible(has_error)
        self._error_label.setText(summary.last_error if has_error else "")

    def _set_state_chip(self, state: AgentState) -> None:
        self._state_chip.setText(state.value)
        style = _STATE_CHIP_STYLES.get(state, _STATE_CHIP_STYLES[AgentState.IDLE])
        self._state_chip.setStyleSheet(f"QLabel#AiStateChip {{ {style} padding: 2px 8px; }}")

    # ----- test-only accessors -----
    def _state_chip_for_test(self) -> QLabel:
        return self._state_chip

    def _agent_label_for_test(self) -> QLabel:
        return self._agent_label

    def _disengage_btn_for_test(self) -> QPushButton:
        return self._disengage_btn

    def _archive_btn_for_test(self) -> QPushButton:
        return self._archive_btn


__all__ = ["AiPanel"]
