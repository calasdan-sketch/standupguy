"""HTTP bridge for workflow tools (n8n, Activepieces, etc.).

n8n can't easily call a CLI on a schedule and parse stdout — but its
HTTP Request node can hit this. Run `standupguy serve`, then point
n8n at http://localhost:8765/digest.

Endpoints:
  GET /health                       -> {"ok": true}
  GET /digest?since=&until=&author=&format= -> standup digest (json|markdown)
  GET /week?author=&format=          -> weekly summary (json|markdown)

Binds 127.0.0.1 by default. Only open --host 0.0.0.0 if you know why.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import digest as digest_mod
from . import gitlog as gitlog_mod


# Per-connection socket timeout (seconds). A connection that goes quiet for
# longer than this gets closed instead of holding a thread — and with the
# single-threaded HTTPServer it previously held the WHOLE bridge hostage.
CONNECTION_TIMEOUT = 30


class ThreadedBridgeServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a per-connection socket timeout.

    One thread per connection so a stalled peer can never wedge the
    bridge; the socket timeout bounds how long a quiet connection can
    linger.
    """

    def get_request(self):
        sock, addr = self.socket.accept()
        sock.settimeout(CONNECTION_TIMEOUT)
        return sock, addr


def make_server(root: str, host: str = "127.0.0.1",
                port: int = 8765) -> ThreadedBridgeServer:
    """Build the configured bridge server (used by serve() and by tests)."""
    Handler.root = root
    return ThreadedBridgeServer((host, port), Handler)


def _payload(root: str, query: dict, weekly: bool) -> tuple[int, str, str]:
    """(status, content_type, body)."""
    author = query.get("author", [None])[0] or None
    fmt = (query.get("format", ["json"])[0] or "json").lower()
    if weekly:
        md = digest_mod.weekly_markdown(root, author)
        commits = gitlog_mod.commits_since(root, "7 days ago", author)
        label = "week"
    else:
        since = query.get("since", ["yesterday"])[0] or "yesterday"
        until = query.get("until", [None])[0] or None
        label = f"{since} → {until}" if until else f"since {since}"
        md = digest_mod.digest_markdown(root, since, label, author, until)
        commits = gitlog_mod.commits_since(root, since, author, until)
    if fmt == "markdown":
        return 200, "text/markdown; charset=utf-8", md
    body = json.dumps({
        "repo": gitlog_mod.repo_name(root), "label": label,
        "in_progress": gitlog_mod.uncommitted(root),
        "commits": [{"sha": c.sha, "author": c.author, "date": c.date,
                     "subject": c.subject, "files": c.files,
                     "pr": c.pr_number} for c in commits],
        "markdown": md,
    })
    return 200, "application/json", body


class Handler(BaseHTTPRequestHandler):
    root = os.getcwd()

    def _send(self, status: int, ctype: str, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send(200, "application/json", '{"ok": true}')
        elif parsed.path == "/digest":
            try:
                s, t, b = _payload(self.root, query, weekly=False)
                self._send(s, t, b)
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}))
        elif parsed.path == "/week":
            try:
                s, t, b = _payload(self.root, query, weekly=True)
                self._send(s, t, b)
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}))
        else:
            self._send(404, "application/json",
                       '{"error": "use /digest, /week, or /health"}')

    def log_message(self, *args):  # noqa: ANN001, ANN202
        pass


def serve(root: str, host: str = "127.0.0.1", port: int = 8765) -> int:
    server = make_server(root, host, port)
    print(f"standupguy serving {root} on http://{host}:{port} "
          f"(/digest, /week, /health)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
