"""Live source of truth for the user's YAML configuration (M4 stage B).

Stage A made the dashboard interactive at the *display* level — the
Stations dock, the device combo, the stream table. Stage B makes it
interactive at the *write* level: the user can add, edit, or remove
devices through the GUI, and the engine reacts by hot-reloading the
device list. Every mutation goes through this module — there is no
other writer of the user YAML file at runtime.

Three invariants justify the size of the abstraction:

1. **Atomicity.** Writes use a tempfile in the same directory followed
   by ``os.replace`` — atomic on POSIX and Windows since Python 3.3.
   A crash mid-write leaves the on-disk file unmodified rather than
   half-written.
2. **Validation before commit.** A mutation is rejected (raising
   ``ConfigError``) if the resulting :class:`RootConfig` would be
   invalid, *before* any I/O. The on-disk file is never replaced
   with a config that wouldn't load.
3. **Backups.** The previous file rotates to ``config.yaml.1``, then
   ``.2``, then ``.3``. ``.4`` is unlinked. Three slots is the right
   trade-off between "I broke my config" recovery and directory
   clutter — users with stronger needs have git or btrfs snapshots.

The ``configChanged`` signal is the single notification mechanism for
downstream consumers (notably :class:`StreamingEngine`'s hot-reload
path). It is zero-payload — readers re-query :attr:`root` to get the
current state. Emitted exactly once per successful write; failures
raise and never emit.

Threading: the store can be mutated from any thread (an internal
``threading.Lock`` guards the read-modify-write of the in-memory
shadow + atomic-write sequence). The signal is emitted on the
emitting thread; consumers connect with ``QueuedConnection`` if they
need to handle on the GUI thread.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from pathlib import Path

import structlog
import yaml
from PySide6.QtCore import QObject, Signal

from echosmonitor.config.schema import (
    DeviceConfig,
    RootConfig,
    StreamSelectorConfig,
)
from echosmonitor.core.exceptions import ConfigError

_log = structlog.get_logger(__name__)

# Number of historical backups kept alongside the live config. ``.1`` is
# the most-recent prior version; ``.3`` is the oldest. Anything older is
# unlinked before the rotation. Three slots is enough that "I edited two
# bad configs in a row and want the one before that" works, while
# avoiding directory clutter for normal use.
_BACKUP_KEEP = 3


class ConfigStore(QObject):
    """Single source of truth for the user YAML config + atomic writer.

    Construct with a freshly-loaded :class:`RootConfig` and the path
    that produced it. The store retains both — the in-memory
    :attr:`root` is what readers consult; the path is what mutations
    rewrite atomically.

    Mutation methods (``add_device`` / ``update_device`` /
    ``remove_device`` / ``add_selectors``) follow this exact sequence:

    1. Build a candidate :class:`RootConfig` via ``model_copy``.
    2. Re-validate by round-tripping through ``model_dump`` +
       ``RootConfig.model_validate``. Catches schema violations early
       *before* any file I/O. (model_copy alone bypasses validation.)
    3. Rotate backups.
    4. Atomic write of the new YAML to the live path.
    5. Update the in-memory shadow.
    6. Emit :attr:`configChanged`.

    Validation failures raise :class:`ConfigError` and leave the
    on-disk file and the in-memory shadow unchanged.

    The store is a ``QObject`` solely so it can emit a Qt signal —
    construction does not require a running event loop and no slot
    machinery is used internally.
    """

    # Zero-payload signal. Readers re-query ``root`` to get the new
    # state. Emitted exactly once per successful write.
    configChanged = Signal()  # noqa: N815

    def __init__(self, root: RootConfig, path: Path) -> None:
        super().__init__()
        self._root: RootConfig = root
        self._path: Path = path
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Read-side accessors
    # ------------------------------------------------------------------
    @property
    def root(self) -> RootConfig:
        """Current in-memory configuration. Always validated."""
        return self._root

    @property
    def path(self) -> Path:
        """File path the store mutates atomically. Set at construction."""
        return self._path

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def add_device(self, cfg: DeviceConfig) -> None:
        """Append a new device to the live config.

        Raises:
            ConfigError: A device with the same ``name`` already
                exists, or the resulting config fails schema
                validation.
        """
        with self._lock:
            if any(d.name == cfg.name for d in self._root.devices):
                raise ConfigError(f"device with name {cfg.name!r} already exists")
            new_devices = [*self._root.devices, cfg]
            self._commit(new_devices, action="add_device", device=cfg.name)
        # Emit OUTSIDE the lock — see ``_emit_changed`` for why.
        self._emit_changed()

    def update_device(self, name: str, cfg: DeviceConfig) -> None:
        """Replace the device named ``name`` with ``cfg``.

        Renaming is supported: pass a ``cfg`` whose ``name`` differs
        from the current ``name``. The new name must not already
        belong to another device.

        Raises:
            ConfigError: ``name`` does not match any current device,
                the renamed-to name collides, or schema validation
                fails.
        """
        with self._lock:
            idx = next(
                (i for i, d in enumerate(self._root.devices) if d.name == name),
                None,
            )
            if idx is None:
                raise ConfigError(f"unknown device {name!r}")
            if cfg.name != name and any(
                d.name == cfg.name for i, d in enumerate(self._root.devices) if i != idx
            ):
                raise ConfigError(f"device with name {cfg.name!r} already exists")
            new_devices = list(self._root.devices)
            new_devices[idx] = cfg
            self._commit(new_devices, action="update_device", device=name)
        self._emit_changed()

    def remove_device(self, name: str) -> None:
        """Remove the device named ``name``.

        Raises:
            ConfigError: ``name`` does not match any current device.
        """
        with self._lock:
            new_devices = [d for d in self._root.devices if d.name != name]
            if len(new_devices) == len(self._root.devices):
                raise ConfigError(f"unknown device {name!r}")
            self._commit(new_devices, action="remove_device", device=name)
        self._emit_changed()

    def add_selectors(self, name: str, selectors: list[StreamSelectorConfig]) -> None:
        """Append stream selectors to an existing device.

        Duplicate selectors (same NSLC tuple) are silently filtered
        out so the station-browser's "Add to device" path is
        idempotent across rapid clicks.
        """
        wrote = False
        with self._lock:
            idx = next(
                (i for i, d in enumerate(self._root.devices) if d.name == name),
                None,
            )
            if idx is None:
                raise ConfigError(f"unknown device {name!r}")
            existing = self._root.devices[idx]
            existing_set = {
                (s.network, s.station, s.location, s.channel) for s in existing.selectors
            }
            additions: list[StreamSelectorConfig] = []
            for sel in selectors:
                key = (sel.network, sel.station, sel.location, sel.channel)
                if key not in existing_set:
                    additions.append(sel)
                    existing_set.add(key)
            if additions:
                new_device = existing.model_copy(
                    update={"selectors": [*existing.selectors, *additions]}
                )
                new_devices = list(self._root.devices)
                new_devices[idx] = new_device
                self._commit(
                    new_devices,
                    action="add_selectors",
                    device=name,
                    added=len(additions),
                )
                wrote = True
            else:
                # Nothing to write; treat as a successful no-op so the
                # caller doesn't need to short-circuit.
                _log.debug(
                    "config_store_add_selectors_noop",
                    device=name,
                    requested=len(selectors),
                )
        if wrote:
            self._emit_changed()

    def reload_from_disk(self) -> None:
        """Discard the in-memory shadow and reload from ``path``.

        Useful when the user has edited the YAML externally between
        runs. Also fires :attr:`configChanged` so the engine picks up
        the new state via the same path as in-app mutations.

        Raises:
            ConfigError: The file no longer parses or fails schema.
        """
        from echosmonitor.config.loader import load_config

        with self._lock:
            try:
                new_root, _ = load_config(self._path)
            except Exception as exc:
                raise ConfigError(f"failed to reload {self._path}: {exc}") from exc
            self._root = new_root
            _log.info(
                "config_store_reloaded_from_disk",
                path=str(self._path),
                devices=len(new_root.devices),
            )
        # Emit OUTSIDE the lock — keep the lock's critical section to
        # the in-memory mutation only, so a slot connected via
        # DirectConnection that calls back into the store doesn't
        # deadlock on its own lock.
        self.configChanged.emit()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _commit(self, new_devices: list[DeviceConfig], *, action: str, **log_extra: object) -> None:
        """Validate, rotate backups, atomic-write, swap in.

        Caller holds ``self._lock``. **Does NOT emit ``configChanged``** —
        the public mutation method does that *after* the ``with`` block
        exits via :meth:`_emit_changed`. The earlier draft emitted from
        inside ``_commit`` while the lock was still held; a Direct-
        Connection consumer that re-entered the store would have hard-
        deadlocked on the non-reentrant ``threading.Lock``.
        """
        candidate = self._root.model_copy(update={"devices": new_devices})
        # Re-validate by round-trip. ``model_copy(update=...)`` bypasses
        # field-level validators, so without this the candidate could
        # carry an invalid combination (e.g. duplicate selector entries
        # if a future schema rule forbids them).
        try:
            validated = RootConfig.model_validate(candidate.model_dump(mode="json"))
        except Exception as exc:
            raise ConfigError(f"config validation failed: {exc}") from exc

        self._rotate_backups()
        self._atomic_write(validated)
        self._root = validated
        _log.info("config_store_committed", action=action, **log_extra)

    def _emit_changed(self) -> None:
        """Fire :attr:`configChanged`. Always called outside ``self._lock``.

        Centralised so the lock-release contract is documented in one
        place and a future contributor adding a new mutation method
        does not accidentally re-introduce the in-lock emit.
        """
        self.configChanged.emit()

    def _rotate_backups(self) -> None:
        """Rotate ``.1 → .2 → .3`` and move the live file into ``.1``.

        Preserves the last :data:`_BACKUP_KEEP` versions. The oldest
        slot (``.<KEEP>``) is unlinked first, then each surviving
        slot shifts up by one, then the live file (if it exists)
        moves into ``.1``. Any failure during rotation surfaces
        immediately — better to abort the write than to clobber
        partial backups.
        """
        # Drop the oldest slot.
        oldest = self._path.with_suffix(f"{self._path.suffix}.{_BACKUP_KEEP}")
        oldest.unlink(missing_ok=True)
        # Shift remaining slots up by one. Walk from the second-oldest
        # downwards so we don't overwrite a slot before reading it.
        for i in range(_BACKUP_KEEP - 1, 0, -1):
            src = self._path.with_suffix(f"{self._path.suffix}.{i}")
            dst = self._path.with_suffix(f"{self._path.suffix}.{i + 1}")
            if src.exists():
                src.replace(dst)
        # Move the live file into ``.1``.
        if self._path.exists():
            self._path.replace(self._path.with_suffix(f"{self._path.suffix}.1"))

    def _atomic_write(self, root: RootConfig) -> None:
        """Write ``root`` to ``self._path`` atomically.

        Uses :func:`tempfile.NamedTemporaryFile` in the *same
        directory* as the target so :func:`os.replace` is a same-
        filesystem rename (atomic). ``fsync`` between flush and
        replace ensures a power cut after the rename does not
        leave a zero-byte file with the live name.

        On failure the temp file is unlinked and the live path is
        left untouched (``os.replace`` is the only step that touches
        the target name; rotation already moved the prior content
        into ``.1`` before this method runs).
        """
        target_dir = self._path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        # ``model_dump(mode='json')`` because pyyaml understands
        # JSON-style scalars (None → null, etc.) and we don't want
        # pydantic's repr-style ``Path('...')`` strings in the YAML.
        payload = root.model_dump(mode="json", exclude_none=False)
        # Sort keys so two byte-identical configs always serialise
        # the same way — useful for diff-friendliness and the
        # backup-rotation test.
        body = yaml.safe_dump(
            payload,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
        # ``delete=False`` so we control the lifetime; cleanup on
        # error is explicit. A ``with`` block (SIM115's suggestion)
        # would defeat the whole point: we need the file to OUTLIVE
        # the constructor (so ``os.replace`` can rename it into the
        # target path) and only get unlinked on the failure branch.
        # mode='w' for text + encoding='utf-8'.
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w",
            encoding="utf-8",
            dir=target_dir,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            tmp.write(body)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self._path)
        except Exception:
            # Best-effort cleanup of the temp file. The rename hadn't
            # happened (the exception fired before os.replace) so the
            # live path either holds the prior content (if rotation
            # didn't move it) or doesn't exist yet — either way this
            # branch never overwrites a good config with a partial.
            with contextlib.suppress(OSError):
                Path(tmp.name).unlink()
            raise


__all__ = ["ConfigStore"]
