"""Tray icon with an unread-count badge drawn on it.

Many trays (KDE Plasma, Waybar) ignore AppIndicator labels, so the count is
painted into the icon itself and handed to the tray as a themed icon.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gtk  # noqa: E402

log = logging.getLogger(__name__)

SIZE = 64
RED = (0.86, 0.15, 0.15)
AMBER = (0.96, 0.69, 0.05)


def _base_pixbuf(icon: str, size: int):
    try:
        if icon.startswith("/"):
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(icon, size, size, True)
        theme = Gtk.IconTheme.get_default() or Gtk.IconTheme.new()  # no display: plain theme
        for name in (icon, "mail-unread", "mail-message"):
            info = theme.lookup_icon(name, size, Gtk.IconLookupFlags.FORCE_SIZE)
            if info:
                return info.load_icon()
    except GLib.Error as e:
        log.warning("Cannot load icon %s: %s", icon, e.message)
    return None


def render(icon: str, text: str, color: tuple, out_path: Path, size: int = SIZE) -> Path:
    """Draw *icon* with a round badge showing *text* in the top-right corner."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    base = _base_pixbuf(icon, size)
    if base is not None:
        # Via PNG bytes: works without the gi<->cairo "foreign" bridge.
        ok, png = base.save_to_bufferv("png", [], [])
        if ok:
            img = cairo.ImageSurface.create_from_png(io.BytesIO(png))
            cr.set_source_surface(img, (size - img.get_width()) / 2, (size - img.get_height()) / 2)
            cr.paint()

    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(size * (0.42 if len(text) == 1 else 0.36))
    ext = cr.text_extents(text)
    h = size * 0.56                       # badge height
    w = max(h, ext.width + size * 0.22)   # pill for 2–3 digits
    x, y = size - w, 0.0
    r = h / 2

    cr.new_path()
    cr.arc(x + r, y + r, r, math.pi / 2, 3 * math.pi / 2)
    cr.arc(x + w - r, y + r, r, -math.pi / 2, math.pi / 2)
    cr.close_path()
    cr.set_source_rgb(*color)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, 0.9)     # thin ring keeps it readable on any panel
    cr.set_line_width(size * 0.035)
    cr.stroke()

    if color == AMBER:
        cr.set_source_rgb(0, 0, 0)
    else:
        cr.set_source_rgb(1, 1, 1)
    cr.move_to(x + (w - ext.width) / 2 - ext.x_bearing, y + (h - ext.height) / 2 - ext.y_bearing)
    cr.show_text(text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    surface.write_to_png(str(tmp))
    tmp.replace(out_path)
    return out_path


class BadgeIcons:
    """Renders badge icons into a private directory and returns icon names for it."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def label(count: int) -> str:
        return "99+" if count > 99 else str(count)

    def name_for(self, icon: str, text: str, color: tuple) -> str:
        # Unique name per state: trays cache icons by name.
        tag = hashlib.sha1(f"{icon}|{text}|{color}".encode()).hexdigest()[:10]
        name = f"gmail-tray-badge-{tag}"
        path = self.dir / f"{name}.png"
        if not path.exists():
            render(icon, text, color, path)
        self._prune(keep=path)
        return name

    def _prune(self, keep: Path, limit: int = 20):
        files = sorted(self.dir.glob("gmail-tray-badge-*.png"), key=lambda p: p.stat().st_mtime)
        for p in files[:-limit]:
            if p != keep:
                p.unlink(missing_ok=True)
