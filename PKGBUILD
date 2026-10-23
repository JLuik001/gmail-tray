# Maintainer: Jyri Luik
# Contributor: Maykon Marcos Junior <maykon.marcos.junior.dev@gmail.com>
pkgname=gmail-tray-git
pkgver=2.2.0.r0.g0000000
pkgrel=1
pkgdesc="Minimal Gmail tray notifier with unread counter, keyring support and a settings GUI"
arch=('any')
url="https://github.com/JLuik001/gmail-tray"
license=('MIT')
depends=('fetchmail'
         'gnupg'
         'gtk3'
         'libayatana-appindicator'
         'libnotify'
         'libsecret'
         'python'
         'python-cairo'
         'python-gobject')
optdepends=('org.freedesktop.secrets: keep passwords in the desktop keyring (gnome-keyring, kwallet, keepassxc)')
makedepends=('git')
provides=('gmail-tray')
conflicts=('gmail-tray' 'gmail-tray-appimage')
source=("gmail-tray::git+${url}.git"
        'gmail-tray.svg::https://upload.wikimedia.org/wikipedia/commons/archive/7/7e/20201210105307%21Gmail_icon_%282020%29.svg')
sha256sums=('SKIP'
            '7b6c2b6e3b67271a2a27e11986265745de3ffe8e84f692be79f6f554ac405275')

pkgver() {
  cd gmail-tray
  local base
  base=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' gmail_tray/__init__.py)
  printf "%s.r%s.g%s" "$base" "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
}

check() {
  cd gmail-tray
  python -m unittest discover -s tests
}

package() {
  cd gmail-tray
  make DESTDIR="$pkgdir" PREFIX=/usr install
  install -Dm644 "$srcdir/gmail-tray.svg" "$pkgdir/usr/share/icons/hicolor/scalable/apps/gmail-tray.svg"
}
