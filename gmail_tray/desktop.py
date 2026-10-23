"""Opening the mail page and showing notifications — no shell involved anywhere."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
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


def installed_browsers() -> list[tuple[str, str]]:
    """(desktop-file id, name) of every installed web browser, Flatpaks included."""
    from gi.repository import Gio
    seen, out = set(), []
    for app in Gio.AppInfo.get_all():   # does not depend on an up-to-date mimeinfo.cache
        app_id = app.get_id()
        if not app_id or app_id in seen or not app.should_show():
            continue
        categories = (app.get_categories() or "") if isinstance(app, Gio.DesktopAppInfo) else ""
        types = app.get_supported_types() or []
        if "WebBrowser" not in categories.split(";") and "x-scheme-handler/https" not in types:
            continue
        seen.add(app_id)
        out.append((app_id, app.get_display_name() or app_id))
    return sorted(out, key=lambda x: x[1].lower())


def browser_name(browser: str) -> str | None:
    """Display name for a desktop-file id, None if it is not installed."""
    from gi.repository import Gio
    try:
        info = Gio.DesktopAppInfo.new(browser)
    except TypeError:
        info = None
    return info.get_display_name() if info else None


def account_url(cfg: Config, acc: Account | None) -> str:
    """Gmail can open a specific signed-in account directly."""
    if acc is not None and acc.is_gmail and cfg.url.rstrip("/") in (
            "https://mail.google.com", "https://mail.google.com/mail"):
        return f"https://mail.google.com/mail/u/?authuser={quote(acc.user, safe='@')}"
    return cfg.url


def _popen(argv: list[str]) -> None:
    """Start a detached program; a thread reaps it so no zombie is left behind."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    threading.Thread(target=proc.wait, daemon=True).start()


def _open_with_browser(cfg: Config, url: str) -> bool:
    """Open *url* with the configured browser. Returns False to fall back to the default."""
    browser = cfg.browser
    if not browser:
        return False
    if browser.endswith(".desktop"):
        from gi.repository import Gio
        try:
            info = Gio.DesktopAppInfo.new(browser)
        except TypeError:
            info = None
        if info is None:
            log.warning("Browser %s is not installed, using the system default", browser)
            return False
        exe = info.get_executable()
        exe_path = shutil.which(exe) if exe else None
        # Extra options need a command line; Flatpak wrappers cannot take them.
        if cfg.flags and exe_path and os.path.basename(exe_path) not in ("flatpak", "env", "snap"):
            _popen([exe_path, *cfg.flags, url])
        else:
            info.launch_uris([url], None)
        return True
    exe = shutil.which(os.path.expanduser(browser))
    if not exe:
        log.warning("Browser %r not found, using the system default", browser)
        return False
    _popen([exe, *cfg.flags, url])
    return True


def open_url(cfg: Config, url: str | None = None) -> None:
    url = url or cfg.url
    try:
        if _open_with_browser(cfg, url):
            return
    except Exception as e:  # noqa: BLE001
        log.warning("Cannot start browser %s (%s), using the system default", cfg.browser, e)
    try:
        from gi.repository import Gio
        Gio.AppInfo.launch_default_for_uri(url, None)
    except Exception as e:  # noqa: BLE001
        log.warning("Default browser failed (%s), trying xdg-open", e)
        if shutil.which("xdg-open"):
            _popen(["xdg-open", url])


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
