"""Vertical stack of device groups, each hosting one TracePlot per stream.

Layout (M3 part 1 — multi-device, M6 spectrogram pane):

    LiveStack (vertical QSplitter)
    ├── _DeviceGroup #1
    │     ├── header strip (name bold · state badge · "N/M" · "+K hidden"
    │     │                  · spec toggle)
    │     └── plots-splitter (vertical QSplitter of _StreamPair widgets)
    │           ├── _StreamPair (TracePlot + optional SpectrogramView)
    │           └── _StreamPair ...
    ├── _DeviceGroup #2
    │     └── ...

Streams are addressed by ``(device_name, nslc)`` tuples — two devices
publishing the same NSLC each get their own plot. The widget pulls the
device → has-DSP-chain mapping from the project configuration so that
streams whose device declares a non-empty ``dsp_chain`` get a stacked
TracePlot (raw on top, filtered below) and others get the single-plot
rendering.

Visibility is capped at ``cfg.ui.max_visible_plots`` across all devices
(default 8). Streams beyond the cap stay constructed and receive data;
they're just hidden. Most-recently-seen wins — when a new stream pushes
the count over the cap, the oldest visible plot is the one hidden. A
device group whose total exceeds its visible count grows a "+K hidden"
indicator on its header. UI to toggle individual streams is M3 part 2.

M6 spectrograms (per-device toggle):
A SpectrogramView is constructed below each TracePlot inside a per-stream
mini-splitter. Visibility is per-device, controlled by a toolbutton on
the device group's header. Default ON for devices whose ``dsp_chain``
contains a bandpass / highpass / lowpass (the user is filtering, so
they care about frequency content); OFF otherwise. Persisted under the
QSettings group ``Spectrograms/<device_name>`` (boolean).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.models import ConnState
from echosmonitor.gui.widgets.spectrogram_view import SpectrogramView, _epoch_from
from echosmonitor.gui.widgets.trace_plot import TracePlot

if TYPE_CHECKING:
    from echosmonitor.config import RootConfig

_DEFAULT_MAX_VISIBLE_PLOTS = 8
# Display-only peak-decimation cap (rule 11) used when no cfg is supplied
# (test harnesses). Mirrors UiConfig.max_display_rate_hz's default.
_DEFAULT_MAX_DISPLAY_RATE_HZ = 250

# Stage-1 default: spectrograms ON for devices whose chain contains any
# of these stage types (the frequency content matters to the user).
# Detrend / decimation / sta_lta alone does not flip the default.
_FREQ_FILTER_TYPES = frozenset({"bandpass", "highpass", "lowpass", "notch"})

# Detector-only stage types: they emit triggers, not a displayable filtered
# waveform (STA/LTA passes its input through unchanged). A chain made up
# solely of these produces no meaningful "processed" trace, so the stacked
# raw+filtered plot would only show an empty (or duplicate) lower pane. The
# second plot is therefore shown only when the chain has at least one
# waveform-producing stage (detrend, taper, a filter, decimation).
_DETECTOR_STAGE_TYPES = frozenset({"sta_lta"})

_SPEC_TOGGLE_STYLE = "QToolButton#DeviceGroupSpec { font-size: 10px; padding: 0 4px; }"

# Mini-splitter stretch factors for the per-stream TracePlot / SpectrogramView
# pair. The trace dominates; the spectrogram is half its height by default.
_PAIR_TRACE_STRETCH = 7
_PAIR_SPEC_STRETCH = 3

_STATE_COLORS: dict[int, str] = {
    int(ConnState.DISCONNECTED): "#888888",
    int(ConnState.CONNECTING): "#d9a441",
    int(ConnState.CONNECTED): "#3aa371",
    int(ConnState.RECONNECTING): "#d9a441",
    # Distinct darker amber for the backoff-sleep state. Lets the operator
    # tell at a glance whether a struggling device is actively trying or
    # waiting between tries — the same hex would conflate them.
    int(ConnState.WAITING_RETRY): "#c98f2a",
    int(ConnState.STOPPED): "#666666",
}

_BADGE_BASE_STYLE = "QLabel#DeviceGroupBadge { font-size: 10px; padding: 1px 4px; }"
_HIDDEN_INDICATOR_STYLE = (
    "QLabel#DeviceGroupHidden { color: #888; font-size: 10px; padding: 0 6px; }"
)
_COUNTER_STYLE = "QLabel#DeviceGroupCounter { color: #888; font-size: 10px; }"
_NAME_STYLE = "QLabel#DeviceGroupName { font-weight: bold; }"


class _DeviceGroup(QWidget):
    """One device's section of the LiveStack.

    Top: a thin header strip with the device name (bold), connection-state
    badge, visible/total counter, and an optional "+K hidden" indicator.
    Bottom: a vertical ``QSplitter`` of ``TracePlot`` widgets for that
    device's streams.

    The counters are driven by ``LiveStack`` (which knows the global
    visibility cap); ``_DeviceGroup`` is otherwise passive.
    """

    def __init__(
        self,
        device_name: str,
        parent: QWidget | None = None,
        *,
        spec_default_on: bool = False,
    ) -> None:
        super().__init__(parent)
        self._device_name = device_name
        self._state_int = int(ConnState.DISCONNECTED)
        self._spec_panes: list[SpectrogramView] = []
        # Wallets / per-device persistence — read once on construction;
        # writes happen on user toggle so a power-cut state is the
        # last user-confirmed state.
        self._spec_visible = spec_default_on

        self._name_label = QLabel(device_name)
        self._name_label.setObjectName("DeviceGroupName")
        self._name_label.setStyleSheet(_NAME_STYLE)

        self._badge = QLabel(ConnState.DISCONNECTED.name)
        self._badge.setObjectName("DeviceGroupBadge")

        self._counter = QLabel("0/0")
        self._counter.setObjectName("DeviceGroupCounter")
        self._counter.setStyleSheet(_COUNTER_STYLE)

        self._hidden_indicator = QLabel("")
        self._hidden_indicator.setObjectName("DeviceGroupHidden")
        self._hidden_indicator.setStyleSheet(_HIDDEN_INDICATOR_STYLE)

        # M6 per-device spectrogram visibility toggle. Bulk action: one
        # click hides every spectrogram pane under this device.
        self._spec_toggle = QToolButton(self)
        self._spec_toggle.setObjectName("DeviceGroupSpec")
        self._spec_toggle.setStyleSheet(_SPEC_TOGGLE_STYLE)
        self._spec_toggle.setText("spec")
        self._spec_toggle.setCheckable(True)
        self._spec_toggle.setChecked(self._spec_visible)
        self._spec_toggle.setToolTip(
            "Show/hide the spectrogram pane under each stream of this device. "
            "Default ON when the device's DSP chain contains a band/high/low/"
            "notch filter."
        )
        self._spec_toggle.toggled.connect(self._on_spec_toggled)

        header = QFrame(self)
        header.setObjectName("DeviceGroupHeader")
        header.setFrameShape(QFrame.Shape.NoFrame)
        h = QHBoxLayout(header)
        h.setContentsMargins(6, 2, 6, 2)
        h.setSpacing(8)
        h.addWidget(self._name_label)
        h.addWidget(self._badge)
        h.addStretch(1)
        h.addWidget(self._hidden_indicator)
        h.addWidget(self._counter)
        h.addWidget(self._spec_toggle)

        self._plots_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._plots_splitter.setObjectName("DeviceGroupPlots")
        self._plots_splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self._plots_splitter, stretch=1)

        self._apply_badge_style()

    def add_plot(self, plot: QWidget) -> None:
        self._plots_splitter.addWidget(plot)

    def add_stream_pair(self, plot: TracePlot, spec: SpectrogramView) -> None:
        """Attach a stream's TracePlot + SpectrogramView pair.

        The two widgets share a vertical mini-splitter so the user can
        rebalance heights at the per-stream level. The mini-splitter is
        what the device-group's plots-splitter actually contains.
        """
        pair = QSplitter(Qt.Orientation.Vertical, self)
        pair.setObjectName("DeviceGroupStreamPair")
        pair.setChildrenCollapsible(False)
        pair.addWidget(plot)
        pair.addWidget(spec)
        pair.setStretchFactor(0, _PAIR_TRACE_STRETCH)
        pair.setStretchFactor(1, _PAIR_SPEC_STRETCH)
        self._spec_panes.append(spec)
        spec.setVisible(self._spec_visible)
        self._plots_splitter.addWidget(pair)

    def set_spec_visible(self, visible: bool) -> None:
        if visible == self._spec_visible:
            return
        self._spec_visible = visible
        self._spec_toggle.blockSignals(True)
        self._spec_toggle.setChecked(visible)
        self._spec_toggle.blockSignals(False)
        for pane in self._spec_panes:
            pane.setVisible(visible)

    def is_spec_visible(self) -> bool:
        return self._spec_visible

    def _on_spec_toggled(self, checked: bool) -> None:
        self._spec_visible = checked
        for pane in self._spec_panes:
            pane.setVisible(checked)
        # Persist the user's choice. Format follows the M4 dock-state
        # pattern (QSettings under the existing org/app keys).
        QSettings().setValue(f"Spectrograms/{self._device_name}", checked)

    def set_state(self, state_int: int) -> None:
        self._state_int = state_int
        try:
            self._badge.setText(ConnState(state_int).name)
        except ValueError:
            self._badge.setText(str(state_int))
        self._apply_badge_style()

    def update_counts(self, *, visible: int, hidden: int, total: int) -> None:
        self._counter.setText(f"{visible}/{total}")
        if hidden > 0:
            self._hidden_indicator.setText(f"+{hidden} hidden")
        else:
            self._hidden_indicator.setText("")

    def _apply_badge_style(self) -> None:
        color = _STATE_COLORS.get(self._state_int, "#888888")
        self._badge.setStyleSheet(
            _BADGE_BASE_STYLE + f" QLabel#DeviceGroupBadge {{ color: {color}; }}"
        )

    # Test-only accessors
    def _badge_text_for_test(self) -> str:
        return self._badge.text()

    def _counter_text_for_test(self) -> str:
        return self._counter.text()

    def _hidden_indicator_text_for_test(self) -> str:
        return self._hidden_indicator.text()


class LiveStack(QSplitter):
    """Vertical splitter that hosts one ``_DeviceGroup`` per device.

    Streams are keyed by ``(device_name, nslc)``. Plots beyond the global
    visibility cap are kept constructed (and continue to receive data)
    but are hidden until something newer ages out. The cap is taken from
    ``cfg.ui.max_visible_plots`` (default 8 across all devices).
    """

    def __init__(
        self,
        window_seconds: float,
        cfg: RootConfig | None = None,
        parent: QWidget | None = None,
        *,
        max_visible: int | None = None,
    ) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self._window_seconds = float(window_seconds)
        self._max_visible = _DEFAULT_MAX_VISIBLE_PLOTS
        self._max_display_rate_hz = _DEFAULT_MAX_DISPLAY_RATE_HZ
        # True for a device whose chain produces a displayable filtered
        # waveform (≥1 non-detector stage). Drives the stacked raw+filtered
        # plot: a detector-only (e.g. sta_lta) chain stays single, since its
        # "processed" output is not a meaningful trace.
        self._device_has_processed_view: dict[str, bool] = {}
        # M6: per-device default for the spectrogram visibility toggle.
        # ``True`` if any of the device's chain stages alters the
        # frequency content (the user explicitly cares about it). The
        # final visibility honours QSettings if the user has toggled
        # before, falling back to this default on first open.
        self._device_spec_default_on: dict[str, bool] = {}
        if cfg is not None:
            self._device_has_processed_view = {
                dev.name: any(
                    getattr(stage, "type", None) not in _DETECTOR_STAGE_TYPES
                    for stage in dev.dsp_chain
                )
                for dev in cfg.devices
            }
            self._device_spec_default_on = {
                dev.name: any(
                    getattr(stage, "type", None) in _FREQ_FILTER_TYPES for stage in dev.dsp_chain
                )
                for dev in cfg.devices
            }
            self._max_visible = int(cfg.ui.max_visible_plots)
            self._max_display_rate_hz = int(cfg.ui.max_display_rate_hz)
        # An explicit ``max_visible`` overrides the config-derived cap.
        # Used by the per-device tabs in :class:`LiveTabs`, which cap each
        # device at 8 streams independently of the global config cap. The
        # default ``None`` preserves today's config-driven behaviour.
        if max_visible is not None:
            self._max_visible = int(max_visible)
        # GUI render-rate gate (M7 Stage B3). Tracks the active flag so a
        # plot/spectrogram view constructed *after* a set_render_active(False)
        # call inherits the paused state rather than rendering at full rate.
        self._render_active = True
        # Per-stream USER visibility override driven by the chips toolbar
        # in :class:`LiveTabs`. A key present here is hidden regardless of
        # the recency cap. Empty by default → unchanged legacy behaviour.
        self._user_hidden: set[tuple[str, str]] = set()
        self._plots: dict[tuple[str, str], TracePlot] = {}
        # Per-stream spectrogram views, parented to their _DeviceGroup
        # via ``add_stream_pair``.
        self._spec_views: dict[tuple[str, str], SpectrogramView] = {}
        # Insertion order: most recent at the end. Drives recency-based
        # visibility when the cap is exceeded.
        self._order: list[tuple[str, str]] = []
        self._groups: dict[str, _DeviceGroup] = {}
        self.setChildrenCollapsible(False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def add_stream(self, device_name: str, nslc: str, fs: float) -> TracePlot:
        """Create (or return existing) TracePlot for ``(device_name, nslc)``.

        New streams are added to the bottom of their device group's
        plots-splitter; the global visibility cap is reapplied so the
        oldest plot ages out if necessary. A spectrogram pane is also
        constructed and attached as a sibling under the same per-stream
        mini-splitter; its visibility is controlled by the device-group
        spectrogram toggle.
        """
        key = (device_name, nslc)
        existing = self._plots.get(key)
        if existing is not None:
            return existing

        group = self._groups.get(device_name)
        if group is None:
            group = self._make_group(device_name)

        has_processed_view = bool(self._device_has_processed_view.get(device_name, False))
        plot = TracePlot(
            window_seconds=self._window_seconds,
            fs=fs,
            label=nslc,
            parent=group,
            mode="stacked" if has_processed_view else "single",
            max_display_rate_hz=self._max_display_rate_hz,
        )
        spec = SpectrogramView(
            window_seconds=self._window_seconds,
            fs=fs,
            label=nslc,
            parent=group,
        )
        self._plots[key] = plot
        self._spec_views[key] = spec
        self._order.append(key)
        group.add_stream_pair(plot, spec)
        # A stream added while the whole stack is render-paused (a hidden
        # tab) must inherit the paused state, not redraw at full rate.
        if not self._render_active:
            plot.set_render_active(False)
            spec.set_render_active(False)
        self._reapply_visibility()
        return plot

    def spec_view_for(self, device_name: str, nslc: str) -> SpectrogramView | None:
        return self._spec_views.get((device_name, nslc))

    def on_spectrogram_column(
        self,
        device_name: str,
        nslc: str,
        column: object,
        freqs: object,
        t_end: object,
    ) -> None:
        """Slot for ``StreamingEngine.spectrogramColumnReady``.

        Looks up the matching :class:`SpectrogramView` (if any) and
        forwards the column. Streams without a constructed view (the
        very first packet may race the new-stream wiring) are skipped
        silently — the next column lands on the now-existing view.
        """
        view = self._spec_views.get((device_name, nslc))
        if view is None:
            return
        if not isinstance(column, np.ndarray) or not isinstance(freqs, np.ndarray):
            return
        # The inline pane uses a column-index axis (time_axis=False), so
        # t_end is accepted but ignored; forwarded for call-site parity.
        view.add_column(column, freqs, t_end=_epoch_from(t_end))

    def update_processed_meta(self, device_name: str, nslc: str, fs_out: float) -> None:
        """Forward a chain hot-reload's new ``fs_out`` to the matching
        spectrogram view (if any). Drops the view's accumulated state so
        the new sample rate's frequency axis takes over immediately."""
        view = self._spec_views.get((device_name, nslc))
        if view is None:
            return
        view.update_meta(fs=fs_out)

    def _make_group(self, device_name: str) -> _DeviceGroup:
        # Device-default for spectrogram visibility, optionally
        # overridden by a stored user toggle from a previous session.
        default_on = bool(self._device_spec_default_on.get(device_name, False))
        stored = QSettings().value(f"Spectrograms/{device_name}")
        if isinstance(stored, bool):
            spec_on = stored
        elif isinstance(stored, str):  # QSettings INI format stores as string
            spec_on = stored.lower() in ("1", "true", "yes")
        else:
            spec_on = default_on
        group = _DeviceGroup(device_name, parent=self, spec_default_on=spec_on)
        self._groups[device_name] = group
        self.addWidget(group)
        return group

    def has_stream(self, device_name: str, nslc: str) -> bool:
        return (device_name, nslc) in self._plots

    def plot_for(self, device_name: str, nslc: str) -> TracePlot | None:
        return self._plots.get((device_name, nslc))

    def set_markers_visible(self, visible: bool) -> None:
        """Toggle detection markers on every trace in this stack (M8 C1
        global View-menu toggle)."""
        for plot in self._plots.values():
            plot.set_markers_visible(visible)

    def visible_count(self) -> int:
        # ``isVisible()`` requires the widget tree to be shown, which
        # makes it useless in unit tests that never call ``show()``.
        # The intent is to count plots not explicitly hidden by the
        # cap, which ``isHidden()`` answers correctly even off-screen.
        return sum(1 for plot in self._plots.values() if not plot.isHidden())

    def set_drop_count(self, device_name: str, nslc: str, count: int) -> None:
        plot = self._plots.get((device_name, nslc))
        if plot is not None:
            plot.set_drop_count(count)

    def set_device_state(self, device_name: str, state_int: int) -> None:
        """Update the device group's state badge. Creates the group if a
        device announces its state before any of its streams have arrived
        — keeps the multi-device UI populated during CONNECTING."""
        group = self._groups.get(device_name)
        if group is None:
            group = self._make_group(device_name)
        group.set_state(state_int)

    def set_render_active(self, active: bool) -> None:
        """Toggle full-rate rendering for every child plot + spectrogram.

        Propagated by :class:`LiveTabs` on tab change: only the visible
        tab's stack renders at full rate. Hidden tabs keep rolling their
        buffers (cheap) but skip the costly ``setData`` / ``setImage``
        calls. GUI render-rate only (CLAUDE.md rule 8).
        """
        if active == self._render_active:
            return
        self._render_active = active
        for plot in self._plots.values():
            plot.set_render_active(active)
        for spec in self._spec_views.values():
            spec.set_render_active(active)

    def set_stream_user_visible(self, device_name: str, nslc: str, visible: bool) -> None:
        """User-driven per-stream visibility override (chips toolbar).

        A stream marked not-visible stays hidden regardless of the
        recency cap; clearing the override returns it to cap-managed
        visibility. Only affects this stack — never sibling tabs.
        """
        key = (device_name, nslc)
        if key not in self._plots:
            return
        if visible:
            self._user_hidden.discard(key)
        else:
            self._user_hidden.add(key)
        self._reapply_visibility()

    def is_stream_user_visible(self, device_name: str, nslc: str) -> bool:
        """Whether ``(device, nslc)`` is NOT user-hidden. Default ``True``."""
        return (device_name, nslc) not in self._user_hidden

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _reapply_visibility(self) -> None:
        # A user-hidden stream (chips toolbar) never occupies a cap slot
        # and is always hidden. The recency cap applies only to the
        # streams the user has left visible, newest-first.
        cap = self._max_visible
        candidates = [key for key in self._order if key not in self._user_hidden]
        n = len(candidates)
        visible_keys = set(candidates) if n <= cap else set(candidates[-cap:])
        for key, plot in self._plots.items():
            plot.setVisible(key in visible_keys)

        per_device_total: dict[str, int] = {}
        per_device_visible: dict[str, int] = {}
        for dev, _nslc in self._order:
            per_device_total[dev] = per_device_total.get(dev, 0) + 1
        for dev, _nslc in visible_keys:
            per_device_visible[dev] = per_device_visible.get(dev, 0) + 1
        for dev, group in self._groups.items():
            total = per_device_total.get(dev, 0)
            visible = per_device_visible.get(dev, 0)
            hidden = total - visible
            group.update_counts(visible=visible, hidden=hidden, total=total)

    # ------------------------------------------------------------------
    # Test-only accessors
    # ------------------------------------------------------------------
    def _device_group_for_test(self, device_name: str) -> _DeviceGroup | None:
        return self._groups.get(device_name)

    def _max_visible_for_test(self) -> int:
        return self._max_visible
