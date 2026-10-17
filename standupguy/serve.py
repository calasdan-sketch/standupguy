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

Several repos: `standupguy serve --root A --root B` answers for all of
them, like the multi-repo digest: one markdown section per repo, in the
order given, and JSON of the form {"repos": [one object per repo, as in
`standupguy --format json`], "markdown": ..., "slack_text": ...}. With one
root the JSON stays flat. Each repo's default author is its own
git user.email.

Errors: an impossible date or other bad parameter is a 400 that says why.
Anything that fails inside the bridge (the repo moved, git is gone) is a 500
with a generic message: git's command line and your paths stay out of the
response, and go to the server's stderr instead, one line per failure.

Binds 127.0.0.1 by default. Only open --host 0.0.0.0 if you know why.
"""

from __future__ import annotations

import errno
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import digest as digest_mod
from . import gitlog as gitlog_mod
from . import push as push_mod
from . import stdio as stdio_mod


# Per-connection socket timeout (seconds). A connection that goes quiet for
# longer than this gets closed instead of holding a thread — and with the
# single-threaded HTTPServer it previously held the WHOLE bridge hostage.
CONNECTION_TIMEOUT = 30

_TRUE = {"1", "true", "yes", "on"}

# What a client is told when a request fails inside the bridge.
INTERNAL_ERROR = "internal error (the details are in the server's log)"


class ThreadedBridgeServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a per-connection socket timeout.

    One thread per connection so a stalled peer can never wedge the
    bridge; the socket timeout bounds how long a quiet connection can
    linger.
    """

    # POSIX: SO_REUSEADDR only lets a restart skip TIME_WAIT. Windows: it
    # lets a second server bind a port somebody is already listening on
    # (and take part of its requests), so "port in use" would never be an
    # error there. Leave it off on Windows.
    allow_reuse_address = os.name != "nt"

    roots: list   # the repos this server answers for (set by make_server)

    def get_request(self):
        sock, addr = self.socket.accept()
        sock.settimeout(CONNECTION_TIMEOUT)
        return sock, addr

    def handle_error(self, request, client_address):
        # A client that resets or drops its connection (BrokenPipeError,
        # ConnectionResetError, ConnectionAbortedError) is not worth a
        # traceback on the console; everything else still gets one.
        if stdio_mod.client_gone(sys.exc_info()[1]):
            return
        super().handle_error(request, client_address)


def make_server(root, host: str = "127.0.0.1",
                port: int = 8765) -> ThreadedBridgeServer:
    """Build the configured bridge server (used by serve() and by tests).
    `root` is one repo path or a list of them. The roots live on the
    server, not on the Handler class, so two servers never share them."""
    server = ThreadedBridgeServer((host, port), Handler)
    server.roots = [root] if isinstance(root, str) else list(root)
    return server


def _author(root: str, query: dict) -> str | None:
    """Same default as the CLI: your git user.email; `all=1` for everyone."""
    if (query.get("all", [""])[0] or "").lower() in _TRUE:
        return None
    return (query.get("author", [None])[0] or None
            or gitlog_mod.default_author(root) or None)


def _payload(roots: list, query: dict, weekly: bool) -> tuple[int, str, str]:
    """(status, content_type, body) for one repo or several."""
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
    sections, repos = [], []
    for root in roots:
        author = _author(root, query)    # each repo's own user, like the CLI
        # git runs ONCE per repo per request; markdown and JSON share the facts
        commits = gitlog_mod.commits_since(root, since, author, until)
        in_progress = gitlog_mod.uncommitted(root)
        if weekly:
            md = digest_mod.weekly_markdown(root, author, since, until,
                                            commits=commits)
        else:
            md = digest_mod.digest_markdown(root, since, label, author, until,
                                            commits=commits,
                                            in_progress=in_progress)
        sections.append(md)
        repos.append(digest_mod.to_json(root, label, since, until, commits,
                                        in_progress))
    md = "\n".join(sections)
    if fmt == "markdown":
        return 200, "text/markdown; charset=utf-8", md
    if len(repos) == 1:
        data = repos[0]               # one root: the flat object, as ever
        data["markdown"] = md
    else:
        data = {"repos": repos, "markdown": md}
    # ready-to-post Slack text (escaped & < >, mass mentions neutralized):
    # a workflow that posts `markdown` straight into Slack would otherwise
    # forward live <@user>/<!subteam> markup from commit messages.
    data["slack_text"] = push_mod.sanitize_slack_text(md)
    return 200, "application/json", json.dumps(data)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, ctype: str, body: str) -> None:
        data = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError as e:       # the client hung up mid-response
            if not stdio_mod.client_gone(e):
                raise
            self.close_connection = True

    def _digest(self, query: dict, weekly: bool) -> None:
        try:
            status, ctype, body = _payload(self.server.roots, query,
                                           weekly=weekly)
        except ValueError as e:
            status, ctype, body = 400, "application/json", \
                json.dumps({"error": str(e)})
        except Exception as e:
            # the client learns nothing about git's command line or our
            # paths; whoever runs the bridge gets the detail
            self._log_error(e)
            status, ctype, body = 500, "application/json", \
                json.dumps({"error": INTERNAL_ERROR})
        self._send(status, ctype, body)

    def _log_error(self, e: Exception) -> None:
        """One line on stderr for a request that failed inside the bridge.
        The request line is the client's own text: it reaches the operator's
        terminal only with its control characters made visible."""
        detail = " ".join(f"{type(e).__name__}: {e}".split())
        print(stdio_mod.printable(
            f"error: {self.command} {self.path}: {detail}"), file=sys.stderr)

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


def _listen_error(e: Exception, host: str, port: int) -> str:
    if getattr(e, "errno", None) == errno.EADDRINUSE:
        why = ("the port is already in use (is `standupguy serve` already "
               "running? pick another with --port)")
    else:
        why = getattr(e, "strerror", None) or str(e)
    return f"cannot listen on {host}:{port}: {why}"


def serve(root, host: str = "127.0.0.1", port: int = 8765) -> int:
    """Serve one repo path or a list of them until Ctrl-C. Returns 2 (with
    a one-line error) when the address cannot be bound."""
    try:
        server = make_server(root, host, port)
    except (OSError, OverflowError) as e:   # in use, not ours, bad port ...
        print(f"error: {_listen_error(e, host, port)}", file=sys.stderr)
        return 2
    roots = server.roots
    shown = roots[0] if len(roots) == 1 else \
        f"{len(roots)} repos ({', '.join(roots)})"
    try:
        # inside the try: with nobody to read the banner (stdout is a closed
        # pipe) we leave, and give the port back on the way out
        print(f"standupguy serving {shown} on "
              f"http://{host}:{server.server_address[1]} "
              f"(/digest, /week, /health)", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
