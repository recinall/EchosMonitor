"""Tabbed full-size spectrogram view, one tab per active stream.

The dock is a sibling of the inline spectrogram panes inside
``LiveStack``: same data source (the engine's
:attr:`spectrogramColumnReady` signal), larger surface for detailed
inspection. Tabs are created on first :meth:`add_stream` call and
removed on :meth:`remove_stream`; the dock itself is constructed
empty.

Per-STREAM vs per-DEVICE (intentional, see
:class:`~seedlink_dashboard.gui.widgets.live_tabs.LiveTabs`): this dock
is **per-STREAM** — one full-size waterfall per tab for detailed
single-channel inspection. The Live panel's ``LiveTabs`` is
**per-DEVICE** — each tab shows all of one device's streams. The two
tab granularities serve different workflows and are deliberately
distinct, not an inconsistency.

Threading: all public methods MUST be called from the GUI thread.
``on_column`` is the slot the engine signal lands on (queued).
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from seedlink_dashboard.gui.widgets.spectrogram_view import SpectrogramView, _epoch_from


class SpectrogramDock(QWidget):
    """Tabbed container of full-size :class:`SpectrogramView` widgets.

    Args:
        parent: Owning widget. Typically a ``QDockWidget`` body.
        window_seconds: Window length passed to each constructed view.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        window_seconds: float = 600.0,
    ) -> None:
        super().__init__(parent)
        self._window_seconds = float(window_seconds)
        self._views: dict[tuple[str, str], SpectrogramView] = {}

        self._tabs = QTabWidget(self)
        self._tabs.setMovable(True)
        self._tabs.setTabsClosable(False)

        self._empty_label = QLabel(
            "No streams yet — connect a device and the spectrogram "
            "tabs will appear here automatically."
        )
        self._empty_label.setObjectName("SpectrogramDockEmpty")
        self._empty_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._empty_label)
        layout.addWidget(self._tabs, stretch=1)
        self._tabs.setVisible(False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def add_stream(self, device_name: str, nslc: str, fs: float) -> None:
        """Create a tab for ``(device, nslc)`` if one doesn't exist yet.

        Idempotent: a second call with the same ``(device, nslc)`` is
        a no-op (keeps the existing tab and its accumulated history).
        """
        key = (device_name, nslc)
        if key in self._views:
            return
        view = SpectrogramView(
            window_seconds=self._window_seconds,
            fs=fs,
            label=f"{device_name} · {nslc}",
            time_axis=True,
            parent=self._tabs,
        )
        self._views[key] = view
        self._tabs.addTab(view, f"{device_name} · {nslc}")
        self._tabs.setVisible(True)
        self._empty_label.setVisible(False)

    def remove_stream(self, device_name: str, nslc: str) -> None:
        key = (device_name, nslc)
        view = self._views.pop(key, None)
        if view is None:
            return
        index = self._tabs.indexOf(view)
        if index >= 0:
            self._tabs.removeTab(index)
        view.deleteLater()
        if not self._views:
            self._tabs.setVisible(False)
            self._empty_label.setVisible(True)

    def remove_device(self, device_name: str) -> None:
        """Drop every tab belonging to one device. Called on
        ``_stop_device`` so a closed device's stale tabs disappear."""
        targets = [k for k in self._views if k[0] == device_name]
        for key in targets:
            self.remove_stream(key[0], key[1])

    def update_meta(self, device_name: str, nslc: str, fs: float) -> None:
        """Forward an fs change to the relevant tab. Called on
        ``processedStreamMeta``: a chain hot-reload may shift the
        stream's effective fs (decimation), and the tab needs to
        update its title + drop accumulated state."""
        view = self._views.get((device_name, nslc))
        if view is None:
            return
        view.update_meta(fs=fs)

    @Slot(str, str, object, object, object)
    def on_column(
        self,
        device_name: str,
        nslc: str,
        column: object,
        freqs: object,
        t_end: object,
    ) -> None:
        """Slot for ``StreamingEngine.spectrogramColumnReady``."""
        if not isinstance(column, np.ndarray) or not isinstance(freqs, np.ndarray):
            return
        view = self._views.get((device_name, nslc))
        if view is None:
            # No tab yet — silently drop. The dock's add_stream is
            # called by MainWindow on newStreamSeen, so the typical
            # ordering is "tab created, then column arrives". A column
            # that lands first is harmless to ignore.
            return
        view.add_column(column, freqs, t_end=_epoch_from(t_end))

    # ------------------------------------------------------------------
    # Detection markers (M8 C2)
    # ------------------------------------------------------------------
    def add_detection_marker(
        self,
        device_name: str,
        nslc: str,
        det_id: int,
        t_on: float,
    ) -> None:
        """Add an onset marker to the matching stream's wall-clock view."""
        view = self._views.get((device_name, nslc))
        if view is not None:
            view.add_detection_marker(det_id, t_on)

    def set_markers_visible(self, visible: bool) -> None:
        for view in self._views.values():
            view.set_markers_visible(visible)
