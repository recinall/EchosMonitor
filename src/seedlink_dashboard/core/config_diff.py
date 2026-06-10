"""Pure diff between two device-config snapshots (M4 stage B).

Used by :class:`StreamingEngine` to react to :class:`ConfigStore`
mutations without restarting the whole engine. The diff splits a
mutation into the minimum work the engine has to do:

* :attr:`DeviceDiff.added` — devices that need a fresh worker.
* :attr:`DeviceDiff.removed` — names whose workers must be stopped.
* :attr:`DeviceDiff.restart` — devices whose host / port / selectors
  / connect-timeout changed; the running worker must be torn down and
  a new one spawned. The DSP chain change comes for free in the
  restart so we don't list it separately.
* :attr:`DeviceDiff.chain_only` — devices where ONLY the
  ``dsp_chain`` changed; the existing socket stays open and the
  router reinstalls the chain in place.

Comparison rules:

* Match by ``name``. A renamed device is one ``removed`` + one
  ``added`` — there's no "rename" action because the engine has no
  cheap way to migrate per-stream state across a name change.
* ``selectors`` are compared as a frozenset over the
  ``(net, sta, loc, cha)`` tuples, so reordering is a no-op.
* ``dsp_chain`` is compared as an ordered tuple — chain order is
  semantic (a detrend after a bandpass is not the same chain as
  a detrend before it).
* ``reconnect.connect_timeout_s`` is compared as part of the
  reconnect block; any change forces a restart because the timeout
  is evaluated only at connect time.

The function is pure: no Qt, no I/O, no global state. Trivial to
unit-test in isolation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from seedlink_dashboard.config.schema import DeviceConfig


@dataclass(frozen=True, slots=True)
class DeviceDiff:
    """Result of :func:`diff_devices`.

    Each tuple holds the values the engine needs to act on. A device
    appearing in ``restart`` will NOT also appear in ``chain_only``
    even if its chain changed — restart implicitly reinstalls the
    chain, so listing it twice would just cause duplicate work.
    """

    added: tuple[DeviceConfig, ...]
    removed: tuple[str, ...]
    restart: tuple[DeviceConfig, ...]
    chain_only: tuple[DeviceConfig, ...]

    @property
    def is_empty(self) -> bool:
        """``True`` iff the diff carries no work for the engine."""
        return not (self.added or self.removed or self.restart or self.chain_only)


def diff_devices(
    old: Sequence[DeviceConfig],
    new: Sequence[DeviceConfig],
) -> DeviceDiff:
    """Compute the minimum-work diff between ``old`` and ``new`` device lists.

    Args:
        old: Devices the engine is currently running.
        new: Devices the engine should be running after the mutation.

    Returns:
        A :class:`DeviceDiff` whose four buckets together describe the
        full transition. Devices unchanged across the two lists do not
        appear anywhere — empty buckets all around mean "no work".
    """
    old_by_name = {d.name: d for d in old}
    new_by_name = {d.name: d for d in new}

    added: list[DeviceConfig] = []
    removed: list[str] = []
    restart: list[DeviceConfig] = []
    chain_only: list[DeviceConfig] = []

    for name, new_cfg in new_by_name.items():
        old_cfg = old_by_name.get(name)
        if old_cfg is None:
            added.append(new_cfg)
            continue
        if _connection_differs(old_cfg, new_cfg):
            # Connection-level changes (host / port / selectors /
            # connect-timeout) require a fresh socket. The chain
            # comes along for free since a restart will reinstall it.
            restart.append(new_cfg)
        elif _chain_differs(old_cfg, new_cfg):
            chain_only.append(new_cfg)
        # else: identical, nothing to do.

    for name in old_by_name:
        if name not in new_by_name:
            removed.append(name)

    return DeviceDiff(
        added=tuple(added),
        removed=tuple(removed),
        restart=tuple(restart),
        chain_only=tuple(chain_only),
    )


def _connection_differs(old: DeviceConfig, new: DeviceConfig) -> bool:
    """Return ``True`` iff anything affecting the live socket changed.

    Selector list reordering is NOT a change — the SeedLink server
    serialises per-station regardless of the ``SELECT`` order — so we
    compare as a set. Chain changes are intentionally NOT considered
    here; the caller falls through to :func:`_chain_differs` only
    when the connection is unchanged.
    """
    if old.host != new.host or old.port != new.port:
        return True
    if old.reconnect != new.reconnect:
        return True
    old_set = frozenset((s.network, s.station, s.location, s.channel) for s in old.selectors)
    new_set = frozenset((s.network, s.station, s.location, s.channel) for s in new.selectors)
    return old_set != new_set


def _chain_differs(old: DeviceConfig, new: DeviceConfig) -> bool:
    """Return ``True`` iff ``dsp_chain`` changed in any meaningful way.

    Order matters: a detrend after a bandpass is not the same chain
    as a detrend before it, even though the stages may be the same.
    Pydantic models compare structurally so ``__eq__`` is the right
    primitive.
    """
    return list(old.dsp_chain) != list(new.dsp_chain)


__all__ = ["DeviceDiff", "diff_devices"]
