"""M7-B/E: headless start-and-exit smoke for the CLI (`--version`, `--check`).

These run the app in a fresh interpreter so they exercise the same start path a
packaged binary does — the whole import graph plus, for ``--check``, runtime
config load and main-window construction. They are the in-repo analogue of the
packaged smoke test the CI release job will run against the built bundle.

The subprocess does NOT inherit the conftest's platformdirs monkeypatch, so the
environment is sandboxed via XDG_*/HOME into ``tmp_path`` — the M2-C launch
sweep then runs against a throwaway archive root, never the real user data dir.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _sandbox_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["HOME"] = str(tmp_path)
    return env


def test_version_flag(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "echosmonitor", "--version"],
        env=_sandbox_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    from echosmonitor import __version__

    assert __version__ in proc.stdout


def test_check_flag_starts_and_exits_clean(tmp_path: Path) -> None:
    """``--check`` constructs config + the main window headless and exits 0.

    Proves the bundled default.yaml and resources resolve at runtime and the
    main window builds without a display — the packaged smoke contract.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "echosmonitor", "--check"],
        env=_sandbox_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"--check failed:\n{proc.stderr}"


def test_obspy_io_self_check_round_trips() -> None:
    """The ``--check`` obspy-IO probe round-trips MiniSEED + StationXML through
    obspy's entry-point plugin registry.

    Regression for the v0.1.1 field bug: a freeze missing obspy's distribution
    metadata has an EMPTY plugin registry, so archive MSEED reads, StationXML
    parsing and SeedLink packet decoding all fail with ``Format "X" is not
    supported``. In the dev env obspy's metadata is present so this only asserts
    the probe never raises; its real teeth are the packaged-binary ``--check``
    in CI, which fails if obspy's metadata was not bundled.
    """
    from echosmonitor.__main__ import _obspy_io_self_check

    _obspy_io_self_check()  # must not raise


def _packaged_binary() -> Path:
    root = Path(__file__).resolve().parents[1] / "dist" / "echosmonitor"
    # one-dir layout differs by OS: bare name on POSIX, .exe on Windows.
    for name in ("echosmonitor", "echosmonitor.exe"):
        candidate = root / name
        if candidate.exists():
            return candidate
    return root / "echosmonitor"


@pytest.mark.skipif(
    not _packaged_binary().exists(),
    reason="no local PyInstaller bundle in dist/ (run scripts/build.sh first)",
)
def test_packaged_binary_check(tmp_path: Path) -> None:
    """When a local bundle exists, the built binary starts and exits clean.

    Uses ``--check`` (exit-code only, no stdout dependency) so it is portable to
    the Windows GUI-subsystem build where ``--version`` has nowhere to print.
    Skipped unless ``scripts/build.sh`` has produced ``dist/echosmonitor/``; the
    CI release job runs this against every platform's artifact (M7-E).
    """
    proc = subprocess.run(
        [str(_packaged_binary()), "--check"],
        env=_sandbox_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
