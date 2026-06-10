"""Device admin-credential store (rule 15; skill: echos-rest-api).

Echos device passwords NEVER live in the YAML config, and never appear
in logs or exception messages. The primary backend is the OS keyring
(Secret Service / macOS Keychain / Windows Credential Locker via the
``keyring`` package). When no usable keyring backend exists, the store
falls back to a plain-JSON file under the app data dir — the skill's
"NVS-style local file": chmod 0600, atomic writes (temp in same dir →
fsync → ``os.replace``), and a loud one-time warning per process that
the password is on disk unencrypted.

Lookup key: the device's config ``name`` (the rule-15 "credentials
reference" — the YAML stores host/port, the store maps name → password).

Read order is keyring first, then the fallback file: a password saved
while the keyring was broken stays retrievable after the keyring heals.
A successful keyring write removes any stale fallback-file copy so the
plaintext file never outlives a working keyring.

Threading: keyring backends can block on D-Bus round-trips or OS unlock
prompts. Never call this from the GUI thread (rule 1) — the M1-C poller
and M1-D dialog run it on their worker thread.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Protocol

import keyring
import platformdirs
import structlog

_log = structlog.get_logger(__name__)


# Keyring service namespace; the username slot carries the device key.
_SERVICE = "echosmonitor"

# Fallback file name under the app data dir. JSON object: device key →
# password. Plain text by design (the skill's NVS-style fallback) —
# hence the 0600 mode and the loud warning.
_FALLBACK_FILE = "credentials.json"

# Mode bits for the fallback file: owner read/write only.
_FALLBACK_MODE = 0o600


class _KeyringLike(Protocol):
    """Structural type of the ``keyring`` module (injectable for tests)."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class CredentialsStore:
    """Keyring-backed password store with a chmod-600 file fallback.

    All methods are best-effort against the keyring: any backend failure
    (no backend, locked daemon, D-Bus error) degrades to the fallback
    file instead of raising, because "cannot talk to the keyring" must
    not strand the user's devices. Backend errors are logged by exception
    *type only* — never their message, which on some backends echoes call
    arguments (rule 15).
    """

    def __init__(
        self,
        *,
        fallback_dir: Path | None = None,
        keyring_module: _KeyringLike | None = None,
    ) -> None:
        self._keyring: _KeyringLike = keyring if keyring_module is None else keyring_module
        base = (
            Path(platformdirs.user_data_dir("echosmonitor", "EchosMonitor"))
            if fallback_dir is None
            else fallback_dir
        )
        self._fallback_path = base / _FALLBACK_FILE
        # Loud warning emitted at most once per store instance (the app
        # holds one), so a multi-device save doesn't spam the log.
        self._fallback_warned = False

    @property
    def fallback_path(self) -> Path:
        """Where the plaintext fallback lives (for UI warnings, never logs)."""
        return self._fallback_path

    def get_password(self, device_key: str) -> str | None:
        """Return the stored password for ``device_key``, or ``None``."""
        try:
            password = self._keyring.get_password(_SERVICE, device_key)
        except Exception as exc:  # broad by design — degrade, never strand (class docstring)
            self._warn_keyring_unavailable(exc, operation="get")
            password = None
        if password is not None:
            return password
        return self._read_fallback().get(device_key)

    def set_password(self, device_key: str, password: str) -> None:
        """Store ``password`` for ``device_key`` (keyring, else file)."""
        try:
            self._keyring.set_password(_SERVICE, device_key, password)
        except Exception as exc:  # broad by design — degrade, never strand (class docstring)
            self._warn_keyring_unavailable(exc, operation="set")
            entries = self._read_fallback()
            entries[device_key] = password
            self._write_fallback(entries)
            return
        _log.info("credentials_stored", device=device_key, backend="keyring")
        # A working keyring now owns this secret: drop any stale plaintext
        # copy left over from a fallback-era save.
        entries = self._read_fallback()
        if device_key in entries:
            del entries[device_key]
            self._write_fallback(entries)
            _log.info("credentials_fallback_copy_removed", device=device_key)

    def delete_password(self, device_key: str) -> None:
        """Remove the password for ``device_key`` from both backends.

        Idempotent: deleting a key that was never stored is not an error.
        """
        try:
            self._keyring.delete_password(_SERVICE, device_key)
        except Exception as exc:  # broad by design — "not stored" and "no backend" are both fine
            _log.debug(
                "credentials_keyring_delete_skipped",
                device=device_key,
                error_type=type(exc).__name__,
            )
        entries = self._read_fallback()
        if device_key in entries:
            del entries[device_key]
            self._write_fallback(entries)
        _log.info("credentials_deleted", device=device_key)

    # -- fallback file -----------------------------------------------------

    def _read_fallback(self) -> dict[str, str]:
        try:
            raw = self._fallback_path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            _log.warning(
                "credentials_file_unreadable",
                path=str(self._fallback_path),
                error_type=type(exc).__name__,
            )
            return {}
        self._fix_loose_permissions()
        try:
            entries = json.loads(raw)
        except ValueError:
            _log.warning(
                "credentials_file_corrupt",
                path=str(self._fallback_path),
                hint="treating as empty; next save overwrites it",
            )
            return {}
        if not isinstance(entries, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in entries.items()
        ):
            _log.warning("credentials_file_corrupt", path=str(self._fallback_path))
            return {}
        return entries

    def _write_fallback(self, entries: dict[str, str]) -> None:
        """Atomic 0600 write: temp in same dir → fsync → ``os.replace``."""
        directory = self._fallback_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        tmp_path = self._fallback_path.with_name(self._fallback_path.name + ".tmp")
        # A hard kill between open and replace can leave a stale temp file
        # behind; without this unlink, O_EXCL would then fail every future
        # write forever (and the stale temp could hold an unpurgeable
        # secret). Deterministic name + single-writer app make it safe.
        tmp_path.unlink(missing_ok=True)
        # O_EXCL + explicit 0600 so the secret is never readable by others,
        # not even between create and chmod.
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FALLBACK_MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._fallback_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def _fix_loose_permissions(self) -> None:
        """Re-tighten the fallback file to 0600 if it grew loose (POSIX only)."""
        if os.name != "posix":
            return
        try:
            mode = stat.S_IMODE(self._fallback_path.stat().st_mode)
            if mode & 0o077:
                os.chmod(self._fallback_path, _FALLBACK_MODE)
                _log.warning(
                    "credentials_file_permissions_tightened",
                    path=str(self._fallback_path),
                    found_mode=oct(mode),
                )
        except FileNotFoundError:
            # Unlinked between read and stat/chmod (e.g. concurrent
            # delete) — nothing left to tighten.
            return

    def _warn_keyring_unavailable(self, exc: Exception, *, operation: str) -> None:
        if self._fallback_warned:
            return
        self._fallback_warned = True
        _log.warning(
            "credentials_keyring_unavailable",
            operation=operation,
            error_type=type(exc).__name__,
            fallback_path=str(self._fallback_path),
            hint=(
                "device passwords will be stored UNENCRYPTED in the fallback "
                "file (owner-only 0600); install/unlock an OS keyring backend "
                "to store them securely"
            ),
        )
