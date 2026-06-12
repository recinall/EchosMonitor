"""Configuration loader.

Resolution order:

1. Explicit ``path`` argument (raises ``FileNotFoundError`` if missing).
2. ``$XDG_CONFIG_HOME/echosmonitor/config.yaml`` if it exists
   (resolved via ``platformdirs``).
3. The bundled ``default.yaml`` (always present).

The bundled defaults are always loaded first; the user file (if any) is
deep-merged on top, then the merged dict is validated against
:class:`RootConfig`. Dicts merge recursively; scalars and lists replace.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any, cast

import structlog
import yaml
from platformdirs import user_config_dir

from echosmonitor.config.schema import RootConfig

_log = structlog.get_logger(__name__)

DEFAULT_CONFIG_NAME = "config.yaml"
_APP_NAME = "echosmonitor"
# Pre-rename app name (M0-A). No automatic migration (decision log): a
# config found only at the old path is ignored, but loudly, so the user
# knows to copy it over once.
_LEGACY_APP_NAME = "seedlink-dashboard"


def _read_bundled_default() -> tuple[dict[str, Any], Path]:
    """Read the bundled ``default.yaml`` and return ``(data, on_disk_path)``.

    Uses :func:`importlib.resources.as_file` so the resource is materialized to
    a real filesystem path even when the package is loaded from a zip archive.
    The on-disk path is exposed solely for status-bar display; never use it for
    further filesystem operations.
    """
    ref = resources.files("echosmonitor.config").joinpath("default.yaml")
    with resources.as_file(ref) as path:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        on_disk = Path(path)
    if raw is None:
        data: dict[str, Any] = {}
    else:
        if not isinstance(raw, dict):
            raise ValueError(
                f"bundled default.yaml root must be a mapping, got {type(raw).__name__}"
            )
        data = cast(dict[str, Any], raw)
    return data, on_disk


def _user_config_path() -> Path:
    return Path(user_config_dir(_APP_NAME)) / DEFAULT_CONFIG_NAME


# Public alias used by ``core/config_store.py`` (M4 stage B). Keeping the
# leading-underscore form too so the existing test fixtures that monkey-
# patch ``loader._user_config_path`` keep working without churn.
def user_config_path() -> Path:
    """Resolve the per-user YAML config path via :mod:`platformdirs`.

    Returns the same path the loader falls back to when no explicit
    ``--config`` is supplied. Stage B's :class:`ConfigStore` uses this
    so the in-memory store and the on-disk file stay in lockstep.
    """
    return _user_config_path()


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file and return the top-level mapping (or ``{}`` if empty)."""
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping, got {type(data).__name__}: {path}")
    return cast(dict[str, Any], data)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``override`` deep-merged in.

    Dicts merge recursively. Lists and scalars in ``override`` replace those
    in ``base`` — merging device lists by index is not safe.
    """
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _strip_legacy_ai_key(data: dict[str, Any], source: Path) -> dict[str, Any]:
    """Drop the removed top-level ``ai:`` section from pre-rule-12 configs.

    The schema is ``extra="forbid"``, so a user file written before the AI
    subsystem was removed would otherwise fail validation outright. Strip
    the key and warn once per load instead; the section is never written
    back (ConfigStore serialises from the validated model, which no longer
    has the field).
    """
    if "ai" in data:
        data = {k: v for k, v in data.items() if k != "ai"}
        _log.warning("config_legacy_ai_key_stripped", source=str(source))
    return data


def load_config(path: Path | None = None) -> tuple[RootConfig, Path]:
    """Load and validate the configuration.

    Args:
        path: Explicit path to a user YAML file. If ``None``, falls back to
            the per-user config under ``$XDG_CONFIG_HOME``, and finally to
            the bundled defaults.

    Returns:
        A tuple ``(config, resolved_path)``. ``resolved_path`` is the file
        runtime mutations must WRITE to: the explicit ``path`` when given,
        else the per-user config path — even when that file does not exist
        yet and the bundled defaults provided every value. It is NEVER the
        bundled file: handing that to :class:`ConfigStore` made the
        first-run wizard rewrite the package's ``default.yaml`` in place
        (caught in the 2026-06-12 field run; in a packaged install the
        bundle is read-only and every wizard finish would fail).

    Raises:
        FileNotFoundError: An explicit ``path`` was provided but does not exist.
        yaml.YAMLError: The user YAML file is malformed.
        pydantic.ValidationError: The merged configuration violates the schema.
    """
    base, _bundled_path = _read_bundled_default()

    if path is not None:
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")
        user_data = _strip_legacy_ai_key(_read_yaml(path), source=path)
        merged = _deep_merge(base, user_data)
        return RootConfig.model_validate(merged), path

    user_path = _user_config_path()
    if user_path.exists():
        user_data = _strip_legacy_ai_key(_read_yaml(user_path), source=user_path)
        merged = _deep_merge(base, user_data)
        return RootConfig.model_validate(merged), user_path

    legacy_path = Path(user_config_dir(_LEGACY_APP_NAME)) / DEFAULT_CONFIG_NAME
    if legacy_path.exists():
        _log.warning(
            "legacy_config_ignored",
            legacy_path=str(legacy_path),
            expected_path=str(user_path),
            hint="copy the file to the new path to keep your devices",
        )
    # Bundled defaults are in effect, but the resolved path is the USER
    # path: it is where the first write (wizard, settings, add-device)
    # must land. The bundled file is package data, never a write target.
    return RootConfig.model_validate(base), user_path
