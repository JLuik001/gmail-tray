# Gmail-Tray

Minimal Gmail notifier with a tray icon, unread counter and a settings window.
It uses [fetchmail](https://www.fetchmail.info) to check your mailboxes and
opens Gmail in your browser with one click.

Originally written by [Maykon Marcos Junior](https://github.com/maykonmarcosjuniordev-star/gmail-tray);
this fork adds the settings GUI, keyring storage and a security overhaul.

# Table of Contents
*   [Features](#features)
*   [Install](#install)
*   [Setting up accounts](#setting-up-accounts)
*   [Customizations](#customizations)
*   [Security](#security)
*   [Command line](#command-line)
*   [SNI (Waybar) support](#sni-waybar-support)
*   [Development](#development)

# Features
*   [x] Regular email checking (several accounts, checked in parallel)
*   [x] Clickable system notifications — open the inbox of the account that got mail
*   [x] Tray icon with unread counter, per-account counts and errors in the menu
*   [x] **Settings window for fetchmail accounts** — add, edit, test, import
*   [x] **Settings window for customizations** — browser, flags, interval, icon, notifications, autostart
*   [x] **Passwords in the desktop keyring** (GNOME Keyring, KWallet, KeePassXC…) instead of a plain-text file
*   [x] Config changes apply immediately, no restart needed
*   [x] Gmail, Yahoo, iCloud, Fastmail presets, any IMAP/POP3 server
*   [x] systemd user service for autostart and crash recovery
*   [x] Waybar module output
*   [x] Full Wayland and Xorg support, works with any libnotify notification daemon

# Install

Arch Linux:

```sh
git clone https://github.com/JLuik001/gmail-tray.git
cd gmail-tray
makepkg -si
```

Any other distro (needs fetchmail, python-gobject, gtk3, libayatana-appindicator, libnotify, libsecret):

```sh
sudo make install           # PREFIX=/usr by default
```

Start it from the app menu, or enable autostart (also a switch in Settings):

```sh
systemctl --user enable --now gmail-tray.service
```

# Setting up accounts

1.  Turn on 2-Step Verification for your Google account and create an
    [app password](https://myaccount.google.com/apppasswords).
2.  Run `gmail-tray --settings` (or *Settings…* in the tray menu, or right-click the app icon).
3.  *Accounts → Add…*, enter your address and the app password, press *Test connection*, then *Save*.

Already have a `~/.fetchmailrc`? Click *Import from .fetchmailrc…*. Your file is
never modified; after saving, the app offers to delete it if your passwords are
now in the keyring (keep it if you use fetchmail to actually download mail).

If you don't add accounts in Settings, an existing `~/.fetchmailrc` keeps working as before.

# Customizations

Everything is in the *General* and *Notifications & startup* tabs. The config file
is `~/.config/gmail-tray/gmail-tray-configs.json` and can still be edited by hand:

| Key | Meaning |
| --- | --- |
| `url` | Page to open (https only). Gmail accounts open their own inbox. |
| `browser` | Command name or path; empty = system default browser |
| `flags` | Extra browser arguments, e.g. `["--new-tab"]`, `["-P", "work"]` |
| `interval` | Seconds between checks (30–86400) |
| `icon` | Icon theme name or image path |
| `title` | App title |
| `notifications`, `notification_timeout` | On/off, seconds (0 = until dismissed) |
| `password_store` | `keyring` (recommended) or `fetchmailrc` |
| `accounts` | Managed accounts (no passwords in here) |

# Security

*   No shell is ever used: fetchmail, the browser and notifications get argument lists,
    so config values or mail-server output cannot inject commands.
*   Passwords live in the Secret Service keyring and are piped to fetchmail through stdin —
    they never touch the disk. Optional file mode writes `~/.config/gmail-tray/fetchmailrc` with mode 0600.
*   TLS is mandatory and certificates are verified (`ssl sslcertck`, or STARTTLS).
*   Config is written atomically with mode 0600; old world-readable configs are tightened.
*   Lock and pid files are in `$XDG_RUNTIME_DIR`, not the world-writable `/tmp`.
*   All config values are validated (https-only URL, hostnames, ports, no control characters).
*   Text shown in notifications is escaped (no markup injection from mailbox names).
*   fetchmail runs with its own pid file, so it never clashes with a fetchmail daemon you run.

# Command line

```
gmail-tray               start the tray (if already running: open Gmail)
gmail-tray --settings    open the settings window
gmail-tray --check       check all accounts, print the result (exit 1 on errors)
gmail-tray --once        print Waybar JSON and exit
gmail-tray -v            debug logging
```

# (SNI) Waybar support

Waybar's tray shows the icon but not the unread label, so use a custom module:

```jsonc
"custom/gmail": {
    "exec": "gmail-tray --once",
    "interval": 60,
    "return-type": "json",
    "format": " {} ",
    "on-click": "gmail-tray"
},
```

The module gets the CSS classes `unread`, `none` or `error`:

```css
#custom-gmail.unread { color: #ea4335; }
#custom-gmail.error  { color: #fbbc05; }
```

# Development

```sh
python3 bin/gmail-tray --settings   # runs straight from the checkout
make test                           # unit tests + real fetchmail against a fake IMAP server
makepkg --printsrcinfo > .SRCINFO
```
