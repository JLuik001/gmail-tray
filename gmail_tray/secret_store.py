"""Passwords in the desktop keyring (Secret Service: GNOME Keyring, KWallet, KeePassXC…)."""

from __future__ import annotations

import logging

from .config import Account

log = logging.getLogger(__name__)

try:
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import GLib, Secret
    AVAILABLE = True
except (ImportError, ValueError):  # libsecret typelib missing
    AVAILABLE = False

SCHEMA_NAME = "ee.jluik.GmailTray.Account"


class SecretStoreError(RuntimeError):
    pass


def _schema():
    if not AVAILABLE:
        raise SecretStoreError("libsecret is not installed")
    return Secret.Schema.new(
        SCHEMA_NAME,
        Secret.SchemaFlags.NONE,
        {"user": Secret.SchemaAttributeType.STRING, "server": Secret.SchemaAttributeType.STRING},
    )


def _attrs(acc: Account) -> dict:
    return {"user": acc.user.lower(), "server": acc.server.lower()}


def lookup(acc: Account) -> str | None:
    try:
        return Secret.password_lookup_sync(_schema(), _attrs(acc), None)
    except GLib.Error as e:
        raise SecretStoreError(e.message) from e


def store(acc: Account, password: str) -> None:
    if not password:
        raise SecretStoreError("empty password")
    try:
        ok = Secret.password_store_sync(
            _schema(), _attrs(acc), Secret.COLLECTION_DEFAULT,
            f"Gmail Tray: {acc.user} ({acc.server})", password, None)
    except GLib.Error as e:
        raise SecretStoreError(e.message) from e
    if not ok:
        raise SecretStoreError("the keyring refused to store the password")


def clear(acc: Account) -> None:
    try:
        Secret.password_clear_sync(_schema(), _attrs(acc), None)
    except GLib.Error as e:
        raise SecretStoreError(e.message) from e


def usable() -> tuple[bool, str]:
    """Whether a Secret Service is reachable right now."""
    if not AVAILABLE:
        return False, "libsecret is not installed"
    try:
        Secret.Service.get_sync(Secret.ServiceFlags.NONE, None)
        return True, ""
    except GLib.Error as e:
        return False, e.message
