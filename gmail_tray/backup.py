"""Encrypted backup of all settings and passwords (e.g. to a USB stick).

The backup is a JSON document encrypted with GnuPG (AES-256, symmetric,
passphrase only — no keys needed). The passphrase is handed to gpg through
a private pipe, never on the command line, and gpg's passphrase cache is
disabled so nothing lingers in gpg-agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import __version__, fetchmail
from .config import Account, Config, ConfigError, atomic_write_private

FORMAT = "gmail-tray-backup"
FORMAT_VERSION = 1
SUFFIX = ".gmail-tray.gpg"
MIN_PASSPHRASE = 8

Key = tuple[str, str]


class BackupError(RuntimeError):
    pass


def default_filename() -> str:
    return f"gmail-tray-backup-{time.strftime('%Y-%m-%d')}{SUFFIX}"


def is_backup(path: Path) -> bool:
    """GnuPG output (binary or ASCII-armored) rather than a text fetchmailrc."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return False
    if head.startswith(b"-----BEGIN PGP MESSAGE"):
        return True
    return bool(head) and head[0] & 0x80 != 0


# ---------------------------------------------------------------- payload

def build_payload(cfg: Config, passwords: dict[Key, str]) -> bytes:
    cfg.validate()
    missing = [a.user for a in cfg.accounts if not passwords.get(a.key)]
    if missing:
        raise BackupError(f"No password known for: {', '.join(missing)}")
    doc = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "app_version": __version__,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": cfg.to_dict(),
        "passwords": [{"user": a.user, "server": a.server, "password": passwords[a.key]}
                      for a in cfg.accounts],
    }
    return json.dumps(doc, indent=2).encode("utf-8")


def parse_payload(data: bytes) -> tuple[Config, dict[Key, str]]:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BackupError("The file is not a gmail-tray backup") from e
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise BackupError("The file is not a gmail-tray backup")
    if doc.get("version", 0) > FORMAT_VERSION:
        raise BackupError("This backup was made by a newer gmail-tray — update first")
    try:
        cfg = Config.from_dict(doc.get("config", {}))
    except ConfigError as e:
        raise BackupError(f"Backup contains invalid settings: {e}") from e
    passwords: dict[Key, str] = {}
    for entry in doc.get("passwords", []):
        if not isinstance(entry, dict):
            continue
        acc = Account(str(entry.get("user", "")), str(entry.get("server", "")))
        pw = entry.get("password")
        if isinstance(pw, str) and pw:
            fetchmail.rc_quote(pw)  # rejects control characters
            passwords[acc.key] = pw
    return cfg, passwords


# ---------------------------------------------------------------- gpg

def gpg_binary() -> str | None:
    return shutil.which("gpg")


def _gpg(args: list[str], data: bytes, passphrase: str) -> bytes:
    exe = gpg_binary()
    if not exe:
        raise BackupError("GnuPG (gpg) is not installed")
    if "\n" in passphrase or "\r" in passphrase:
        raise BackupError("The passphrase must not contain line breaks")
    r, w = os.pipe()
    try:
        os.write(w, passphrase.encode("utf-8") + b"\n")
        os.close(w)
        w = -1
        proc = subprocess.run(
            [exe, "--batch", "--yes", "--no-tty", "--quiet", "--pinentry-mode", "loopback",
             "--no-symkey-cache", "--passphrase-fd", str(r), *args],
            input=data, capture_output=True, pass_fds=(r,), timeout=120)
    except subprocess.TimeoutExpired as e:
        raise BackupError("gpg timed out") from e
    finally:
        os.close(r)
        if w != -1:
            os.close(w)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace")
        if "Bad session key" in err or "decryption failed" in err.lower():
            raise BackupError("Wrong passphrase")
        raise BackupError("gpg failed: " + (err.strip().splitlines() or ["unknown error"])[-1])
    return proc.stdout


def export_to(path: Path, cfg: Config, passwords: dict[Key, str], passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters")
    blob = _gpg(["--symmetric", "--cipher-algo", "AES256", "--s2k-digest-algo", "SHA512",
                 "--s2k-mode", "3", "--s2k-count", "65011712", "--output", "-"],
                build_payload(cfg, passwords), passphrase)
    try:
        atomic_write_private(Path(path), blob)
    except OSError as e:
        raise BackupError(f"Cannot write {path}: {e.strerror or e}") from e


def import_from(path: Path, passphrase: str) -> tuple[Config, dict[Key, str]]:
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise BackupError(f"Cannot read {path}: {e.strerror or e}") from e
    return parse_payload(_gpg(["--decrypt"], data, passphrase))
