"""Interactive DSP chain editor with live preview.

Modal dialog opened from the DeviceDialog "Edit chain..." button (and
the DevicePanel double-click). Layout:

* Top half — stage list editor:
    - Left: "Add stage..." palette (one button per live-allowed stage
      type; Taper is intentionally absent).
    - Middle: ordered ``QListWidget`` of the current chain. Drag-and-
      drop reorders; ``Delete`` removes the selected row.
    - Right: the selected row's per-stage parameter form (see
      :mod:`gui.widgets.stage_forms`). Edits live-update the row's
      summary and the preview.

* Bottom half — live preview:
    - Source-stream combo (only this device's announced streams).
    - Window-length combo (10 / 30 / 60 s, capped at 60).
    - Two stacked tiny ``pyqtgraph`` plots: raw snapshot on top,
      processed snapshot on bottom.
    - A small inline :class:`SpectrogramView` of the processed signal.
    - A red banner pinned above the plots when the current chain is
      invalid (``ConfigError`` from the factory or any stage form
      flagged it).

Threading: this dialog runs entirely on the GUI thread.
``MAX_PREVIEW_SECONDS = 60`` caps the input snapshot to <= 60 s x
``stream_fs`` samples; running an 8-stage chain on 12 000 samples
costs ~10-20 ms on a modern laptop, well within one 50-ms GUI frame.
Re-computes are debounced 200 ms so dragging a spinner does not
re-run on every event. This is the only inline DSP path in the
codebase and is documented in :file:`docs/ARCHITECTURE.md`.

Persistence: the editor itself does NOT call :meth:`ConfigStore.update_device`.
It returns the new ``list[DspStageConfig]`` to the caller (typically
:class:`DeviceForm`), which integrates it into its own ``to_config``
+ OK-button flow. ``Apply`` emits :attr:`stagesApplied(stages)`
without closing the dialog so the caller can mirror the change into
its working state and the user can keep editing.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from obspy import UTCDateTime
from PySide6.QtCore import QPoint, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from seedlink_dashboard.core.exceptions import ConfigError
from seedlink_dashboard.core.models import DEVICE_KEY_SEP, StreamID
from seedlink_dashboard.dsp.factory import build_chain
from seedlink_dashboard.dsp.spectrogram import RollingSpectrogram
from seedlink_dashboard.gui.widgets.spectrogram_view import SpectrogramView
from seedlink_dashboard.gui.widgets.stage_forms import (
    default_stage_for,
    make_form,
    stage_summary,
)

if TYPE_CHECKING:
    from seedlink_dashboard.config.schema import DspStageConfig
    from seedlink_dashboard.core.streaming_engine import StreamingEngine
    from seedlink_dashboard.gui.widgets.stage_forms import _BaseStageForm


# Live-preview bound. 60 s x 200 Hz = 12 000 samples; through 8 stages
# the worst case is ~10-20 ms. Documented in :file:`docs/ARCHITECTURE.md`.
MAX_PREVIEW_SECONDS = 60.0
_DEBOUNCE_MS = 200
_WINDOW_PRESETS_S: tuple[float, ...] = (10.0, 30.0, 60.0)
_DEFAULT_PREVIEW_FS = 100.0
_BANNER_OK_STYLE = (
    "QLabel#ChainEditorBanner { padding: 4px 8px; background: transparent; color: #888; }"
)
_BANNER_ERROR_STYLE = (
    "QLabel#ChainEditorBanner { padding: 4px 8px; background: #2b1316; color: #f2a;}"
)

# Display labels for the palette buttons.
_PALETTE_LABELS: tuple[tuple[str, str], ...] = (
    ("detrend", "Detrend"),
    ("bandpass", "Bandpass"),
    ("highpass", "Highpass"),
    ("lowpass", "Lowpass"),
    ("notch", "Notch"),
    ("decimation", "Decimation"),
    ("sta_lta", "STA/LTA"),
)


class ChainEditorDialog(QDialog):
    """Modal editor for one device's DSP chain.

    The dialog owns a working copy of the stage list. On Accept the
    caller reads :meth:`stages` to retrieve the edited list. On Apply
    the dialog emits :attr:`stagesApplied(stages)` so the caller can
    mirror the change into its working state without closing. The
    editor itself does NOT call ``ConfigStore.update_device`` —
    persistence is the caller's responsibility (typically via the
    surrounding DeviceDialog's OK button).

    Args:
        stages: current stage list (typically ``device_cfg.dsp_chain``).
        device_name: name of the device the chain belongs to (used only
            in the dialog title and to scope the preview-stream combo).
        engine: live :class:`StreamingEngine` (the preview reads samples
            from its ring buffers via ``engine.read_recent``).
        parent: parent widget.
    """

    # ``stagesApplied(stages_tuple)`` — fired on Apply so the caller can
    # update its working state without the dialog closing.
    stagesApplied = Signal(object)  # noqa: N815

    def __init__(
        self,
        stages: list[DspStageConfig],
        device_name: str,
        engine: StreamingEngine,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"DSP chain — {device_name}")
        self.resize(900, 700)
        self._device_name = device_name
        self._engine = engine
        # Working copy of the stage list. We mutate this in place as the
        # user edits, but the on-disk config is only updated on OK /
        # Apply. A deep copy isn't required — pydantic models are frozen
        # and the chain is rebuilt via model_validate before writing.
        self._stages: list[DspStageConfig] = list(stages)
        self._saved_stages: tuple[DspStageConfig, ...] = tuple(stages)
        # Each chain entry gets a stable identity token written into the
        # matching ``QListWidgetItem``'s ``Qt.UserRole``. The token lets
        # us recover the correct list index after a drag-reorder
        # (mapping the dragged widget back to the underlying stage by
        # identity, NOT by visible summary), and it lets the per-row
        # form bind to "this token" instead of "row N" so a reorder
        # cannot corrupt the wrong stage.
        self._stage_tokens: list[str] = [self._new_token() for _ in stages]
        # The form currently shown in the right pane (rebuilt on row
        # selection change). ``_current_form_token`` is the stable
        # token of the stage the form writes to; ``_on_form_stage_*``
        # looks it up in ``_stage_tokens`` to find the live row index.
        self._current_form: _BaseStageForm | None = None
        self._current_form_token: str | None = None
        # Tokens of stages whose form is currently in an invalid state.
        # Token-keyed so a drag-reorder cannot leak a stale row number
        # — the row number is recomputed at validity-check time.
        self._invalid_tokens: set[str] = set()
        # The (device, nslc) the preview is currently bound to, plus its
        # cached input sample rate. None until the user picks one.
        self._preview_stream: tuple[str, str] | None = None
        self._preview_fs: float = _DEFAULT_PREVIEW_FS
        # The last valid chain we successfully ran on the preview; we
        # keep it so a transient bad edit can still show a plot.
        self._last_valid_chain: object | None = None
        # Telemetry: counts every call to ``chain.process`` from the
        # preview path. Used by tests to assert the debounce behaves.
        self._preview_compute_count = 0

        self._build_ui()
        self._refresh_stream_combo()
        # Auto-pick the first row so the user lands on a form, not on
        # an empty pane.
        if self._stages and self._chain_list.count() > 0:
            self._chain_list.setCurrentRow(0)
        self._update_buttons()
        self._schedule_preview()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        # Splitter: top half = chain editor, bottom half = preview.
        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._splitter.setChildrenCollapsible(False)

        self._splitter.addWidget(self._build_editor_pane())
        self._splitter.addWidget(self._build_preview_pane())
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)

        root.addWidget(self._splitter, stretch=1)

        # Buttons: OK / Apply / Cancel.
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._apply_button = self._buttons.button(QDialogButtonBox.StandardButton.Apply)
        self._ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._buttons.accepted.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)
        if self._apply_button is not None:
            self._apply_button.clicked.connect(self._on_apply)
        root.addWidget(self._buttons)

    def _build_editor_pane(self) -> QWidget:
        pane = QWidget(self)
        h = QHBoxLayout(pane)
        h.setContentsMargins(0, 0, 0, 0)

        # Left: palette.
        palette = QWidget(pane)
        palette_layout = QVBoxLayout(palette)
        palette_layout.setContentsMargins(0, 0, 0, 0)
        palette_layout.addWidget(QLabel("Add stage:", palette))
        for type_str, label in _PALETTE_LABELS:
            btn = QPushButton(label, palette)
            btn.clicked.connect(lambda _checked=False, t=type_str: self._on_add_stage(t))
            palette_layout.addWidget(btn)
        palette_layout.addStretch(1)

        # Middle: ordered chain list.
        self._chain_list = QListWidget(pane)
        self._chain_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._chain_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._chain_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._chain_list.customContextMenuRequested.connect(self._on_list_context_menu)
        self._chain_list.currentRowChanged.connect(self._on_row_changed)
        # ``QListWidget.model().rowsMoved`` fires after an internal-move
        # drag completes — that's when we sync our local list to the
        # widget's new order.
        self._chain_list.model().rowsMoved.connect(self._on_rows_moved)
        for stage, token in zip(self._stages, self._stage_tokens, strict=True):
            self._chain_list.addItem(self._make_list_item(stage, token))

        # Right: per-stage form host (form swapped on row change).
        self._form_host = QWidget(pane)
        form_host_layout = QVBoxLayout(self._form_host)
        form_host_layout.setContentsMargins(0, 0, 0, 0)
        self._form_placeholder = QLabel("Select a stage to edit its parameters.", self._form_host)
        self._form_placeholder.setStyleSheet("QLabel { color: #888; }")
        form_host_layout.addWidget(self._form_placeholder)
        form_host_layout.addStretch(1)

        h.addWidget(palette)
        h.addWidget(self._chain_list, stretch=1)
        h.addWidget(self._form_host, stretch=2)
        return pane

    def _build_preview_pane(self) -> QWidget:
        pane = QWidget(self)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)

        # Toolbar.
        toolbar = QWidget(pane)
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.addWidget(QLabel("Preview stream:"))
        from PySide6.QtWidgets import QComboBox

        self._stream_combo = QComboBox(toolbar)
        self._stream_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._stream_combo.currentIndexChanged.connect(self._on_stream_combo_changed)
        tb.addWidget(self._stream_combo, stretch=1)
        tb.addWidget(QLabel("Window:"))
        self._window_combo = QComboBox(toolbar)
        for s in _WINDOW_PRESETS_S:
            self._window_combo.addItem(f"{s:g} s", userData=s)
        self._window_combo.setCurrentIndex(len(_WINDOW_PRESETS_S) - 1)  # default 60s
        self._window_combo.currentIndexChanged.connect(lambda _i: self._schedule_preview())
        tb.addWidget(self._window_combo)
        layout.addWidget(toolbar)

        # Banner.
        self._banner = QLabel("Preview idle.", pane)
        self._banner.setObjectName("ChainEditorBanner")
        self._banner.setStyleSheet(_BANNER_OK_STYLE)
        layout.addWidget(self._banner)

        # Plots.
        graphics = pg.GraphicsLayoutWidget(pane)
        graphics.setBackground("#101418")
        self._raw_plot = graphics.addPlot(row=0, col=0)
        self._raw_plot.setLabel("left", "raw")
        self._raw_plot.setMouseEnabled(x=False, y=True)
        self._raw_plot.showGrid(x=True, y=True, alpha=0.2)
        self._raw_plot.setMenuEnabled(False)
        self._raw_curve = self._raw_plot.plot(pen=pg.mkPen("#3aa3ff", width=1))
        self._processed_plot = graphics.addPlot(row=1, col=0)
        self._processed_plot.setLabel("left", "processed")
        self._processed_plot.setMouseEnabled(x=False, y=True)
        self._processed_plot.showGrid(x=True, y=True, alpha=0.2)
        self._processed_plot.setMenuEnabled(False)
        self._processed_curve = self._processed_plot.plot(pen=pg.mkPen("#f5b942", width=1))
        self._processed_plot.setXLink(self._raw_plot)
        layout.addWidget(graphics, stretch=2)

        # Inline spectrogram of the processed signal.
        self._spec_view = SpectrogramView(
            window_seconds=MAX_PREVIEW_SECONDS,
            fs=_DEFAULT_PREVIEW_FS,
            label="preview",
            parent=pane,
        )
        layout.addWidget(self._spec_view, stretch=1)

        # Debounce timer.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._refresh_preview)

        return pane

    def _make_list_item(self, stage: object, token: str) -> QListWidgetItem:
        item = QListWidgetItem(stage_summary(stage))
        # The stable token lives in UserRole; drag-reorders preserve
        # the role payload because Qt copies the QListWidgetItem
        # wholesale across the move.
        item.setData(Qt.ItemDataRole.UserRole, token)
        return item

    @staticmethod
    def _new_token() -> str:
        return uuid.uuid4().hex

    def _row_for_token(self, token: str) -> int | None:
        for i in range(self._chain_list.count()):
            item = self._chain_list.item(i)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == token:
                return i
        return None

    # ------------------------------------------------------------------
    # Public state (for tests + DeviceDialog)
    # ------------------------------------------------------------------
    def stages(self) -> tuple[DspStageConfig, ...]:
        return tuple(self._stages)

    def is_dirty(self) -> bool:
        return tuple(self._stages) != self._saved_stages

    def preview_compute_count(self) -> int:
        return self._preview_compute_count

    # ------------------------------------------------------------------
    # Slots — palette / list / context menu
    # ------------------------------------------------------------------
    def _on_add_stage(self, type_str: str) -> None:
        stage = default_stage_for(type_str, fs=self._preview_fs)
        token = self._new_token()
        self._stages.append(stage)  # type: ignore[arg-type]
        self._stage_tokens.append(token)
        self._chain_list.addItem(self._make_list_item(stage, token))
        self._chain_list.setCurrentRow(self._chain_list.count() - 1)
        self._update_buttons()
        self._schedule_preview()

    def _on_row_changed(self, row: int) -> None:
        self._unmount_current_form()
        if not 0 <= row < len(self._stages):
            self._form_placeholder.setVisible(True)
            return
        self._form_placeholder.setVisible(False)
        stage = self._stages[row]
        token = self._stage_tokens[row]
        form = make_form(stage, fs=self._preview_fs)
        # Bind the form to the STABLE token, not the row number. A
        # subsequent drag-reorder shifts the row but ``_row_for_token``
        # always resolves the new index so the write lands on the right
        # stage (HIGH finding from the M6-closure review).
        form.stageChanged.connect(
            lambda new_stage, tok=token: self._on_form_stage_changed(tok, new_stage)
        )
        form.stageInvalid.connect(
            lambda reason, tok=token: self._on_form_stage_invalid(tok, reason)
        )
        layout = self._form_host.layout()
        # The placeholder lives at index 0; insert the form at index 1
        # (just before the stretcher) so the layout order stays stable.
        # ``QLayout`` itself has no insertWidget; the host's layout is
        # the ``QVBoxLayout`` we created in ``_build_editor_pane``, so
        # cast for mypy without changing behaviour.
        if isinstance(layout, QVBoxLayout):
            layout.insertWidget(1, form)
        self._current_form = form
        self._current_form_token = token

    def _unmount_current_form(self) -> None:
        if self._current_form is None:
            return
        self._current_form.setParent(None)
        self._current_form.deleteLater()
        self._current_form = None
        self._current_form_token = None

    def _on_form_stage_changed(self, token: str, new_stage: object) -> None:
        row = self._row_for_token(token)
        if row is None:
            return
        self._stages[row] = new_stage  # type: ignore[call-overload]
        self._invalid_tokens.discard(token)
        item = self._chain_list.item(row)
        if item is not None:
            item.setText(stage_summary(new_stage))
        self._update_buttons()
        self._schedule_preview()

    def _on_form_stage_invalid(self, token: str, reason: str) -> None:
        # The form already paints its own fields red and shows a tooltip;
        # we also surface the message in the preview banner so the user
        # sees it even when the form isn't in focus.
        self._invalid_tokens.add(token)
        self._set_banner_error(reason)
        # Invalid stage → OK / Apply disabled.
        self._update_buttons()

    def _on_rows_moved(self, *_args: object) -> None:
        """Sync the local stage + token lists to the QListWidget's new
        row order after a drag-reorder.

        Stable per-row tokens (UserRole on each ``QListWidgetItem``)
        let us map widget rows back to underlying stages by identity
        rather than by visible summary — fixes the HIGH finding from
        the M6-closure code review (two identical stages, or an edit
        that hasn't yet updated the summary, would otherwise corrupt
        the list).
        """
        old_by_token: dict[str, DspStageConfig] = dict(
            zip(self._stage_tokens, self._stages, strict=True)
        )
        new_stages: list[DspStageConfig] = []
        new_tokens: list[str] = []
        for i in range(self._chain_list.count()):
            item = self._chain_list.item(i)
            if item is None:
                continue
            token = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(token, str) or token not in old_by_token:
                continue
            new_stages.append(old_by_token[token])
            new_tokens.append(token)
        # Sanity: if the matching failed (broken Qt state), keep the
        # current state to avoid silently dropping stages.
        if len(new_stages) == len(self._stages):
            self._stages = new_stages
            self._stage_tokens = new_tokens
        self._update_buttons()
        self._schedule_preview()

    def _on_list_context_menu(self, pos: QPoint) -> None:
        item = self._chain_list.itemAt(pos)
        if item is None:
            return
        row = self._chain_list.row(item)
        menu = QMenu(self._chain_list)
        delete_action = menu.addAction("Delete stage")
        chosen = menu.exec(self._chain_list.mapToGlobal(pos))
        if chosen is delete_action:
            self._on_delete_row(row)

    def _on_delete_row(self, row: int) -> None:
        if not 0 <= row < len(self._stages):
            return
        gone_token = self._stage_tokens[row]
        del self._stages[row]
        del self._stage_tokens[row]
        self._chain_list.takeItem(row)
        self._invalid_tokens.discard(gone_token)
        # If the deleted row's form was mounted, drop it too.
        if self._current_form_token == gone_token:
            self._unmount_current_form()
            self._form_placeholder.setVisible(True)
        self._update_buttons()
        self._schedule_preview()

    # ------------------------------------------------------------------
    # Slots — preview
    # ------------------------------------------------------------------
    def _on_stream_combo_changed(self, _index: int) -> None:
        data = self._stream_combo.currentData()
        if isinstance(data, tuple) and len(data) == 2:
            self._preview_stream = data
            _, fs, _t = self._engine.read_recent(data[0], data[1], 0.1)
            if fs > 0:
                self._preview_fs = fs
                # Propagate the new fs to the current form so its
                # Nyquist check uses the right value.
                if self._current_form is not None:
                    self._current_form.set_fs(fs)
                self._spec_view.update_meta(fs=fs)
        else:
            self._preview_stream = None
        self._schedule_preview()

    def _refresh_stream_combo(self) -> None:
        """Populate the preview combo with streams belonging to this
        device. Reads :attr:`StreamingEngine._buffers` directly — the
        engine has no public ``streams()`` API yet (see follow-up note
        in stage 2 review)."""
        self._stream_combo.blockSignals(True)
        self._stream_combo.clear()
        for composite in self._engine._buffers:
            if DEVICE_KEY_SEP not in composite:
                continue
            device, nslc = composite.split(DEVICE_KEY_SEP, maxsplit=1)
            if device != self._device_name:
                continue
            self._stream_combo.addItem(f"{device} / {nslc}", userData=(device, nslc))
        if self._stream_combo.count() == 0:
            self._stream_combo.addItem("(no live streams)")
            self._stream_combo.setEnabled(False)
        else:
            self._stream_combo.setEnabled(True)
            self._stream_combo.setCurrentIndex(0)
            data = self._stream_combo.currentData()
            if isinstance(data, tuple) and len(data) == 2:
                self._preview_stream = data
                _, fs, _t = self._engine.read_recent(data[0], data[1], 0.1)
                if fs > 0:
                    self._preview_fs = fs
        self._stream_combo.blockSignals(False)

    def _schedule_preview(self) -> None:
        self._debounce.start(_DEBOUNCE_MS)

    @Slot()
    def _refresh_preview(self) -> None:
        if self._preview_stream is None:
            self._set_banner_ok("No live stream selected.")
            return
        device, nslc = self._preview_stream
        window_s = float(self._window_combo.currentData() or _WINDOW_PRESETS_S[-1])
        window_s = min(window_s, MAX_PREVIEW_SECONDS)
        samples, fs, _t_end = self._engine.read_recent(device, nslc, window_s)
        if samples.size == 0 or fs <= 0:
            self._set_banner_ok("No data yet.")
            return
        self._preview_fs = float(fs)

        # ------------------------------------------------------------------
        # Build a fresh chain from the current stages. ``build_chain``
        # may raise ConfigError; we leave the LAST valid preview on
        # screen and paint the banner red so the user sees the cause.
        # ------------------------------------------------------------------
        if not self._stages:
            self._raw_curve.setData(
                np.arange(samples.size, dtype=np.float64) / fs,
                samples,
            )
            # No chain → processed == raw, nothing to feed to the
            # spectrogram.
            self._processed_curve.setData(
                np.arange(samples.size, dtype=np.float64) / fs,
                samples,
            )
            self._spec_view.clear()
            self._set_banner_ok(
                f"Chain is empty — processed = raw ({samples.size} samples @ {fs:g} Hz)."
            )
            self._update_buttons()
            return

        try:
            chain = build_chain(
                stages=list(self._stages),
                fs_in=float(fs),
                stream_id=StreamID.from_trace_id(nslc),
                live=True,
            )
        except ConfigError as exc:
            self._set_banner_error(f"Chain invalid: {exc}")
            self._update_buttons()
            return

        try:
            result = chain.process(samples.astype(np.float64), UTCDateTime())
        except Exception as exc:  # defence-in-depth — bad input must not freeze the UI
            self._set_banner_error(f"Chain processing failed: {exc}")
            self._update_buttons()
            return

        self._preview_compute_count += 1
        self._last_valid_chain = chain

        # Plot raw vs processed.
        t_raw = np.arange(samples.size, dtype=np.float64) / fs
        self._raw_curve.setData(t_raw, samples)
        processed = np.asarray(result.samples, dtype=np.float32)
        fs_out = float(result.fs_out)
        t_proc = np.arange(processed.size, dtype=np.float64) / max(fs_out, 1e-9)
        self._processed_curve.setData(t_proc, processed)

        # Feed the processed signal to a fresh RollingSpectrogram and
        # paint columns. One-shot; fresh allocation each tick keeps the
        # preview stateless.
        self._spec_view.update_meta(fs=fs_out)
        self._spec_view.clear()
        spec = RollingSpectrogram(fs=fs_out)
        for col in spec.add_samples(processed):
            self._spec_view.add_column(col, spec.freqs())

        self._set_banner_ok(
            f"OK · {samples.size} in @ {fs:g} Hz → {processed.size} out @ {fs_out:g} Hz"
        )
        self._update_buttons()

    def _set_banner_ok(self, msg: str) -> None:
        self._banner.setStyleSheet(_BANNER_OK_STYLE)
        self._banner.setText(msg)

    def _set_banner_error(self, msg: str) -> None:
        self._banner.setStyleSheet(_BANNER_ERROR_STYLE)
        self._banner.setText(msg)

    # ------------------------------------------------------------------
    # Buttons + apply
    # ------------------------------------------------------------------
    def _is_chain_valid(self) -> bool:
        # An in-progress edit that's currently invalid (the per-row
        # form emitted ``stageInvalid``) must block OK / Apply even
        # though ``self._stages`` still holds the last-good values.
        # ``_invalid_tokens`` is token-keyed so a drag-reorder cannot
        # leak a stale row number.
        if self._invalid_tokens:
            return False
        if not self._stages:
            return True
        # Build the chain at preview fs to catch Nyquist / cross-stage
        # constraints. Empty stream selection falls back to 100 Hz so
        # the editor stays usable even before any packet has arrived.
        fs = self._preview_fs or _DEFAULT_PREVIEW_FS
        try:
            build_chain(
                stages=list(self._stages),
                fs_in=fs,
                stream_id=StreamID("XX", "TEST", "00", "HHZ"),
                live=True,
            )
        except ConfigError:
            return False
        return True

    def _update_buttons(self) -> None:
        valid = self._is_chain_valid()
        dirty = self.is_dirty()
        if self._ok_button is not None:
            self._ok_button.setEnabled(valid)
        if self._apply_button is not None:
            self._apply_button.setEnabled(valid and dirty)

    def _on_ok(self) -> None:
        if not self._is_chain_valid():
            QMessageBox.warning(self, "DSP chain", "Chain is invalid; cannot save.")
            return
        self.accept()

    def _on_apply(self) -> None:
        if not self._is_chain_valid():
            QMessageBox.warning(self, "DSP chain", "Chain is invalid; cannot save.")
            return
        self._saved_stages = tuple(self._stages)
        self.stagesApplied.emit(tuple(self._stages))
        self._update_buttons()
