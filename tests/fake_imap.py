"""Tiny plain-text IMAP server, just enough for `fetchmail --check`."""

import re
import socketserver
import threading


class _Handler(socketserver.StreamRequestHandler):
    def send(self, line):
        self.wfile.write((line + "\r\n").encode())

    def handle(self):
        srv = self.server
        self.send("* OK IMAP4rev1 fake ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").strip()
            srv.log.append(line)
            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            tag, cmd = parts[0], parts[1].upper()
            arg = parts[2] if len(parts) > 2 else ""
            if cmd == "CAPABILITY":
                self.send("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
                self.send(f"{tag} OK done")
            elif cmd == "LOGIN":
                user, _, pw = arg.partition(" ")
                if re.sub(r'\\(.)', r'\1', pw.strip()[1:-1] if pw.startswith('"') else pw) == srv.password:
                    self.send(f"{tag} OK logged in")
                else:
                    self.send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
            elif cmd in ("SELECT", "EXAMINE"):
                self.send(f"* {srv.total} EXISTS")
                self.send("* 0 RECENT")
                if srv.unseen:
                    self.send(f"* OK [UNSEEN {srv.total - srv.unseen + 1}]")
                self.send("* OK [UIDVALIDITY 1]")
                self.send(f"{tag} OK [READ-ONLY] done")
            elif cmd == "SEARCH" or (cmd == "UID" and arg.upper().startswith("SEARCH")):
                ids = " ".join(str(i) for i in range(srv.total - srv.unseen + 1, srv.total + 1))
                self.send(f"* SEARCH {ids}".rstrip())
                self.send(f"{tag} OK done")
            elif cmd == "LOGOUT":
                self.send("* BYE")
                self.send(f"{tag} OK bye")
                return
            else:
                self.send(f"{tag} OK done")


class FakeImap(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, total=5, unseen=3, password="secret"):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.total, self.unseen, self.password = total, unseen, password
        self.log = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()
