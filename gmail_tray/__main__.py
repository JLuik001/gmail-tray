"""Command line entry point."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
from pathlib import Path

from . import APP_ID, __version__, config


def _acquire_lock():
    """Single-instance lock in the private runtime dir (not world-writable /tmp)."""
    path = config.runtime_dir() / f"{APP_ID}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def waybar_once(cfg_path) -> int:
    from . import fetchmail
    try:
        cfg = config.load(cfg_path)
    except config.ConfigError as e:
        print(json.dumps({"text": "!", "tooltip": str(e), "class": "error"}))
        return 0
    statuses = fetchmail.check_all(cfg)
    total = sum(s.unread for s in statuses if s.ok)
    errors = [s for s in statuses if not s.ok]
    lines = [f"{s.user}: {s.unread} unread" if s.ok else f"{s.user}: {s.error}" for s in statuses]
    print(json.dumps({
        "text": str(total),
        "alt": "unread" if total else "none",
        "tooltip": "\n".join(lines) or "No accounts configured",
        "class": "error" if errors else ("unread" if total else "none"),
        "percentage": 0,
    }))
    return 0


def print_check(cfg_path) -> int:
    from . import fetchmail
    cfg = config.load(cfg_path)
    statuses = fetchmail.check_all(cfg)
    for s in statuses:
        print(f"{s.user}: {s.unread} unread / {s.total}" if s.ok else f"{s.user}: ERROR {s.error}")
    return 0 if all(s.ok for s in statuses) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog=APP_ID, description="Minimal Gmail tray notifier (fetchmail based)")
    p.add_argument("--settings", action="store_true", help="open the settings window")
    p.add_argument("--once", action="store_true", help="print unread count as Waybar JSON and exit")
    p.add_argument("--check", action="store_true", help="check all accounts, print results and exit")
    p.add_argument("--config", metavar="FILE", help="use another configuration file")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    cfg_path = Path(args.config).expanduser() if args.config else config.config_path()

    try:
        if args.once:
            return waybar_once(cfg_path)
        if args.check:
            return print_check(cfg_path)
        if args.settings:
            from .settings_gui import run_standalone
            return run_standalone(cfg_path)

        lock = _acquire_lock()
        if lock is None:
            # Already running: behave like a launcher and open the mail page.
            from . import desktop
            try:
                desktop.open_url(config.load(cfg_path))
            except config.ConfigError as e:
                logging.error("%s", e)
                return 1
            return 0

        from .tray import TrayApp  # pins GTK 3 before Gtk is imported
        from gi.repository import Gtk
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        app = TrayApp(cfg_path)
        try:
            Gtk.main()
        finally:
            del app
        return 0
    except config.ConfigError as e:
        logging.error("Configuration error: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
