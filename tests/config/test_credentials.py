"""Tests for ``config/credentials.py`` (rule 15).

The real OS keyring is NEVER touched: every test injects either an
in-memory fake or a deliberately broken backend. Pins: keyring-first
read order, the chmod-600 atomic file fallback with its one-time loud
warning, stale-plaintext cleanup after the keyring heals, idempotent
delete, corrupt-file resilience, and the no-credentials-in-logs rule.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from echosmonitor.config.credentials import CredentialsStore

_SECRET = "s3cret-Adm1n-pw"


class FakeKeyring:
    """In-memory stand-in for the ``keyring`` module."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.store.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.store[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        if (service_name, username) not in self.store:
            raise RuntimeError("no such password")
        del self.store[(service_name, username)]


class BrokenKeyring:
    """A backend with no usable keyring (e.g. headless Linux, no daemon)."""

    def get_password(self, service_name: str, username: str) -> str | None:
        raise RuntimeError("no keyring backend")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise RuntimeError("no keyring backend")

    def delete_password(self, service_name: str, username: str) -> None:
        raise RuntimeError("no keyring backend")


@pytest.fixture
def fake_keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def keyring_store(tmp_path: Path, fake_keyring: FakeKeyring) -> CredentialsStore:
    return CredentialsStore(fallback_dir=tmp_path, keyring_module=fake_keyring)


@pytest.fixture
def file_store(tmp_path: Path) -> CredentialsStore:
    return CredentialsStore(fallback_dir=tmp_path, keyring_module=BrokenKeyring())


# ----------------------------------------------------------------------
# Keyring path
# ----------------------------------------------------------------------


def test_keyring_roundtrip(keyring_store: CredentialsStore, tmp_path: Path) -> None:
    assert keyring_store.get_password("echos-field-01") is None
    keyring_store.set_password("echos-field-01", _SECRET)
    assert keyring_store.get_password("echos-field-01") == _SECRET
    # The fallback file must not exist when the keyring works.
    assert not keyring_store.fallback_path.exists()


def test_keyring_delete_is_idempotent(keyring_store: CredentialsStore) -> None:
    keyring_store.set_password("dev", _SECRET)
    keyring_store.delete_password("dev")
    assert keyring_store.get_password("dev") is None
    keyring_store.delete_password("dev")  # second delete: no raise


def test_keys_are_independent(keyring_store: CredentialsStore) -> None:
    keyring_store.set_password("dev-a", "password-a")
    keyring_store.set_password("dev-b", "password-b")
    keyring_store.delete_password("dev-a")
    assert keyring_store.get_password("dev-a") is None
    assert keyring_store.get_password("dev-b") == "password-b"


# ----------------------------------------------------------------------
# File fallback path
# ----------------------------------------------------------------------


def test_fallback_roundtrip_with_0600(file_store: CredentialsStore) -> None:
    file_store.set_password("echos-field-01", _SECRET)
    assert file_store.get_password("echos-field-01") == _SECRET
    path = file_store.fallback_path
    assert path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # On-disk format: plain JSON object key → password.
    assert json.loads(path.read_text(encoding="utf-8")) == {"echos-field-01": _SECRET}


def test_fallback_warns_loudly_exactly_once(
    file_store: CredentialsStore, capture_structlog: list[dict[str, Any]]
) -> None:
    file_store.set_password("dev-a", "password-a")
    file_store.set_password("dev-b", "password-b")
    file_store.get_password("dev-a")
    warnings = [r for r in capture_structlog if r["event"] == "credentials_keyring_unavailable"]
    assert len(warnings) == 1
    assert warnings[0]["fallback_path"] == str(file_store.fallback_path)


def test_fallback_survives_corrupt_file(
    file_store: CredentialsStore, capture_structlog: list[dict[str, Any]]
) -> None:
    file_store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
    file_store.fallback_path.write_text("{not json", encoding="utf-8")
    assert file_store.get_password("dev") is None
    assert any(r["event"] == "credentials_file_corrupt" for r in capture_structlog)
    # A save overwrites the corrupt file and recovers.
    file_store.set_password("dev", _SECRET)
    assert file_store.get_password("dev") == _SECRET


def test_fallback_rejects_non_dict_payload(file_store: CredentialsStore) -> None:
    file_store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
    file_store.fallback_path.write_text('["a", "list"]', encoding="utf-8")
    assert file_store.get_password("dev") is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_loose_permissions_are_tightened(
    file_store: CredentialsStore, capture_structlog: list[dict[str, Any]]
) -> None:
    file_store.set_password("dev", _SECRET)
    os.chmod(file_store.fallback_path, 0o644)
    assert file_store.get_password("dev") == _SECRET
    assert stat.S_IMODE(file_store.fallback_path.stat().st_mode) == 0o600
    assert any(
        r["event"] == "credentials_file_permissions_tightened" for r in capture_structlog
    )


def test_fallback_recovers_from_stale_temp_file(file_store: CredentialsStore) -> None:
    # A hard kill between open and replace leaves the temp file behind;
    # the next write must clean it up and succeed instead of failing on
    # O_EXCL forever (code-review finding, M1-B).
    stale = file_store.fallback_path.with_name(file_store.fallback_path.name + ".tmp")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"dev": "half-written-old-secret"}', encoding="utf-8")
    file_store.set_password("dev", _SECRET)
    assert file_store.get_password("dev") == _SECRET
    # The stale temp (which may hold a secret) is gone after the write.
    assert not stale.exists()


def test_fallback_delete_is_idempotent(file_store: CredentialsStore) -> None:
    file_store.set_password("dev", _SECRET)
    file_store.delete_password("dev")
    assert file_store.get_password("dev") is None
    assert json.loads(file_store.fallback_path.read_text(encoding="utf-8")) == {}
    file_store.delete_password("dev")  # no raise, no file required


# ----------------------------------------------------------------------
# Mixed backend transitions
# ----------------------------------------------------------------------


def test_read_falls_back_to_file_when_keyring_has_no_entry(
    tmp_path: Path, fake_keyring: FakeKeyring
) -> None:
    # Password saved while the keyring was broken…
    CredentialsStore(fallback_dir=tmp_path, keyring_module=BrokenKeyring()).set_password(
        "dev", _SECRET
    )
    # …stays retrievable once a (still empty) keyring is available.
    healed = CredentialsStore(fallback_dir=tmp_path, keyring_module=fake_keyring)
    assert healed.get_password("dev") == _SECRET


def test_keyring_set_removes_stale_plaintext_copy(
    tmp_path: Path, fake_keyring: FakeKeyring
) -> None:
    CredentialsStore(fallback_dir=tmp_path, keyring_module=BrokenKeyring()).set_password(
        "dev", "old-password"
    )
    healed = CredentialsStore(fallback_dir=tmp_path, keyring_module=fake_keyring)
    healed.set_password("dev", _SECRET)
    assert fake_keyring.store[("echosmonitor", "dev")] == _SECRET
    # The plaintext copy must not outlive the working keyring.
    assert json.loads(healed.fallback_path.read_text(encoding="utf-8")) == {}
    assert healed.get_password("dev") == _SECRET


def test_delete_clears_both_backends(tmp_path: Path, fake_keyring: FakeKeyring) -> None:
    store = CredentialsStore(fallback_dir=tmp_path, keyring_module=fake_keyring)
    fake_keyring.store[("echosmonitor", "dev")] = _SECRET
    store._write_fallback({"dev": _SECRET})  # simulate a stale plaintext copy
    store.delete_password("dev")
    assert fake_keyring.store == {}
    assert store.get_password("dev") is None


# ----------------------------------------------------------------------
# Rule 15: credentials never reach the log stream
# ----------------------------------------------------------------------


def test_passwords_never_logged(
    tmp_path: Path, capture_structlog: list[dict[str, Any]]
) -> None:
    for module in (FakeKeyring(), BrokenKeyring()):
        store = CredentialsStore(fallback_dir=tmp_path, keyring_module=module)
        store.set_password("dev", _SECRET)
        store.get_password("dev")
        store.delete_password("dev")
    assert _SECRET not in repr(capture_structlog)
