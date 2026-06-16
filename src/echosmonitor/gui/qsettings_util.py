"""Cross-platform ``QSettings`` construction.

On Windows the default ``NativeFormat`` is the **registry**, which
``QSettings.setPath`` cannot redirect (so tests can leak into — and read
stale state from — the real user registry) and which makes settings
non-portable between machines/OSes. ``IniFormat`` is a user-scope **file**
on every platform: testable (the test fixture redirects it via ``setPath``)
and portable. The whole app constructs its window/layout settings through
:func:`open_settings` so the format is consistent everywhere.

This is consistent with the M0-A decision that QSettings holds only disposable
window geometry / dock layout state (reset, log once — never migrated): a
one-time loss of layout on the format switch is acceptable.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings


def open_settings(org: str, app: str) -> QSettings:
    """Return a user-scope, file-backed (``IniFormat``) ``QSettings``.

    File-backed on every OS (never the Windows registry), so it is portable
    and honours ``QSettings.setPath`` redirection in tests.
    """
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, org, app)
