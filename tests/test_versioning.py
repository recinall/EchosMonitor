"""M7-A: version resolution, window-title display, and CHANGELOG presence."""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMainWindow
from pytestqt.qtbot import QtBot

import echosmonitor
from echosmonitor import _resolve_version
from echosmonitor.config.loader import load_config
from echosmonitor.gui.main_window import MainWindow

# Loose PEP 440 shape: a release (1.2.3) or a dev/local version
# (0.1.dev43+g4d7a..., 0.0.0+dev). Good enough to reject the empty string
# and obvious garbage without re-implementing packaging.version.
_PEP440_RE = re.compile(r"^\d+(\.\d+)*([.\-_]?(a|b|rc|dev)\d*)?(\+[a-zA-Z0-9.]+)?$")


def test_version_is_nonempty_pep440_string() -> None:
    assert isinstance(echosmonitor.__version__, str)
    assert echosmonitor.__version__
    assert _PEP440_RE.match(echosmonitor.__version__), echosmonitor.__version__


def test_resolve_version_prefers_importlib_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the dist-info is present, its version wins (the editable/installed
    and metadata-collecting-bundle path)."""
    monkeypatch.setattr(echosmonitor, "version", lambda _name: "7.8.9")
    assert _resolve_version() == "7.8.9"


def test_resolve_version_falls_back_to_generated_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No dist-info -> the hatch-vcs generated ``_version.py`` is used (the
    frozen-app-without-metadata path)."""

    def _raise(_name: str) -> str:
        raise echosmonitor.PackageNotFoundError(_name)

    monkeypatch.setattr(echosmonitor, "version", _raise)
    fake = types.ModuleType("echosmonitor._version")
    fake.__version__ = "9.9.9"  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "echosmonitor._version", fake)
    assert _resolve_version() == "9.9.9"


def test_resolve_version_final_fallback_is_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No dist-info AND no generated module -> the explicit dev sentinel (a raw
    source checkout that was never built)."""

    def _raise(_name: str) -> str:
        raise echosmonitor.PackageNotFoundError(_name)

    monkeypatch.setattr(echosmonitor, "version", _raise)
    # A module object missing ``__version__`` makes the ``from … import``
    # raise ImportError, exercising the final branch without deleting the real
    # generated module from the installed tree.
    broken = types.ModuleType("echosmonitor._version")
    monkeypatch.setitem(__import__("sys").modules, "echosmonitor._version", broken)
    assert _resolve_version() == "0.0.0+dev"


def test_window_title_shows_version(qtbot: QtBot) -> None:
    cfg, _ = load_config(None)
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    assert isinstance(window, QMainWindow)
    title = window.windowTitle()
    assert "EchosMonitor" in title
    assert echosmonitor.__version__ in title


def _find_changelog() -> Path | None:
    """Locate CHANGELOG.md by walking up from the package (dev-checkout only;
    it is a repo-root doc, not a bundled resource)."""
    for ancestor in Path(echosmonitor.__file__).resolve().parents:
        candidate = ancestor / "CHANGELOG.md"
        if candidate.is_file():
            return candidate
    return None


def test_changelog_exists_and_names_current_minor() -> None:
    changelog = _find_changelog()
    assert changelog is not None, "CHANGELOG.md not found above the package"
    text = changelog.read_text(encoding="utf-8")
    minor = ".".join(echosmonitor.__version__.split(".")[:2])  # e.g. "0.1"
    assert minor in text, f"CHANGELOG.md does not mention the {minor}.x series"
