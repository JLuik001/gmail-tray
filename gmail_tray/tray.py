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
        self.badges = None
        try:
            from .badge import BadgeIcons
            self.badges = BadgeIcons(config.runtime_dir() / "icons")
            self.indicator.set_icon_theme_path(str(self.badges.dir))
        except Exception as e:  # noqa: BLE001  (pycairo missing etc.)
            log.warning("Unread badge disabled: %s", e)
        self.notifier = desktop.Notifier(self.cfg.title)
        self._build_menu()

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
        self.indicator.set_title(self.cfg.title)
        self._update_icon(sum(s.unread for s in self.statuses if s.ok),
                          any(not s.ok for s in self.statuses))
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

        self._update_icon(total, bool(errors))

        newly = [s for s in statuses if s.ok and s.unread > self.prev_unread.get(s.key, 0)]
        if newly and self.cfg.notifications:
            self._notify(newly)
        self.prev_unread = {s.key: s.unread for s in statuses if s.ok}
        for s in errors:
            log.warning("%s: %s", s.user, s.error)
        self._update_menu()
        return False

    def _update_icon(self, total: int, has_errors: bool) -> None:
        """Red badge with the unread count, amber "!" on errors, plain icon otherwise."""
        name = self.cfg.icon
        if self.badges and self.cfg.badge and (total or has_errors):
            from .badge import AMBER, RED
            try:
                name = (self.badges.name_for(self.cfg.icon, self.badges.label(total), RED) if total
                        else self.badges.name_for(self.cfg.icon, "!", AMBER))
            except Exception as e:  # noqa: BLE001
                log.warning("Cannot draw badge: %s", e)
                name = self.cfg.icon
        desc = f"{total} unread" if total else self.cfg.title
        self.indicator.set_icon_full(name, desc)
        self.indicator.set_attention_icon_full(name, desc)
        # Labels show where the tray supports them (GNOME); others rely on the badge.
        self.indicator.set_label(str(total) if total else "", "999")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ATTENTION if total
                                  else AppIndicator.IndicatorStatus.ACTIVE)

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
    #
    # The menu is built once and then only updated in place. KDE Plasma caches
    # the exported DBusMenu layout; replacing the whole menu after every check
    # made its clicks point at item ids that no longer existed, so nothing
    # happened when an entry was clicked.

    def _build_menu(self) -> None:
        self.menu = Gtk.Menu()
        self.account_items: dict[tuple, Gtk.MenuItem] = {}
        self.item_actions: dict[Gtk.MenuItem, object] = {}

        def add(label, action):
            item = Gtk.MenuItem(label=label)
            item.set_use_underline(False)
            item.connect("activate", lambda it: self._run_action(it))
            self.item_actions[item] = action
            self.menu.append(item)
            return item

        self.open_item = add(f"Open {self.cfg.title}", lambda: desktop.open_url(self.cfg))
        add("Check now", self.check_now)
        self.accounts_top = Gtk.SeparatorMenuItem()
        self.menu.append(self.accounts_top)
        self.accounts_end = Gtk.SeparatorMenuItem()
        self.menu.append(self.accounts_end)
        add("Settings…", self.open_settings)
        add("Quit", Gtk.main_quit)
        self.menu.show_all()
        self.accounts_top.hide()
        self.indicator.set_menu(self.menu)
        # Middle click on the tray icon opens the mail page directly.
        self.indicator.set_secondary_activate_target(self.open_item)

    def _run_action(self, item) -> None:
        action = self.item_actions.get(item)
        if action is None:
            return
        try:
            action()
        except Exception:  # noqa: BLE001  (a failing click must not kill the tray)
            log.exception("Menu action failed")

    def _update_menu(self) -> None:
        self.open_item.set_label(f"Open {self.cfg.title}")
        wanted = []
        for st in self.statuses:
            key = st.key if st.user != "fetchmail" else ("fetchmail", st.error or "")
            if st.ok:
                label = f"{st.user}: {st.unread} unread"
                action = (lambda s=st: desktop.open_url(
                    self.cfg, desktop.account_url(self.cfg, self._account_for(s))))
            else:
                label = f"⚠ {st.user}: {st.error}"
                action = self.open_settings
            wanted.append((key, label, action))

        keys = {k for k, _, _ in wanted}
        for key in list(self.account_items):
            if key not in keys:
                item = self.account_items.pop(key)
                self.item_actions.pop(item, None)
                item.destroy()

        pos = self.menu.get_children().index(self.accounts_top) + 1
        for i, (key, label, action) in enumerate(wanted):
            item = self.account_items.get(key)
            if item is None:
                item = Gtk.MenuItem(label=label)
                item.set_use_underline(False)
                item.connect("activate", lambda it: self._run_action(it))
                self.menu.insert(item, pos + i)
                item.show()
                self.account_items[key] = item
            elif item.get_label() != label:
                item.set_label(label)
            self.item_actions[item] = action
        self.accounts_top.set_visible(bool(wanted))

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
