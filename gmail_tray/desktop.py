"""Opening the mail page and showing notifications — no shell involved anywhere."""

from __future__ import annotations

import logging
import shutil
import subprocess
from urllib.parse import quote

import gi
from gi.repository import GLib

from .config import Account, Config

log = logging.getLogger(__name__)

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify
    HAVE_NOTIFY = True
except (ImportError, ValueError):
    HAVE_NOTIFY = False

KNOWN_BROWSERS = ["firefox", "librewolf", "chromium", "google-chrome-stable", "brave",
                  "vivaldi-stable", "microsoft-edge-stable", "epiphany", "falkon", "qutebrowser"]


def installed_browsers() -> list[str]:
    return [b for b in KNOWN_BROWSERS if shutil.which(b)]


def account_url(cfg: Config, acc: Account | None) -> str:
    """Gmail can open a specific signed-in account directly."""
    if acc is not None and acc.is_gmail and cfg.url.rstrip("/") in (
            "https://mail.google.com", "https://mail.google.com/mail"):
        return f"https://mail.google.com/mail/u/?authuser={quote(acc.user, safe='@')}"
    return cfg.url


def _spawn(argv: list[str]) -> None:
    # GLib.spawn_async reaps the child itself (no zombies, no blocking wait).
    GLib.spawn_async(argv, flags=GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_DEV_NULL
                     | GLib.SpawnFlags.STDERR_TO_DEV_NULL)


def open_url(cfg: Config, url: str | None = None) -> None:
    url = url or cfg.url
    if cfg.browser:
        exe = shutil.which(cfg.browser)
        if exe:
            try:
                _spawn([exe, *cfg.flags, url])
                return
            except GLib.Error as e:
                log.warning("Cannot start %s: %s", cfg.browser, e.message)
        else:
            log.warning("Browser %r not found, using the system default", cfg.browser)
    try:
        from gi.repository import Gio
        Gio.AppInfo.launch_default_for_uri(url, None)
    except Exception as e:  # noqa: BLE001
        log.warning("Default browser failed (%s), trying xdg-open", e)
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)


class Notifier:
    def __init__(self, app_name: str):
        self.app_name = app_name
        self._live = []  # keep references so action callbacks stay alive
        self.ready = HAVE_NOTIFY and Notify.init(app_name)
        self.actions = self.ready and "actions" in (Notify.get_server_caps() or [])

    def show(self, summary: str, body: str, icon: str, timeout_s: int, on_open=None) -> None:
        # Notification servers interpret markup in the body: escape untrusted text.
        body = GLib.markup_escape_text(body)
        if not self.ready:
            if shutil.which("notify-send"):
                args = ["notify-send", "-a", self.app_name, "-i", icon]
                if timeout_s:
                    args += ["-t", str(timeout_s * 1000)]
                subprocess.Popen(args + ["--", summary, body], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            return
        n = Notify.Notification.new(summary, body, icon)
        n.set_timeout(timeout_s * 1000 if timeout_s else Notify.EXPIRES_NEVER)
        n.set_hint("desktop-entry", GLib.Variant("s", "gmail-tray"))
        if on_open and self.actions:
            n.add_action("default", "Open", lambda *_: on_open())
            n.add_action("open", "Open", lambda *_: on_open())
        n.connect("closed", lambda notif: self._live.remove(notif) if notif in self._live else None)
        self._live.append(n)
        try:
            n.show()
        except GLib.Error as e:
            log.warning("Notification failed: %s", e.message)
            self._live.remove(n)
