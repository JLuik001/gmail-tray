import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmail_tray import account_store, config, fetchmail  # noqa: E402
from gmail_tray.config import Account, Config, ConfigError  # noqa: E402


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "cfg"),
            "XDG_RUNTIME_DIR": os.path.join(self.tmp, "run"),
            "HOME": self.tmp,
        })
        self.env.start()
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp)


class ConfigTests(TempHome):
    def test_first_run_creates_private_config(self):
        cfg = config.load()
        path = config.config_path()
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(cfg.password_store, "keyring")

    def test_old_config_is_accepted_and_clamped(self):
        path = config.config_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"url": "https://mail.google.com", "icon": "gmail-tray",
                                    "title": "Gmail-Tray", "interval": 5, "browser": "firefox",
                                    "flags": ["--ProfileManager", "--new-tab"], "custom": 1}))
        os.chmod(path, 0o644)
        cfg = config.load()
        self.assertEqual(cfg.interval, config.MIN_INTERVAL)
        self.assertEqual(cfg.flags, ["--ProfileManager", "--new-tab"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        config.save(cfg)
        self.assertEqual(json.loads(path.read_text())["custom"], 1)

    def test_rejects_dangerous_values(self):
        for bad in ({"url": "javascript:alert(1)"}, {"url": "http://mail.google.com"},
                    {"url": "file:///etc/passwd"}, {"title": "a\nb"}, {"interval": "30"},
                    {"password_store": "plain"}, {"flags": [1]},
                    {"accounts": [{"user": 'a"b', "server": "imap.gmail.com"}]},
                    {"accounts": [{"user": "a", "server": "imap.gmail.com; rm -rf /"}]},
                    {"accounts": [{"user": "a", "server": "x", "protocol": "SMTP"}]}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                Config.from_dict(bad)

    def test_duplicate_accounts(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"accounts": [{"user": "A@x.ee"}, {"user": "a@x.ee"}]})

    def test_runtime_dir_private(self):
        d = config.runtime_dir()
        self.assertTrue(str(d).startswith(os.environ["XDG_RUNTIME_DIR"]))
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)


class RcTests(unittest.TestCase):
    def test_roundtrip_with_nasty_password(self):
        accs = [(Account("x@gmail.com"), 'p"q\\r\'s \\042 #x'),
                (Account("y@o.ee", "mail.o.ee", "POP3", 995, True), "z"),
                (Account("z@o.ee", "mail.o.ee", "IMAP", 143, False, "Work Box"), "w")]
        text = fetchmail.render_rc(accs)
        self.assertTrue(text.startswith(fetchmail.GENERATED_HEADER))
        self.assertNotIn('\\"', text)
        self.assertEqual(fetchmail.parse_rc(text), accs)

    def test_control_chars_rejected(self):
        with self.assertRaises(ConfigError):
            fetchmail.render_rc([(Account("x@gmail.com"), "a\nskip evil")])

    def test_plaintext_never_rendered(self):
        text = fetchmail.render_rc([(Account("x@o.ee", "mail.o.ee", ssl=False), "pw")])
        self.assertIn("sslcertck", text)
        self.assertNotIn("sslproto", text)

    def test_import_handwritten(self):
        text = """# my mail
poll imap.gmail.com with proto IMAP
       user 'someone@gmail.com' there with password "abcd efgh ijkl mnop" is me here
       ssl
poll pop.example.com proto pop3 port 995: user "bob" pass secret, folder Sent
skip old.example.com proto imap user gone password x
"""
        got = fetchmail.parse_rc(text)
        self.assertEqual([(a.user, a.server, a.protocol, a.port, a.ssl, p) for a, p in got], [
            ("someone@gmail.com", "imap.gmail.com", "IMAP", 0, True, "abcd efgh ijkl mnop"),
            ("bob", "pop.example.com", "POP3", 995, True, "secret"),
            ("gone", "old.example.com", "IMAP", 0, False, "x"),
        ])


class OutputTests(unittest.TestCase):
    def test_parse_output(self):
        out = """5 messages (2 seen) for a@gmail.com at imap.gmail.com (folder INBOX).
fetchmail: No mail for b@gmail.com at imap.gmail.com
1 message for c@x.ee at mail.x.ee (1234 octets).
2 messages (2 seen) for d@x.ee at mail.x.ee.
garbage line
"""
        expected = [("a@gmail.com", "imap.gmail.com", 3, 5), ("b@gmail.com", "imap.gmail.com", 0, 0),
                    ("c@x.ee", "mail.x.ee", 1, 1), ("d@x.ee", "mail.x.ee", 0, 2)]
        got = [(s.user, s.server, s.unread, s.total) for s in fetchmail.parse_output(out)]
        self.assertEqual(got, expected)

    def test_parse_output_fetchmail_66(self):
        # fetchmail >= 6.5 puts a timestamp and program name in front of every line
        out = """Oct 23 09:35:37 fetchmail: 5 messages (2 seen) for a@gmail.com at imap.gmail.com (folder INBOX).
Oct  3 09:35:37 fetchmail: No mail for b@gmail.com at imap.gmail.com (folder INBOX)
Oct 23 09:35:38 fetchmail: 1 message for c@x.ee at mail.x.ee (1234 octets).
"""
        got = [(s.user, s.server, s.unread, s.total) for s in fetchmail.parse_output(out)]
        self.assertEqual(got, [("a@gmail.com", "imap.gmail.com", 3, 5), ("b@gmail.com", "imap.gmail.com", 0, 0),
                               ("c@x.ee", "mail.x.ee", 1, 1)])

    def test_error_text_strips_prefix(self):
        import subprocess
        proc = subprocess.CompletedProcess([], 3, "", "Oct 23 06:38:00 fetchmail: Authorization failure on "
                                           "me@x.ee@host\nOct 23 06:38:00 fetchmail: For help, see the FAQ\n")
        self.assertEqual(fetchmail._error_text(proc),
                         "Authentication failed (for Gmail use an app password): Authorization failure on me@x.ee@host")


class DictBackend:
    def __init__(self):
        self.data = {}
        self.lookup = lambda a: self.data.get(a.key)
        self.store = lambda a, p: self.data.__setitem__(a.key, p)
        self.clear = lambda a: self.data.pop(a.key, None)


class StoreTests(TempHome):
    def test_keyring_flow_and_switching(self):
        kr = DictBackend()
        old = config.load()
        a, b = Account("a@gmail.com"), Account("b@gmail.com")
        new = Config.from_dict(old.to_dict())
        new.accounts = [a, b]
        with self.assertRaises(ConfigError):     # passwords are required
            account_store.save_all(old, new, {a.key: "pw-A-7781"}, backend=kr)
        self.assertEqual(kr.data, {})
        account_store.save_all(old, new, {a.key: "pw-A-7781", b.key: "pw-B-9932"}, backend=kr)
        self.assertEqual(kr.data, {a.key: "pw-A-7781", b.key: "pw-B-9932"})
        self.assertNotIn("pw-A-7781", config.config_path().read_text())
        self.assertFalse(config.managed_rc_path().exists())

        # switch to file storage: passwords move, keyring is wiped
        old, new = new, Config.from_dict(new.to_dict())
        new.password_store = "fetchmailrc"
        new.accounts = [a]
        account_store.save_all(old, new, {}, backend=kr)
        self.assertEqual(kr.data, {})
        rc = config.managed_rc_path()
        self.assertEqual(stat.S_IMODE(rc.stat().st_mode), 0o600)
        self.assertEqual(account_store.read_managed_rc(), {a.key: "pw-A-7781"})

        # and back to the keyring: generated file is deleted
        old, new = new, Config.from_dict(new.to_dict())
        new.password_store = "keyring"
        account_store.save_all(old, new, {}, backend=kr)
        self.assertEqual(kr.data, {a.key: "pw-A-7781"})
        self.assertFalse(rc.exists())

    def test_warns_about_plaintext_legacy_rc(self):
        kr = DictBackend()
        legacy = Path(self.tmp) / ".fetchmailrc"
        legacy.write_text('poll imap.gmail.com proto imap user "a@gmail.com" password "x" ssl\n')
        old = config.load()
        new = Config.from_dict(old.to_dict())
        new.accounts = [Account("a@gmail.com")]
        notes = account_store.save_all(old, new, {new.accounts[0].key: "x"}, backend=kr)
        self.assertTrue(any(".fetchmailrc" in n for n in notes))
        self.assertTrue(legacy.exists(), "user's own file must never be touched automatically")


@unittest.skipUnless(shutil.which("fetchmail"), "fetchmail not installed")
class FetchmailIntegration(TempHome):
    """Runs the real fetchmail against a local fake IMAP server."""

    def setUp(self):
        super().setUp()
        from fake_imap import FakeImap
        self.server = FakeImap(total=5, unseen=3, password='se"c\\ret 1')
        # The fake server has no TLS; allow plain text for this test only.
        orig = fetchmail.render_rc
        self.patch = mock.patch.object(
            fetchmail, "render_rc",
            lambda e, header=True: orig(e, header).replace(" sslcertck", ' sslproto ""'))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.server.stop()
        super().tearDown()

    def acc(self, **kw):
        return Account("me@x.ee", "127.0.0.1", port=self.server.port, ssl=False, **kw)

    def test_unread_count(self):
        st = fetchmail.check_account(self.acc(), 'se"c\\ret 1')
        self.assertEqual((st.unread, st.total, st.error), (3, 5, None))
        st = fetchmail.check_account(self.acc(folder="INBOX"), 'se"c\\ret 1')
        self.assertEqual(st.unread, 3)

    def test_bad_password(self):
        st = fetchmail.check_account(self.acc(), "wrong")
        self.assertFalse(st.ok)
        self.assertIn("Authentication failed", st.error)

    def test_no_mail(self):
        self.server.total = self.server.unseen = 0
        st = fetchmail.check_account(self.acc(), 'se"c\\ret 1')
        self.assertEqual((st.unread, st.error), (0, None))

    def test_check_all_keyring_mode(self):
        cfg = Config(accounts=[self.acc()])
        res = fetchmail.check_all(cfg, password_lookup=lambda a: 'se"c\\ret 1')
        self.assertEqual(res[0].unread, 3)
        res = fetchmail.check_all(cfg, password_lookup=lambda a: None)
        self.assertIn("No password", res[0].error)

    def test_file_mode(self):
        cfg = Config(password_store="fetchmailrc", accounts=[self.acc()])
        account_store.save_all(Config(), cfg, {self.acc().key: 'se"c\\ret 1'})
        res = fetchmail.check_all(cfg)
        self.assertEqual((res[0].user, res[0].unread), ("me@x.ee", 3))

    def test_rc_permissions_checked(self):
        rc = Path(self.tmp) / "rc"
        rc.write_text("poll x\n")
        os.chmod(rc, 0o644)
        res = fetchmail.check_rcfile(rc)
        self.assertIn("chmod 600", res[0].error)


if __name__ == "__main__":
    unittest.main()
