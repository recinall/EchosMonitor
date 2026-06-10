"""Custom dock title-bar widget with focus + detach affordances.

Qt's *native* :class:`QDockWidget` title bar renders only the dock title
plus the built-in float / close buttons; it offers no supported hook to
add custom buttons. The only sanctioned way to host extra controls is to
replace the whole title bar via :meth:`QDockWidget.setTitleBarWidget` —
which is exactly what this widget is for.

Replacing the native title bar means we also become responsible for the
affordances Qt drew for free:

* **Drag-to-move / drag-to-float** — Qt initiates a dock drag from mouse
  events on the *native* title bar. With a custom widget set, Qt forwards
  press/move events that fall through to the dock only if our child
  widgets do not consume them. The title :class:`QLabel` therefore keeps
  its default (transparent-to-mouse) behaviour: it neither installs an
  event filter nor overrides mouse handlers, so a press on the label
  bubbles up to the dock and the native drag still works.
* **Double-click-to-toggle-floating** — same mechanism: a double-click on
  the (non-consuming) label area reaches the dock and toggles floating,
  matching the native bar.

We deliberately add explicit ⛶ (focus), ⧉ (detach), and ✕ (close) tool
buttons so every action is discoverable from the bar itself, complementing
the keyboard shortcuts and View-menu entries wired by the
:class:`MainWindow`. (Toggling floating also remains available natively by
double-clicking the label area — see above — so a separate float button
would be redundant with the ⧉ detach button.)
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QWidget,
)

# Glyphs for the custom buttons. Kept as named constants so there are no
# magic strings sprinkled through the layout code.
_GLYPH_FOCUS = "⛶"  # ⛶ full-screen / focus
_GLYPH_DETACH = "⧉"  # ⧉ two squares — detach to floating window
_GLYPH_CLOSE = "✕"  # ✕ close (hide) the dock

# Fixed square side for the title-bar tool buttons, in device-independent
# pixels. Chosen to match a compact native title bar height.
_BUTTON_SIDE_PX = 18

# Horizontal margins around the title-bar content.
_MARGIN_PX = 4


class DockTitleBar(QWidget):
    """Replacement title bar for a :class:`QDockWidget`.

    Layout (left → right): title label · stretch · focus · detach · close.
    The label is the draggable area and is intentionally not given any
    custom mouse handling so Qt's native drag-to-move and
    double-click-to-float continue to work (see module docstring).

    Signals:
        focusRequested: The ⛶ button was clicked.
        detachRequested: The ⧉ button was clicked.
        closeRequested: The ✕ button was clicked.
    """

    focusRequested = Signal()  # noqa: N815
    detachRequested = Signal()  # noqa: N815
    closeRequested = Signal()  # noqa: N815

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        """Build the bar.

        Args:
            title: Initial label text; keep it in sync with the dock's
                ``windowTitle`` via :meth:`set_title`.
            parent: Owning widget (normally the QDockWidget).
        """
        super().__init__(parent)
        self.setObjectName("DockTitleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(_MARGIN_PX, 0, _MARGIN_PX, 0)
        layout.setSpacing(_MARGIN_PX)

        self._label = QLabel(title, self)
        self._label.setObjectName("DockTitleBarLabel")
        # Expand horizontally so the label region (the drag handle) covers
        # the whole free width, not just the text. No custom mouse
        # handling here — see module docstring.
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._label, stretch=1)

        self._focus_button = self._make_button(_GLYPH_FOCUS, "Focus this panel full-screen (F11)")
        self._focus_button.clicked.connect(self.focusRequested)
        layout.addWidget(self._focus_button)

        self._detach_button = self._make_button(
            _GLYPH_DETACH, "Detach / re-dock this panel as a floating window"
        )
        self._detach_button.clicked.connect(self.detachRequested)
        layout.addWidget(self._detach_button)

        self._close_button = self._make_button(_GLYPH_CLOSE, "Hide this panel")
        self._close_button.clicked.connect(self.closeRequested)
        layout.addWidget(self._close_button)

    def _make_button(self, glyph: str, tooltip: str) -> QToolButton:
        button = QToolButton(self)
        button.setText(glyph)
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        button.setFixedSize(_BUTTON_SIDE_PX, _BUTTON_SIDE_PX)
        button.setFocusPolicy(  # do not steal keyboard focus from panel
            button.focusPolicy().NoFocus
        )
        return button

    def set_title(self, title: str) -> None:
        """Update the label to mirror the dock's window title."""
        self._label.setText(title)

    def title(self) -> str:
        """Return the current label text (mirrors the dock window title)."""
        return self._label.text()
