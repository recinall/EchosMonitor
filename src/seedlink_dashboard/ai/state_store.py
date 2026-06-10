"""Learned-state persistence for fit-then-infer agents (M10 Stage B).

A small, torch-free store that persists an :class:`~seedlink_dashboard.ai.
base.AIAgent`'s serialised state bytes under ``<data_dir>/models/``, keyed
by ``(kind, device, nslc)``. The bytes are produced by the agent's
:meth:`~seedlink_dashboard.ai.base.AIAgent.serialize_state` and consumed by
:meth:`~seedlink_dashboard.ai.base.AIAgent.load_state`; the store treats
them as opaque (it never learns the agent's format).

**Not in the SDS archive.** Learned state is *derived* from the science
data, not science data itself — re-fitting reproduces it. It therefore
lives under ``models/`` next to (but separate from) the ``archive/`` SDS
tree and the metadata DB, and is intentionally NOT covered by the M5
crash-safety / gap-detection contract that applies to recorded waveforms.

Atomic writes (M5 discipline): :meth:`save` writes to a temp file in the
same directory, ``fsync``s it, then ``os.replace``s it over the target, so
a crash never leaves a half-written state file that ``load`` would
mis-deserialise.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

# Sub-directory of the app data_dir that holds learned state.
_MODELS_SUBDIR = "models"

# Extension for a serialised-state file.
_STATE_SUFFIX = ".state"

# Characters that are path-hostile or part of the "<kind>__<device>__<nslc>"
# field separator scheme are replaced by a single underscore. We replace any
# run of non-[alnum-] with "_", which collapses the NSLC dots ("NET.STA.LOC.
# CHA") and any "/" to "_" while keeping the field order legible and the
# mapping deterministic.
_SLUG_RE = re.compile(r"[^0-9A-Za-z-]+")

# Field separator between (kind, device, nslc) in the filename. A DOUBLE
# underscore so it cannot collide with the single underscores produced by
# slugging the individual fields (the slug regex never emits two in a row).
_FIELD_SEP = "__"


def _slug(value: str) -> str:
    """Slugify one key field to a deterministic, path-safe token.

    Replaces every run of characters outside ``[0-9A-Za-z-]`` with a single
    ``_`` (so ``NET.STA.LOC.CHA`` → ``NET_STA_LOC_CHA`` and ``/`` → ``_``)
    and trims leading/trailing underscores. Collision-resistant within the
    three-field ``<kind>__<device>__<nslc>`` scheme because the
    double-underscore field separator can never appear inside a field's slug.
    """
    return _SLUG_RE.sub("_", value).strip("_") or "_"


class StateStore:
    """Persists agent learned-state bytes under ``<base_dir>/models/``.

    Constructed with the app data_dir (resolved by the caller the same way
    the storage layer resolves the archive root — see
    :class:`~seedlink_dashboard.core.ai_engine.AIEngine`). All keys are
    ``(kind, device, nslc)``; the filename scheme is
    ``<kind>__<device>__<nslc>.state`` with each field slugged.
    """

    def __init__(self, base_dir: Path) -> None:
        self._models_dir = Path(base_dir) / _MODELS_SUBDIR

    def path_for(self, kind: str, device: str, nslc: str) -> Path:
        """Deterministic absolute path for the ``(kind, device, nslc)`` state.

        Filename scheme: ``<kind>__<device>__<nslc>.state`` with each field
        slugged via :func:`_slug` (non-alnum runs → ``_``). The double
        underscore separates fields so it cannot collide with a slugged
        field.
        """
        name = _FIELD_SEP.join((_slug(kind), _slug(device), _slug(nslc)))
        return self._models_dir / f"{name}{_STATE_SUFFIX}"

    def save(self, kind: str, device: str, nslc: str, data: bytes) -> Path:
        """Atomically persist ``data`` for ``(kind, device, nslc)``.

        Creates ``models/`` if missing, writes to a temp file in the same
        directory, ``fsync``s it, then ``os.replace``s it over the target
        (M5 atomic-write discipline). Returns the final path.
        """
        target = self.path_for(kind, device, nslc)
        self._models_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        _log.info(
            "ai_state_saved",
            kind=kind,
            device=device,
            nslc=nslc,
            path=str(target),
            bytes=len(data),
        )
        return target

    def load(self, kind: str, device: str, nslc: str) -> bytes | None:
        """Return the persisted state bytes, or ``None`` if absent."""
        target = self.path_for(kind, device, nslc)
        if not target.exists():
            return None
        return target.read_bytes()

    def has(self, kind: str, device: str, nslc: str) -> bool:
        """Whether persisted state exists for ``(kind, device, nslc)``."""
        return self.path_for(kind, device, nslc).exists()
