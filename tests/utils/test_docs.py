"""Tests for ``utils.docs.find_manual_tests``."""

from __future__ import annotations

from pathlib import Path

import pytest

from echosmonitor.utils import docs as docs_mod


def test_find_manual_tests_returns_path_in_dev_checkout() -> None:
    """In the dev checkout, the helper finds ``docs/MANUAL_TESTS.md``
    by walking up from the package's installed location.
    """
    path = docs_mod.find_manual_tests()
    assert path is not None
    assert path.is_file()
    assert path.name == "MANUAL_TESTS.md"
    assert path.parent.name == "docs"


def test_find_manual_tests_returns_none_when_no_docs_anywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When none of ``__file__``'s ancestors contain ``docs/MANUAL_TESTS.md``,
    the helper returns None — the caller's "not bundled" fallback path.
    """
    # Pretend the helper lives deep inside an isolated temp tree with
    # no ancestors that match the expected layout.
    fake_module_path = tmp_path / "nested" / "pkg" / "module.py"
    fake_module_path.parent.mkdir(parents=True)
    fake_module_path.write_text("# placeholder\n")
    monkeypatch.setattr(docs_mod, "__file__", str(fake_module_path))

    assert docs_mod.find_manual_tests() is None


def test_find_manual_tests_finds_a_docs_sibling_in_an_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper accepts any ancestor whose ``docs/MANUAL_TESTS.md``
    sibling exists — that is the contract documented for installed
    builds with bundled docs.
    """
    project_root = tmp_path / "project"
    package_dir = project_root / "src" / "pkg" / "utils"
    package_dir.mkdir(parents=True)
    fake_module_path = package_dir / "docs.py"
    fake_module_path.write_text("# placeholder\n")

    docs_file = project_root / "docs" / "MANUAL_TESTS.md"
    docs_file.parent.mkdir()
    docs_file.write_text("# Manual tests\n")

    monkeypatch.setattr(docs_mod, "__file__", str(fake_module_path))

    found = docs_mod.find_manual_tests()
    assert found is not None
    assert found == docs_file
