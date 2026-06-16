"""Log-tab text export (M6.6-D, rule 8).

The Log tab's "Export…" write lives here in ``storage/`` (not in ``gui/``),
following the repo's canonical atomic recipe (skill ``miniseed-sds``): temp
file in the same directory → flush + fsync → ``os.replace``. A failed write
leaves NO partial file at the destination. Pure I/O over a string the caller
already rendered (rule 2: no Qt, no engine state).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

_log = structlog.get_logger(__name__)


class LogExportError(Exception):
    """The log text could not be written completely to the destination."""


def write_log_text(text: str, path: Path) -> int:
    """Write ``text`` to ``path`` atomically; return bytes written.

    Raises:
        LogExportError: the filesystem write failed (the temp file is
            removed so no partial artifact remains at ``path``).
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        n_bytes = tmp.stat().st_size
        os.replace(tmp, path)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort cleanup
            _log.warning("log_export_tmp_unlink_failed", path=str(tmp))
        raise LogExportError(f"log export failed: {exc}") from exc
    _log.info("log_exported", path=str(path), n_bytes=n_bytes)
    return int(n_bytes)


__all__ = ["LogExportError", "write_log_text"]
