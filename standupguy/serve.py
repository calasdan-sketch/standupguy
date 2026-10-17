"""HTTP bridge for workflow tools (n8n, Activepieces, etc.).

n8n can't easily call a CLI on a schedule and parse stdout — but its
HTTP Request node can hit this. Run `standupguy serve`, then point
n8n at http://localhost:8765/digest.

Endpoints:
  GET /health                       -> {"ok": true}
  GET /digest?since=&until=&author=&all=&format= -> standup digest (json|markdown)
  GET /week?since=&until=&author=&all=&format=   -> weekly summary (json|markdown)

Defaults match the CLI: since=yesterday (week: 7 days ago), author = the
repo's git user.email (all=1 for everyone), format=json. The JSON has the
same `commits` shape as `standupguy --format json`, plus `markdown` (raw)
and `slack_text` (sanitized + escaped for posting to Slack).

Binds 127.0.0.1 by default. Only open --host 0.0.0.0 if you know why.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import digest as digest_mod
from . import gitlog as gitlog_mod
from . import push as push_mod


# Per-connection socket timeout (seconds). A connection that goes quiet for
# longer than this gets closed instead of holding a thread — and with the
# single-threaded HTTPServer it previously held the WHOLE bridge hostage.
CONNECTION_TIMEOUT = 30

_TRUE = {"1", "true", "yes", "on"}


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


def _author(root: str, query: dict) -> str | None:
    """Same default as the CLI: your git user.email; `all=1` for everyone."""
    if (query.get("all", [""])[0] or "").lower() in _TRUE:
        return None
    return (query.get("author", [None])[0] or None
            or gitlog_mod.default_author(root) or None)


def _payload(root: str, query: dict, weekly: bool) -> tuple[int, str, str]:
    """(status, content_type, body)."""
    author = _author(root, query)
    fmt = (query.get("format", ["json"])[0] or "json").lower()
    until = query.get("until", [None])[0] or None
    if weekly:
        since = query.get("since", ["7 days ago"])[0] or "7 days ago"
        label = "week"
    else:
        since = query.get("since", ["yesterday"])[0] or "yesterday"
        label = f"{since} → {until}" if until else f"since {since}"
    for value in (since, until):
        if value:
            gitlog_mod.check_date(value)  # ValueError -> 400 below
    # git runs ONCE per request; the markdown and the JSON share the facts
    commits = gitlog_mod.commits_since(root, since, author, until)
    in_progress = gitlog_mod.uncommitted(root)
    if weekly:
        md = digest_mod.weekly_markdown(root, author, since, until,
                                        commits=commits)
    else:
        md = digest_mod.digest_markdown(root, since, label, author, until,
                                        commits=commits,
                                        in_progress=in_progress)
    if fmt == "markdown":
        return 200, "text/markdown; charset=utf-8", md
    data = digest_mod.to_json(root, label, since, until, commits,
                              in_progress, markdown=md)
    # ready-to-post Slack text (escaped & < >, mass mentions neutralized):
    # a workflow that posts `markdown` straight into Slack would otherwise
    # forward live <@user>/<!subteam> markup from commit messages.
    data["slack_text"] = push_mod.sanitize_slack_text(md)
    return 200, "application/json", json.dumps(data)


class Handler(BaseHTTPRequestHandler):
    root = os.getcwd()

    def _send(self, status: int, ctype: str, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _digest(self, query: dict, weekly: bool) -> None:
        try:
            s, t, b = _payload(self.root, query, weekly=weekly)
            self._send(s, t, b)
        except ValueError as e:
            self._send(400, "application/json", json.dumps({"error": str(e)}))
        except Exception as e:
            self._send(500, "application/json", json.dumps({"error": str(e)}))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send(200, "application/json", '{"ok": true}')
        elif parsed.path == "/digest":
            self._digest(query, weekly=False)
        elif parsed.path == "/week":
            self._digest(query, weekly=True)
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
