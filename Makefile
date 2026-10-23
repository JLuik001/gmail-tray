PREFIX  ?= /usr
DESTDIR ?=
LIBDIR  := $(DESTDIR)$(PREFIX)/lib/gmail-tray
PYTHON  ?= python3

.PHONY: all install uninstall test check

all:

install:
	install -Dm755 bin/gmail-tray "$(DESTDIR)$(PREFIX)/bin/gmail-tray"
	install -d "$(LIBDIR)/gmail_tray"
	install -m644 gmail_tray/*.py "$(LIBDIR)/gmail_tray/"
	install -Dm644 data/gmail-tray-configs.json "$(DESTDIR)$(PREFIX)/share/gmail-tray/gmail-tray-configs.json"
	install -Dm644 data/gmail-tray.desktop "$(DESTDIR)$(PREFIX)/share/applications/gmail-tray.desktop"
	install -Dm644 data/gmail-tray.service "$(DESTDIR)$(PREFIX)/lib/systemd/user/gmail-tray.service"
	install -Dm644 LICENSE "$(DESTDIR)$(PREFIX)/share/licenses/gmail-tray/LICENSE"

uninstall:
	rm -rf "$(LIBDIR)" "$(DESTDIR)$(PREFIX)/share/gmail-tray"
	rm -f "$(DESTDIR)$(PREFIX)/bin/gmail-tray" \
	      "$(DESTDIR)$(PREFIX)/share/applications/gmail-tray.desktop" \
	      "$(DESTDIR)$(PREFIX)/lib/systemd/user/gmail-tray.service" \
	      "$(DESTDIR)$(PREFIX)/share/licenses/gmail-tray/LICENSE"

test:
	$(PYTHON) -m unittest discover -s tests -v

check: test
	$(PYTHON) -m py_compile gmail_tray/*.py bin/gmail-tray
