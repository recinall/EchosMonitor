"""Tests for the M6.6-D log-text export helper (rule 8 atomic write)."""

from __future__ import annotations

from pathlib import Path

import pytest

from echosmonitor.storage.log_export import LogExportError, write_log_text


def test_write_log_text_roundtrips(tmp_path: Path) -> None:
    target = tmp_path / "echosmonitor.log"
    text = "line one\nline two\n"
    n = write_log_text(text, target)
    assert target.read_text(encoding="utf-8") == text
    assert n == len(text.encode("utf-8"))


def test_write_log_text_no_partial_on_failure(tmp_path: Path) -> None:
    """A failed write leaves neither a partial destination nor a stray .tmp."""
    # A directory that does not exist makes os.replace/open fail.
    target = tmp_path / "missing_dir" / "out.log"
    with pytest.raises(LogExportError):
        write_log_text("data", target)
    assert not target.exists()
    assert not (tmp_path / "missing_dir" / "out.log.tmp").exists()
