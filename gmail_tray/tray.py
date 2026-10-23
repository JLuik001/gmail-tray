"""The tray indicator. Mail checks run in a worker thread so the UI never freezes."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, config, desktop, fetchmail  # noqa: E402
from .fetchmail import AccountStatus  # noqa: E402

log = logging.getLogger(__name__)


class TrayApp:
    def __init__(self, cfg_path: Path):
        self.cfg_path = Path(cfg_path)
        self.cfg = self._load_or_default()
        self.statuses: list[AccountStatus] = []
        self.prev_unread: dict[tuple, int] = {}
        self.checking = False
        self.timer_id = 0
        self.reload_id = 0
        self.settings_window = None

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, self.cfg.icon, AppIndicator.IndicatorCategory.COMMUNICATIONS)
        self.indicator.set_title(self.cfg.title)
        self.indicator.set_attention_icon_full(self.cfg.icon, "Unread mail")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.notifier = desktop.Notifier(self.cfg.title)
        self._rebuild_menu()

        # Reload automatically when the settings window (or an editor) saves the config.
        self.monitor = Gio.File.new_for_path(str(self.cfg_path.parent)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.monitor.connect("changed", self._on_config_dir_changed)

        self._schedule()
        self.check_now()
        if not self.cfg.managed and not self.cfg.fetchmailrc_path.exists():
            GLib.idle_add(self.open_settings)

    # ------------------------------------------------------------ config

    def _load_or_default(self) -> config.Config:
        try:
            return config.load(self.cfg_path)
        except config.ConfigError as e:
            log.error("Configuration error: %s", e)
            self._config_error = str(e)
            return config.Config()

    def _on_config_dir_changed(self, _mon, file, other, event):
        names = {f.get_basename() for f in (file, other) if f is not None}
        if self.cfg_path.name not in names:
            return
        if self.reload_id:
            GLib.source_remove(self.reload_id)
        self.reload_id = GLib.timeout_add(500, self.reload)

    def reload(self) -> bool:
        self.reload_id = 0
        try:
            self.cfg = config.load(self.cfg_path)
        except config.ConfigError as e:
            log.error("Not reloading, configuration invalid: %s", e)
            return False
        log.info("Configuration reloaded")
        self.indicator.set_icon_full(self.cfg.icon, self.cfg.title)
        self.indicator.set_attention_icon_full(self.cfg.icon, "Unread mail")
        self.indicator.set_title(self.cfg.title)
        self.prev_unread = {}
        self._schedule()
        self.check_now()
        return False

    def _schedule(self) -> None:
        if self.timer_id:
            GLib.source_remove(self.timer_id)
        self.timer_id = GLib.timeout_add_seconds(self.cfg.interval, self._on_timer)

    def _on_timer(self) -> bool:
        self.check_now()
        return True

    # ------------------------------------------------------------ checking

    def check_now(self, *_):
        if self.checking:
            return
        self.checking = True
        cfg = self.cfg
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _worker(self, cfg: config.Config) -> None:
        statuses = fetchmail.check_all(cfg)
        GLib.idle_add(self._on_result, statuses)

    def _on_result(self, statuses: list[AccountStatus]) -> bool:
        self.checking = False
        self.statuses = statuses
        total = sum(s.unread for s in statuses if s.ok)
        errors = [s for s in statuses if not s.ok]

        self.indicator.set_label(str(total) if total else ("!" if errors and not total else ""),
                                 "999")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ATTENTION if total
                                  else AppIndicator.IndicatorStatus.ACTIVE)

        newly = [s for s in statuses if s.ok and s.unread > self.prev_unread.get(s.key, 0)]
        if newly and self.cfg.notifications:
            self._notify(newly)
        self.prev_unread = {s.key: s.unread for s in statuses if s.ok}
        for s in errors:
            log.warning("%s: %s", s.user, s.error)
        self._rebuild_menu()
        return False

    def _account_for(self, status: AccountStatus):
        return next((a for a in self.cfg.accounts if a.key == status.key), None)

    def _notify(self, newly: list[AccountStatus]) -> None:
        total = sum(s.unread for s in self.statuses if s.ok)
        summary = f"{total} unread message{'s' if total != 1 else ''}"
        body = "\n".join(f"{s.user}: {s.unread}" for s in newly)
        target = desktop.account_url(self.cfg, self._account_for(newly[0])) if len(newly) == 1 else None
        self.notifier.show(summary, body, self.cfg.icon, self.cfg.notification_timeout,
                           on_open=lambda: desktop.open_url(self.cfg, target))

    # ------------------------------------------------------------ menu

    def _item(self, menu, label, callback=None, sensitive=True):
        item = Gtk.MenuItem(label=label)
        item.set_use_underline(False)
        if callback:
            item.connect("activate", callback)
        item.set_sensitive(sensitive)
        menu.append(item)
        return item

    def _rebuild_menu(self) -> None:
        menu = Gtk.Menu()
        self._item(menu, f"Open {self.cfg.title}", lambda _: desktop.open_url(self.cfg))
        self._item(menu, "Check now", self.check_now)
        if self.statuses:
            menu.append(Gtk.SeparatorMenuItem())
            for s in self.statuses:
                if s.ok:
                    acc = self._account_for(s)
                    url = desktop.account_url(self.cfg, acc)
                    self._item(menu, f"{s.user}: {s.unread} unread",
                               lambda _, u=url: desktop.open_url(self.cfg, u))
                else:
                    self._item(menu, f"⚠ {s.user}: {s.error}", self.open_settings)
        menu.append(Gtk.SeparatorMenuItem())
        self._item(menu, "Settings…", self.open_settings)
        self._item(menu, "Quit", lambda _: Gtk.main_quit())
        menu.show_all()
        self.indicator.set_menu(menu)

    # ------------------------------------------------------------ settings

    def open_settings(self, *_):
        from .settings_gui import SettingsWindow
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self.cfg_path)
            self.settings_window.connect("destroy", self._on_settings_closed)
        self.settings_window.present()
        return False

    def _on_settings_closed(self, *_):
        self.settings_window = None
