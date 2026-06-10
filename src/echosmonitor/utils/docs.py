"""Locate the bundled or repo-root copy of project documentation.

The Help menu's *Manual tests…* entry resolves a filesystem path to
``docs/MANUAL_TESTS.md`` and hands it to ``QDesktopServices.openUrl``.
In dev mode (``uv run``) the doc lives at the project root next to
``src/``; in installed builds it is not currently bundled inside the
wheel, so resolution falls back to ``None`` and the caller surfaces a
polite *not bundled* message.

If a future packaging change ships ``docs/`` as package data, this
helper still works — :func:`find_manual_tests` walks parents of its
own ``__file__`` and accepts any ancestor directory that contains a
``docs/MANUAL_TESTS.md`` sibling.
"""

from __future__ import annotations

from pathlib import Path

_DOC_RELATIVE = Path("docs") / "MANUAL_TESTS.md"


def find_manual_tests() -> Path | None:
    """Return the path to ``docs/MANUAL_TESTS.md`` if discoverable.

    Walks upward from this module's directory looking for a sibling
    ``docs/MANUAL_TESTS.md``. Returns ``None`` when no such file is
    reachable — typical for installed builds without bundled docs.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / _DOC_RELATIVE
        if candidate.is_file():
            return candidate
    return None
