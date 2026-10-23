"""GTK settings window: fetchmail accounts and app customizations."""

from __future__ import annotations

import copy
import logging
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, account_store, config, desktop, fetchmail  # noqa: E402
from .config import Account, Config, ConfigError  # noqa: E402

log = logging.getLogger(__name__)

APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"
SYSTEM_DEFAULT_LABEL = "System default browser"

# name, server, protocol, port, implicit TLS
PROVIDERS = [
    ("Gmail", "imap.gmail.com", "IMAP", 993, True),
    ("Yahoo Mail", "imap.mail.yahoo.com", "IMAP", 993, True),
    ("iCloud Mail", "imap.mail.me.com", "IMAP", 993, True),
    ("Fastmail", "imap.fastmail.com", "IMAP", 993, True),
    ("Other…", "", "IMAP", 993, True),
]


def _label(text: str, xalign: float = 0.0, markup: bool = False, wrap: bool = False) -> Gtk.Label:
    lbl = Gtk.Label()
    if markup:
        lbl.set_markup(text)
    else:
        lbl.set_text(text)
    lbl.set_xalign(xalign)
    lbl.set_line_wrap(wrap)
    return lbl


def _run_async(work, done):
    """Run *work()* in a thread, then *done(result_or_exception)* on the GTK thread."""
    def target():
        try:
            res = work()
        except Exception as e:  # noqa: BLE001
            res = e
        GLib.idle_add(lambda: (done(res), False)[1])
    threading.Thread(target=target, daemon=True).start()


def _describe(status: fetchmail.AccountStatus) -> str:
    if status.ok:
        return f"OK — {status.unread} unread of {status.total}"
    return f"⚠ {status.error}"


# ====================================================================== account dialog

class AccountDialog(Gtk.Dialog):
    def __init__(self, parent, account: Account | None, has_password: bool, password_for_test):
        super().__init__(title="Edit account" if account else "Add account",
                         transient_for=parent, modal=True, use_header_bar=False)
        self.set_default_size(460, -1)
        self.password_for_test = password_for_test
        self.original = account
        self.has_password = has_password
        self.result_account: Account | None = None
        self.result_password = ""

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = self.add_button("Save", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=16)
        self.get_content_area().add(grid)
        row = 0

        def add_row(text, widget, tip=None):
            nonlocal row
            lbl = _label(text, 1.0)
            grid.attach(lbl, 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
            if tip:
                widget.set_tooltip_text(tip)
            row += 1
            return widget

        self.provider = Gtk.ComboBoxText()
        for p in PROVIDERS:
            self.provider.append_text(p[0])
        add_row("Provider", self.provider)

        self.user = add_row("Email address", Gtk.Entry(activates_default=True))
        self.password = Gtk.Entry(visibility=False, activates_default=True,
                                  input_purpose=Gtk.InputPurpose.PASSWORD)
        self.password.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic")
        self.password.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Show / hide")
        self.password.connect("icon-press", lambda e, *_: e.set_visibility(not e.get_visibility()))
        if has_password:
            self.password.set_placeholder_text("Leave empty to keep the stored password")
        add_row("App password", self.password,
                "Gmail needs an app password (Google account → Security → App passwords).")
        link = Gtk.LinkButton.new_with_label(APP_PASSWORD_URL, "Create a Gmail app password")
        link.set_halign(Gtk.Align.START)
        grid.attach(link, 1, row, 1, 1)
        row += 1

        self.expander = Gtk.Expander(label="Server settings")
        grid.attach(self.expander, 0, row, 2, 1)
        row += 1
        sgrid = Gtk.Grid(column_spacing=12, row_spacing=8, margin_top=8)
        self.expander.add(sgrid)
        srow = 0

        def add_srow(text, widget):
            nonlocal srow
            sgrid.attach(_label(text, 1.0), 0, srow, 1, 1)
            widget.set_hexpand(True)
            sgrid.attach(widget, 1, srow, 1, 1)
            srow += 1
            return widget

        self.server = add_srow("Server", Gtk.Entry())
        self.protocol = Gtk.ComboBoxText()
        for p in config.PROTOCOLS:
            self.protocol.append(p, p)
        add_srow("Protocol", self.protocol)
        self.encryption = Gtk.ComboBoxText()
        self.encryption.append("ssl", "SSL/TLS (recommended)")
        self.encryption.append("starttls", "STARTTLS")
        add_srow("Encryption", self.encryption)
        self.port = add_srow("Port", Gtk.SpinButton.new_with_range(0, 65535, 1))
        self.port.set_tooltip_text("0 = default port for the protocol")
        self.folder = add_srow("Folder", Gtk.Entry(placeholder_text="INBOX"))

        test_box = Gtk.Box(spacing=8)
        self.test_button = Gtk.Button(label="Test connection")
        self.test_button.connect("clicked", self.on_test)
        self.spinner = Gtk.Spinner()
        self.test_result = _label("", wrap=True)
        self.test_result.set_selectable(True)
        test_box.pack_start(self.test_button, False, False, 0)
        test_box.pack_start(self.spinner, False, False, 0)
        test_box.pack_start(self.test_result, True, True, 0)
        grid.attach(test_box, 0, row, 2, 1)
        row += 1

        self.error = _label("", wrap=True)
        self.error.get_style_context().add_class("error")
        grid.attach(self.error, 0, row, 2, 1)

        self.protocol.connect("changed", self._on_protocol_changed)
        self.provider.connect("changed", self._on_provider_changed)

        acc = account or Account("")
        self.user.set_text(acc.user)
        self.server.set_text(acc.server)
        self.protocol.set_active_id(acc.protocol)
        self.encryption.set_active_id("ssl" if acc.ssl else "starttls")
        self.port.set_value(acc.port)
        self.folder.set_text(acc.folder)
        idx = next((i for i, p in enumerate(PROVIDERS) if p[1] == acc.server), len(PROVIDERS) - 1)
        self._loading = True
        self.provider.set_active(idx)
        self._loading = False
        self.expander.set_expanded(idx == len(PROVIDERS) - 1)

        self.connect("response", self.on_response)
        self.show_all()

    def _on_protocol_changed(self, _):
        self.folder.set_sensitive(self.protocol.get_active_id() == "IMAP")

    def _on_provider_changed(self, combo):
        if getattr(self, "_loading", False):
            return
        name, server, proto, port, ssl = PROVIDERS[combo.get_active()]
        if server:
            self.server.set_text(server)
            self.protocol.set_active_id(proto)
            self.encryption.set_active_id("ssl" if ssl else "starttls")
            self.port.set_value(0)
        else:
            self.expander.set_expanded(True)
            self.server.grab_focus()

    def _build(self) -> Account:
        return Account(
            user=self.user.get_text().strip(),
            server=self.server.get_text().strip(),
            protocol=self.protocol.get_active_id() or "IMAP",
            port=int(self.port.get_value()),
            ssl=self.encryption.get_active_id() == "ssl",
            folder=self.folder.get_text().strip() if self.protocol.get_active_id() == "IMAP" else "",
        ).validate()

    def on_test(self, _):
        try:
            acc = self._build()
        except ConfigError as e:
            self.test_result.set_text(f"⚠ {e}")
            return
        typed = self.password.get_text()
        original = self.original

        def work():
            pw = typed or (self.password_for_test(original) if original else None)
            if not pw:
                raise ConfigError("Enter the app password first")
            return fetchmail.check_account(acc, pw)

        def done(res):
            self.spinner.stop()
            self.test_button.set_sensitive(True)
            self.test_result.set_text(f"⚠ {res}" if isinstance(res, Exception) else _describe(res))

        self.test_button.set_sensitive(False)
        self.spinner.start()
        self.test_result.set_text("Connecting…")
        _run_async(work, done)

    def on_response(self, _dlg, response):
        if response != Gtk.ResponseType.OK:
            return
        try:
            acc = self._build()
            pw = self.password.get_text()
            if not pw and not (self.has_password and self.original and self.original.key == acc.key):
                raise ConfigError("Enter the app password")
            if pw:
                fetchmail.rc_quote(pw)  # rejects control characters
        except ConfigError as e:
            self.error.set_text(str(e))
            self.stop_emission_by_name("response")
            return
        self.result_account, self.result_password = acc, pw


# ====================================================================== main window

class SettingsWindow(Gtk.Window):
    def __init__(self, cfg_path: Path | None = None):
        super().__init__(title=f"{APP_NAME} — Settings")
        self.set_default_size(680, 540)
        self.set_icon_name(APP_ID)
        self.cfg_path = Path(cfg_path) if cfg_path else config.config_path()

        load_error = None
        try:
            self.orig = config.load(self.cfg_path)
        except ConfigError as e:
            load_error = f"Your configuration had errors and defaults were loaded: {e}"
            self.orig = Config()
        self.accounts: list[Account] = copy.deepcopy(self.orig.accounts)
        self.passwords: dict[tuple, str] = {}
        self.status_text: dict[tuple, str] = {}
        self.keyring_ok, self.keyring_reason = self._keyring_state()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        self.infobar = Gtk.InfoBar(show_close_button=True, revealed=False)
        self.info_label = _label("", wrap=True)
        self.infobar.get_content_area().add(self.info_label)
        self.infobar.connect("response", lambda bar, _r: bar.set_revealed(False))
        outer.pack_start(self.infobar, False, False, 0)

        notebook = Gtk.Notebook()
        outer.pack_start(notebook, True, True, 0)
        notebook.append_page(self._accounts_page(), Gtk.Label(label="Accounts"))
        notebook.append_page(self._general_page(), Gtk.Label(label="General"))
        notebook.append_page(self._notifications_page(), Gtk.Label(label="Notifications & startup"))

        buttons = Gtk.Box(spacing=8, margin=12)
        buttons.pack_start(_label(f"Config: {self.cfg_path}", markup=False), True, True, 0)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _: self.destroy())
        save = Gtk.Button(label="Save")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self.on_save)
        buttons.pack_end(save, False, False, 0)
        buttons.pack_end(cancel, False, False, 0)
        outer.pack_end(buttons, False, False, 0)

        self._refresh_accounts()
        self.show_all()
        if load_error:
            self.show_info(load_error, Gtk.MessageType.ERROR)
        elif not fetchmail.fetchmail_binary():
            self.show_info("fetchmail is not installed — install it with: sudo pacman -S fetchmail",
                           Gtk.MessageType.ERROR)

    @staticmethod
    def _keyring_state():
        from . import secret_store
        return secret_store.usable()

    def show_info(self, text: str, kind=Gtk.MessageType.INFO):
        self.info_label.set_text(text)
        self.infobar.set_message_type(kind)
        self.infobar.set_revealed(True)

    # ---------------------------------------------------------------- accounts page

    def _accounts_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=16)
        box.pack_start(_label("<b>Mail accounts</b>", markup=True), False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        for i, title in enumerate(("Email", "Server", "Protocol", "Status")):
            cell = Gtk.CellRendererText()
            if i == 3:
                cell.set_property("ellipsize", 3)  # Pango.EllipsizeMode.END
            col = Gtk.TreeViewColumn(title, cell, text=i)
            col.set_resizable(True)
            col.set_expand(i in (0, 3))
            self.view.append_column(col)
        self.view.connect("row-activated", lambda *_: self.on_edit(None))
        self.view.get_selection().connect("changed", lambda *_: self._update_buttons())
        scroll = Gtk.ScrolledWindow(min_content_height=150)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(self.view)
        box.pack_start(scroll, True, True, 0)

        row = Gtk.Box(spacing=6)
        self.btn_add = Gtk.Button(label="Add…")
        self.btn_edit = Gtk.Button(label="Edit…")
        self.btn_remove = Gtk.Button(label="Remove")
        self.btn_test = Gtk.Button(label="Test all")
        self.btn_import = Gtk.Button(label="Import from .fetchmailrc…")
        self.btn_add.connect("clicked", self.on_add)
        self.btn_edit.connect("clicked", self.on_edit)
        self.btn_remove.connect("clicked", self.on_remove)
        self.btn_test.connect("clicked", self.on_test_all)
        self.btn_import.connect("clicked", self.on_import)
        for b in (self.btn_add, self.btn_edit, self.btn_remove, self.btn_test):
            row.pack_start(b, False, False, 0)
        row.pack_end(self.btn_import, False, False, 0)
        box.pack_start(row, False, False, 0)

        self.legacy_label = _label("", wrap=True)
        box.pack_start(self.legacy_label, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 4)
        box.pack_start(_label("<b>Where to keep passwords</b>", markup=True), False, False, 0)
        self.rb_keyring = Gtk.RadioButton.new_with_label(
            None, "Desktop keyring (recommended — encrypted, never written to disk in plain text)")
        self.rb_file = Gtk.RadioButton.new_with_label_from_widget(
            self.rb_keyring, f"Private file {config.managed_rc_path()} (plain text, mode 0600)")
        box.pack_start(self.rb_keyring, False, False, 0)
        box.pack_start(self.rb_file, False, False, 0)
        if not self.keyring_ok:
            self.rb_keyring.set_sensitive(False)
            self.rb_keyring.set_tooltip_text(f"Keyring unavailable: {self.keyring_reason}")
            self.rb_file.set_active(True)
        else:
            (self.rb_keyring if self.orig.password_store == "keyring" else self.rb_file).set_active(True)

        link = Gtk.LinkButton.new_with_label(APP_PASSWORD_URL, "Create a Gmail app password")
        link.set_halign(Gtk.Align.START)
        box.pack_start(link, False, False, 0)
        return box

    def _refresh_accounts(self):
        self.store.clear()
        for acc in self.accounts:
            self.store.append([acc.user, acc.server, acc.protocol, self.status_text.get(acc.key, "")])
        legacy = self.orig.fetchmailrc_path
        if not self.accounts and legacy.exists():
            self.legacy_label.set_text(
                f"No accounts are managed here yet, so {legacy} is used as-is. "
                "Use “Import” to manage those accounts here instead.")
        elif not self.accounts:
            self.legacy_label.set_text("Add your first account to start checking mail.")
        else:
            self.legacy_label.set_text("")
        self._update_buttons()

    def _selected(self) -> int | None:
        model, it = self.view.get_selection().get_selected()
        return model.get_path(it).get_indices()[0] if it else None

    def _update_buttons(self):
        sel = self._selected() is not None
        self.btn_edit.set_sensitive(sel)
        self.btn_remove.set_sensitive(sel)
        self.btn_test.set_sensitive(bool(self.accounts) or self.orig.fetchmailrc_path.exists())

    def _stored_password(self, acc: Account) -> str | None:
        """Password known for *acc*: typed this session, or already saved."""
        if acc.key in self.passwords:
            return self.passwords[acc.key]
        if any(a.key == acc.key for a in self.orig.accounts):
            try:
                return account_store.current_password(self.orig, acc)
            except Exception as e:  # noqa: BLE001
                log.warning("Password lookup failed: %s", e)
        return None

    def _has_password(self, acc: Account) -> bool:
        return acc.key in self.passwords or any(a.key == acc.key for a in self.orig.accounts)

    def _run_dialog(self, acc: Account | None):
        dlg = AccountDialog(self, acc, acc is not None and self._has_password(acc), self._stored_password)
        resp = dlg.run()
        result = (dlg.result_account, dlg.result_password) if resp == Gtk.ResponseType.OK else None
        dlg.destroy()
        return result

    def on_add(self, _):
        res = self._run_dialog(None)
        if not res:
            return
        acc, pw = res
        if any(a.key == acc.key for a in self.accounts):
            self.show_info(f"{acc.user} on {acc.server} is already in the list.", Gtk.MessageType.WARNING)
            return
        self.accounts.append(acc)
        self.passwords[acc.key] = pw
        self._refresh_accounts()

    def on_edit(self, _):
        idx = self._selected()
        if idx is None:
            return
        old = self.accounts[idx]
        res = self._run_dialog(old)
        if not res:
            return
        acc, pw = res
        if acc.key != old.key and any(a.key == acc.key for a in self.accounts):
            self.show_info(f"{acc.user} on {acc.server} is already in the list.", Gtk.MessageType.WARNING)
            return
        if pw:
            self.passwords.pop(old.key, None)
            self.passwords[acc.key] = pw
        elif acc.key != old.key:
            # Renamed without a new password: carry the old one over.
            known = self._stored_password(old)
            if known:
                self.passwords[acc.key] = known
        self.accounts[idx] = acc
        self.status_text.pop(old.key, None)
        self._refresh_accounts()

    def on_remove(self, _):
        idx = self._selected()
        if idx is None:
            return
        acc = self.accounts.pop(idx)
        self.passwords.pop(acc.key, None)
        self._refresh_accounts()

    def on_test_all(self, _):
        self.btn_test.set_sensitive(False)
        if not self.accounts:
            path = self.orig.fetchmailrc_path

            def done(res):
                self.btn_test.set_sensitive(True)
                if isinstance(res, Exception):
                    self.show_info(str(res), Gtk.MessageType.ERROR)
                else:
                    text = "\n".join(f"{s.user}: {_describe(s)}" for s in res) or "No accounts found."
                    self.show_info(text, Gtk.MessageType.INFO if all(s.ok for s in res)
                                   else Gtk.MessageType.WARNING)
            _run_async(lambda: fetchmail.check_rcfile(path), done)
            return

        accounts = list(self.accounts)
        for acc in accounts:
            self.status_text[acc.key] = "Checking…"
        self._refresh_accounts()

        def show(key, st):
            self.status_text[key] = _describe(st)
            self._refresh_accounts()
            return False

        def work():
            # Accounts are checked in parallel; each row updates as soon as it is done.
            pws = {a.key: self._stored_password(a) for a in accounts}  # keyring: one at a time

            def one(acc):
                pw = pws[acc.key]
                st = (fetchmail.check_account(acc, pw) if pw else
                      fetchmail.AccountStatus(acc.user, acc.server, error="No password"))
                GLib.idle_add(show, acc.key, st)
            threads = [threading.Thread(target=one, args=(a,), daemon=True) for a in accounts]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        def done(res):
            self.btn_test.set_sensitive(True)
            if isinstance(res, Exception):
                self.show_info(str(res), Gtk.MessageType.ERROR)
        _run_async(work, done)

    def on_import(self, _):
        chooser = Gtk.FileChooserNative.new("Import fetchmail configuration", self,
                                            Gtk.FileChooserAction.OPEN, "Import", "Cancel")
        chooser.set_show_hidden(True)
        legacy = self.orig.fetchmailrc_path
        if legacy.exists():
            chooser.set_filename(str(legacy))
        resp = chooser.run()
        path = chooser.get_filename()
        chooser.destroy()
        if resp != Gtk.ResponseType.ACCEPT or not path:
            return
        try:
            entries = fetchmail.parse_rc(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            self.show_info(f"Cannot read {path}: {e}", Gtk.MessageType.ERROR)
            return
        added = 0
        for acc, pw in entries:
            if any(a.key == acc.key for a in self.accounts):
                continue
            self.accounts.append(acc)
            if pw:
                self.passwords[acc.key] = pw
            added += 1
        self._refresh_accounts()
        missing = [a.user for a, pw in entries if not pw]
        msg = f"Imported {added} account(s) from {path}. The file itself was not changed."
        if missing:
            msg += f" No password found for: {', '.join(missing)} — edit them to add one."
        self.show_info(msg + " Click Save to apply.")

    # ---------------------------------------------------------------- general page

    def _grid_page(self):
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=16)
        grid.row = 0

        def add(text, widget, tip=None, expand=True):
            grid.attach(_label(text, 1.0), 0, grid.row, 1, 1)
            widget.set_hexpand(expand)
            if not expand:
                widget.set_halign(Gtk.Align.START)
            grid.attach(widget, 1, grid.row, 1, 1)
            if tip:
                widget.set_tooltip_text(tip)
            grid.row += 1
            return widget
        return grid, add

    def _general_page(self) -> Gtk.Widget:
        grid, add = self._grid_page()
        c = self.orig
        self.e_title = add("Title", Gtk.Entry(text=c.title))
        self.e_url = add("Mail page", Gtk.Entry(text=c.url),
                         "Opened from the tray. Must be https://. Gmail accounts open their own inbox.")

        self.e_browser = Gtk.ComboBoxText.new_with_entry()
        self.e_browser.append_text(SYSTEM_DEFAULT_LABEL)
        for b in desktop.installed_browsers():
            self.e_browser.append_text(b)
        self.e_browser.get_child().set_text(c.browser or SYSTEM_DEFAULT_LABEL)
        add("Browser", self.e_browser, "Choose from the list or type a command name or path.")

        self.e_flags = add("Browser options", Gtk.Entry(text=shlex.join(c.flags)),
                           "Extra arguments, e.g. --new-tab or -P work (Firefox profile).")
        self.e_interval = add("Check every (seconds)",
                              Gtk.SpinButton.new_with_range(config.MIN_INTERVAL, config.MAX_INTERVAL, 10),
                              expand=False)
        self.e_interval.set_value(c.interval)

        icon_box = Gtk.Box(spacing=6)
        self.e_icon = Gtk.Entry(text=c.icon)
        self.icon_preview = Gtk.Image()
        self.e_icon.connect("changed", lambda *_: self._update_icon_preview())
        pick = Gtk.Button(label="Choose…")
        pick.connect("clicked", self.on_pick_icon)
        icon_box.pack_start(self.icon_preview, False, False, 0)
        icon_box.pack_start(self.e_icon, True, True, 0)
        icon_box.pack_start(pick, False, False, 0)
        add("Tray icon", icon_box, "Icon theme name or path to an image file.")
        self._update_icon_preview()
        return grid

    def _update_icon_preview(self):
        name = self.e_icon.get_text().strip()
        if name.startswith("/"):
            self.icon_preview.set_from_file(name)
        else:
            self.icon_preview.set_from_icon_name(name or "image-missing", Gtk.IconSize.LARGE_TOOLBAR)

    def on_pick_icon(self, _):
        chooser = Gtk.FileChooserNative.new("Choose icon", self, Gtk.FileChooserAction.OPEN,
                                            "Choose", "Cancel")
        flt = Gtk.FileFilter()
        flt.set_name("Images")
        flt.add_mime_type("image/png")
        flt.add_mime_type("image/svg+xml")
        chooser.add_filter(flt)
        if chooser.run() == Gtk.ResponseType.ACCEPT and chooser.get_filename():
            self.e_icon.set_text(chooser.get_filename())
        chooser.destroy()

    # ---------------------------------------------------------------- notifications page

    def _notifications_page(self) -> Gtk.Widget:
        grid, add = self._grid_page()
        c = self.orig
        self.sw_notify = add("Notify about new mail", Gtk.Switch(active=c.notifications), expand=False)
        self.e_timeout = add("Hide notification after (seconds)",
                             Gtk.SpinButton.new_with_range(0, 600, 1),
                             "0 = keep it until you dismiss it", expand=False)
        self.e_timeout.set_value(c.notification_timeout)
        self.sw_notify.connect("notify::active", lambda *_: self.e_timeout.set_sensitive(
            self.sw_notify.get_active()))
        self.e_timeout.set_sensitive(c.notifications)
        test = Gtk.Button(label="Send a test notification")
        test.connect("clicked", self.on_test_notification)
        add("", test, expand=False)

        grid.attach(Gtk.Separator(margin_top=6, margin_bottom=6), 0, grid.row, 2, 1)
        grid.row += 1
        self.sw_autostart = Gtk.Switch()
        self.autostart_available = shutil.which("systemctl") is not None
        add("Start on login", self.sw_autostart,
            "Enables the gmail-tray user service (restarts it if it crashes).", expand=False)
        if self.autostart_available:
            self.sw_autostart.set_active(self._service_enabled())
            self.sw_autostart.connect("notify::active", self.on_autostart)
        else:
            self.sw_autostart.set_sensitive(False)
        return grid

    def on_test_notification(self, _):
        n = desktop.Notifier(self.e_title.get_text() or APP_NAME)
        n.show("3 unread messages", "you@gmail.com: 3", self.e_icon.get_text() or APP_ID,
               int(self.e_timeout.get_value()))
        self._test_notifier = n

    @staticmethod
    def _systemctl(*args) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args, f"{APP_ID}.service"],
                              capture_output=True, text=True, timeout=15)

    def _service_enabled(self) -> bool:
        try:
            return self._systemctl("is-enabled").stdout.strip() == "enabled"
        except (OSError, subprocess.SubprocessError):
            return False

    def on_autostart(self, switch, _pspec):
        action = "enable" if switch.get_active() else "disable"

        def done(res):
            if isinstance(res, Exception) or res.returncode != 0:
                err = res if isinstance(res, Exception) else res.stderr.strip()
                self.show_info(f"Could not {action} autostart: {err}", Gtk.MessageType.ERROR)
                switch.handler_block_by_func(self.on_autostart)
                switch.set_active(not switch.get_active())
                switch.handler_unblock_by_func(self.on_autostart)
        _run_async(lambda: self._systemctl(action), done)

    # ---------------------------------------------------------------- save

    def _collect(self) -> Config:
        browser = self.e_browser.get_child().get_text().strip()
        if browser == SYSTEM_DEFAULT_LABEL:
            browser = ""
        try:
            flags = shlex.split(self.e_flags.get_text())
        except ValueError as e:
            raise ConfigError(f"Browser options: {e}") from e
        if browser and not shutil.which(browser):
            raise ConfigError(f"Browser “{browser}” was not found")
        cfg = copy.deepcopy(self.orig)
        cfg.title = self.e_title.get_text().strip()
        cfg.url = self.e_url.get_text().strip()
        cfg.browser = browser
        cfg.flags = flags
        cfg.interval = int(self.e_interval.get_value())
        cfg.icon = self.e_icon.get_text().strip()
        cfg.notifications = self.sw_notify.get_active()
        cfg.notification_timeout = int(self.e_timeout.get_value())
        cfg.password_store = "keyring" if self.rb_keyring.get_active() else "fetchmailrc"
        cfg.accounts = copy.deepcopy(self.accounts)
        return cfg.validate()

    def on_save(self, _):
        try:
            new = self._collect()
            notes = account_store.save_all(self.orig, new, self.passwords, cfg_path=self.cfg_path)
        except ConfigError as e:
            self.show_info(str(e), Gtk.MessageType.ERROR)
            return
        except Exception as e:  # noqa: BLE001  (keyring, disk…)
            log.exception("Saving failed")
            self.show_info(f"Saving failed: {e}", Gtk.MessageType.ERROR)
            return
        self.orig = new
        self.passwords.clear()

        legacy = new.fetchmailrc_path
        if new.managed and new.password_store == "keyring" and account_store.legacy_rc_has_passwords(legacy):
            self._offer_delete_legacy(legacy)
            notes = [n for n in notes if str(legacy) not in n]
        if notes:
            dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                    buttons=Gtk.ButtonsType.OK, text="Saved, with notes")
            dlg.format_secondary_text("\n\n".join(notes))
            dlg.run()
            dlg.destroy()
        self.destroy()

    def _offer_delete_legacy(self, path: Path):
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE, text=f"Delete {path}?")
        dlg.format_secondary_text(
            "Your passwords are now in the keyring, but this file still holds them in plain text. "
            "Delete it unless you use fetchmail for something else (e.g. downloading mail).")
        dlg.add_button("Keep it", Gtk.ResponseType.NO)
        btn = dlg.add_button("Delete", Gtk.ResponseType.YES)
        btn.get_style_context().add_class("destructive-action")
        if dlg.run() == Gtk.ResponseType.YES:
            try:
                account_store.delete_legacy_rc(path)
            except OSError as e:
                log.error("Cannot delete %s: %s", path, e)
        dlg.destroy()


def run_standalone(cfg_path: Path | None = None) -> int:
    win = SettingsWindow(cfg_path)
    win.connect("destroy", Gtk.main_quit)
    Gtk.main()
    return 0
