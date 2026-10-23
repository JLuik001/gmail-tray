"""Gmail Tray — minimal mail notifier built on fetchmail."""

__version__ = "2.0.2"
APP_ID = "gmail-tray"
APP_NAME = "Gmail Tray"

# Pin GTK 3 before anything imports gi.repository.Gtk: on systems that also
# have GTK 4 installed, an unpinned import would load 4.0 and break the tray.
try:
    import gi

    for _ns, _ver in (("Gtk", "3.0"), ("Gdk", "3.0")):
        try:
            gi.require_version(_ns, _ver)
        except ValueError:
            pass
except ImportError:  # pragma: no cover
    pass
