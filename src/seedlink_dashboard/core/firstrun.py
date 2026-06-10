"""First-run detection for the GUI bootstrap (M4 stage C).

A "first run" is the case where launching the dashboard with the
**bundled defaults** would leave the user staring at an empty UI with
no obvious next step. We surface a wizard in that case (see
:mod:`seedlink_dashboard.gui.dialogs.first_run_wizard`) and skip
straight to the main window in every other case.

Definition (pinned by :func:`is_first_run`):

    no user config file on disk  AND  zero devices in the loaded config

Both halves matter:

* ``len(devices) == 0`` alone is wrong — a sophisticated user can
  legitimately keep an empty config (e.g. they uncomment devices
  conditionally via tooling), and the wizard should not interrupt them
  on every launch.
* "user config file does not exist" alone is wrong — automated installs
  (Ansible, Nix, MDM) can drop a populated YAML before first launch;
  triggering the wizard there would lose their config.

The conjunction matches "truly fresh install" and only that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from seedlink_dashboard.config import RootConfig


def is_first_run(user_config_path: Path, root: RootConfig) -> bool:
    """Return ``True`` iff the GUI should show the first-run wizard.

    Pure: no I/O beyond ``Path.exists()``, no Qt, no global state.
    Trivial to unit-test against the four-cell truth table.

    Args:
        user_config_path: Path the loader resolved (or *would* resolve
            to in the no-explicit-config case). Even when the loader
            fell back to the bundled default, this is the path at
            which a user-overrides file *would* live.
        root: The :class:`RootConfig` the loader handed back.

    Returns:
        ``True`` only when **no** user file exists on disk **and** the
        loaded config has zero configured devices.
    """
    return not user_config_path.exists() and len(root.devices) == 0


__all__ = ["is_first_run"]
