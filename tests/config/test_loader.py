"""Tests for the configuration loader."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from echosmonitor.config import loader as loader_mod
from echosmonitor.config.loader import load_config


def test_loads_bundled_defaults_when_no_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Isolate from any user file under XDG_CONFIG_HOME — the loader would
    # otherwise pick that up and the test would pass or fail depending on
    # the developer's machine.
    monkeypatch.setattr(loader_mod, "_user_config_path", lambda: tmp_path / "missing.yaml")
    cfg, path = load_config(None)
    assert cfg.app.log_level == "INFO"
    assert cfg.ui.theme == "dark"
    assert cfg.devices == []
    assert path.name == "default.yaml"


def test_explicit_path_returned(tmp_path: Path) -> None:
    user = tmp_path / "x.yaml"
    user.write_text("ui:\n  theme: light\n", encoding="utf-8")
    cfg, path = load_config(user)
    assert path == user
    assert cfg.ui.theme == "light"
    # Non-overridden fields keep their defaults.
    assert cfg.app.log_level == "INFO"


def test_missing_explicit_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does_not_exist.yaml")


def test_malformed_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(": : not yaml ::\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_config(bad)


def test_schema_violation_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("app:\n  log_level: BOGUS\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(bad)


def test_deep_merge_preserves_unset_keys(tmp_path: Path) -> None:
    user = tmp_path / "x.yaml"
    user.write_text("ui:\n  theme: light\n", encoding="utf-8")
    cfg, _ = load_config(user)
    # Only ui.theme was overridden; ui.refresh_hz must come from defaults.
    assert cfg.ui.theme == "light"
    assert cfg.ui.refresh_hz == 20
    assert cfg.app.log_level == "INFO"


def test_lists_replace_rather_than_merge(tmp_path: Path) -> None:
    user = tmp_path / "x.yaml"
    user.write_text(
        "devices:\n"
        "  - name: lab\n"
        "    host: localhost\n"
        "    port: 18000\n"
        "    selectors:\n"
        "      - { network: IV, station: '*', location: '*', channel: 'HH?' }\n",
        encoding="utf-8",
    )
    cfg, _ = load_config(user)
    assert len(cfg.devices) == 1
    assert cfg.devices[0].name == "lab"
    assert cfg.devices[0].selectors[0].channel == "HH?"


def test_root_must_be_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(bad)


def test_xdg_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    user = tmp_path / "config.yaml"
    user.write_text("ui:\n  theme: light\n", encoding="utf-8")
    monkeypatch.setattr(loader_mod, "_user_config_path", lambda: user)
    cfg, path = load_config(None)
    assert path == user
    assert cfg.ui.theme == "light"


def test_legacy_ai_key_is_stripped_with_warning(
    tmp_path: Path,
    capture_structlog: list[dict[str, object]],
) -> None:
    """M0 regression (rule 12): a pre-rename user config with a top-level
    ``ai:`` section must still load — the key is stripped (the schema is
    extra="forbid") and a ``config_legacy_ai_key_stripped`` warning is
    emitted with the source file in context."""
    user = tmp_path / "x.yaml"
    user.write_text(
        "ui:\n  theme: light\nai:\n  enabled: true\n  window_seconds: 60\n",
        encoding="utf-8",
    )
    cfg, path = load_config(user)  # must not raise ValidationError
    assert path == user
    # Non-AI settings keep full strength.
    assert cfg.ui.theme == "light"
    # The validated model has no ai field at all.
    assert "ai" not in type(cfg).model_fields
    assert not hasattr(cfg, "ai")

    stripped = [r for r in capture_structlog if r.get("event") == "config_legacy_ai_key_stripped"]
    assert len(stripped) == 1
    assert stripped[0]["source"] == str(user)


def test_legacy_ai_key_stripped_on_platformdirs_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture_structlog: list[dict[str, object]],
) -> None:
    """The strip + warning also applies on the platformdirs fallback branch."""
    user = tmp_path / "config.yaml"
    user.write_text("ui:\n  theme: light\nai:\n  enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(loader_mod, "_user_config_path", lambda: user)
    cfg, path = load_config(None)
    assert path == user
    assert cfg.ui.theme == "light"
    assert not hasattr(cfg, "ai")
    stripped = [r for r in capture_structlog if r.get("event") == "config_legacy_ai_key_stripped"]
    assert len(stripped) == 1
    assert stripped[0]["source"] == str(user)


def test_no_strip_warning_without_legacy_ai_key(
    tmp_path: Path,
    capture_structlog: list[dict[str, object]],
) -> None:
    """A config without an ``ai:`` key must not emit the strip warning."""
    user = tmp_path / "x.yaml"
    user.write_text("ui:\n  theme: light\n", encoding="utf-8")
    cfg, _ = load_config(user)
    assert cfg.ui.theme == "light"
    assert not any(
        r.get("event") == "config_legacy_ai_key_stripped" for r in capture_structlog
    )


def test_legacy_platformdirs_config_ignored_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture_structlog: list[dict[str, object]],
) -> None:
    """M0 regression (decision log): a config that exists ONLY at the
    pre-rename ``seedlink-dashboard`` platformdirs path is never loaded —
    the loader falls back to the bundled defaults — but it warns loudly
    (``legacy_config_ignored``) with both paths so the user knows to copy
    the file over once."""
    expected = tmp_path / "echosmonitor" / "config.yaml"  # does not exist
    monkeypatch.setattr(loader_mod, "_user_config_path", lambda: expected)
    legacy_dir = tmp_path / "seedlink-dashboard"
    legacy_dir.mkdir()
    legacy_file = legacy_dir / "config.yaml"
    legacy_file.write_text("ui:\n  theme: light\n", encoding="utf-8")
    monkeypatch.setattr(
        loader_mod, "user_config_dir", lambda app_name: str(tmp_path / app_name)
    )

    cfg, path = load_config(None)
    # Bundled defaults win — the legacy file's override is NOT applied.
    assert path.name == "default.yaml"
    assert cfg.ui.theme == "dark"

    events = [r for r in capture_structlog if r.get("event") == "legacy_config_ignored"]
    assert len(events) == 1
    assert events[0]["legacy_path"] == str(legacy_file)
    assert events[0]["expected_path"] == str(expected)


def test_no_legacy_warning_when_legacy_config_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture_structlog: list[dict[str, object]],
) -> None:
    """No config anywhere → bundled defaults, and NO legacy warning."""
    monkeypatch.setattr(
        loader_mod, "_user_config_path", lambda: tmp_path / "echosmonitor" / "config.yaml"
    )
    monkeypatch.setattr(
        loader_mod, "user_config_dir", lambda app_name: str(tmp_path / app_name)
    )
    _cfg, path = load_config(None)
    assert path.name == "default.yaml"
    assert not any(r.get("event") == "legacy_config_ignored" for r in capture_structlog)


def test_bundled_default_matches_repo_root() -> None:
    """The bundled YAML and the repo-root example must stay byte-identical;
    if this test fails, sync them before merging."""
    bundled = resources.files("echosmonitor.config").joinpath("default.yaml")
    bundled_bytes = bundled.read_bytes()

    repo_root = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
    if not repo_root.exists():
        pytest.skip("running outside repo checkout — repo-root default.yaml missing")
    assert repo_root.read_bytes() == bundled_bytes, (
        f"{repo_root} differs from bundled default.yaml; sync them"
    )
