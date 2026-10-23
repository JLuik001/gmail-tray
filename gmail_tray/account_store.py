"""Saving settings together with account passwords, in a safe order.

Secrets are written before the config that refers to them, removed accounts'
secrets are wiped, and switching storage backends moves passwords across
without ever leaving a copy behind in the old backend.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from . import config as config_mod
from . import fetchmail
from .config import Account, Config, ConfigError, managed_rc_path

log = logging.getLogger(__name__)

Key = tuple[str, str]


class _Backend:
    """Indirection so tests can swap the keyring for a dict."""
    lookup: Callable[[Account], str | None]
    store: Callable[[Account, str], None]
    clear: Callable[[Account], None]


def keyring_backend() -> _Backend:
    from . import secret_store
    b = _Backend()
    b.lookup, b.store, b.clear = secret_store.lookup, secret_store.store, secret_store.clear
    return b


def read_managed_rc(path: Path | None = None) -> dict[Key, str]:
    path = path or managed_rc_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return {acc.key: pw for acc, pw in fetchmail.parse_rc(text)}


def current_password(cfg: Config, acc: Account, backend: _Backend | None = None) -> str | None:
    """Password currently stored for *acc* under *cfg*'s storage mode."""
    if cfg.password_store == "keyring":
        return (backend or keyring_backend()).lookup(acc)
    return read_managed_rc().get(acc.key)


def save_all(old: Config, new: Config, new_passwords: dict[Key, str],
             backend: _Backend | None = None, cfg_path: Path | None = None) -> list[str]:
    """Persist *new* config and passwords. Returns user-facing notes.

    *new_passwords* holds passwords typed or imported in this session; any
    account without one keeps the password it already has.
    """
    new.validate()
    notes: list[str] = []
    uses_keyring = "keyring" in (old.password_store, new.password_store)
    kr = (backend or keyring_backend()) if uses_keyring else None
    old_file = read_managed_rc() if old.managed and old.password_store == "fetchmailrc" else {}

    def existing(acc: Account) -> str | None:
        if old.managed and any(a.key == acc.key for a in old.accounts):
            if old.password_store == "keyring":
                return kr.lookup(acc)
            return old_file.get(acc.key)
        return None

    # 1. Resolve every password before touching anything.
    resolved: list[tuple[Account, str]] = []
    for acc in new.accounts:
        pw = new_passwords.get(acc.key) or existing(acc)
        if not pw:
            raise ConfigError(f"Enter the app password for {acc.user}")
        resolved.append((acc, pw))

    # 2. Write secrets to the new backend.
    if new.managed and new.password_store == "keyring":
        for acc, pw in resolved:
            kr.store(acc, pw)
    elif new.managed:
        config_mod.ensure_private_dir(managed_rc_path().parent)
        config_mod.atomic_write_private(managed_rc_path(), fetchmail.render_rc(resolved))

    # 3. Save the config itself.
    config_mod.save(new, cfg_path)

    # 4. Remove secrets that are no longer referenced.
    keep = {acc.key for acc in new.accounts} if new.password_store == "keyring" else set()
    if old.managed and old.password_store == "keyring":
        for acc in old.accounts:
            if acc.key not in keep:
                try:
                    kr.clear(acc)
                except Exception as e:
                    notes.append(f"Could not remove {acc.user} from the keyring: {e}")
    if not (new.managed and new.password_store == "fetchmailrc"):
        rc = managed_rc_path()
        if rc.exists() and fetchmail.is_generated(rc):
            rc.unlink()

    # 5. Warn about plaintext passwords left in a hand-written ~/.fetchmailrc.
    if new.managed and new.password_store == "keyring":
        legacy = new.fetchmailrc_path
        try:
            text = legacy.read_text(encoding="utf-8")
            if any(pw for _, pw in fetchmail.parse_rc(text)):
                notes.append(f"{legacy} still contains plaintext passwords. "
                             "Delete it if nothing else uses it.")
        except OSError:
            pass
    return notes


def legacy_rc_has_passwords(path: Path) -> bool:
    try:
        return any(pw for _, pw in fetchmail.parse_rc(path.read_text(encoding="utf-8")))
    except OSError:
        return False


def delete_legacy_rc(path: Path) -> None:
    """Remove a plaintext rc file at the user's request (a symlink is removed, not followed)."""
    os.unlink(path)
