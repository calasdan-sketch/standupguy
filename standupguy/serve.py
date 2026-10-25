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

Who may ask. The commit history is private (every author, e-mail address and
subject), so the bridge answers this computer only, and only a request that
names it:

  - It listens on 127.0.0.1 by default.
  - The Host header must be localhost, 127.0.0.1 or [::1] (or the --host
    address) on the bridge's own port. Any other name is a 403: a web page you
    open cannot read your history by pointing its own name at your computer
    (DNS rebinding). Add a name with --allow-host NAME (every port of that
    name) or --allow-host NAME:PORT (that port only). This works for this
    computer's own names too: a port forwarded to the bridge (docker -p
    9999:8765, ssh -L 9999:127.0.0.1:8765) arrives as localhost:9999, which
    --allow-host localhost:9999 permits. A Host with no port means port 80.
    HTTP/1.1 needs exactly one Host header (else 400); an old HTTP/1.0 client
    may omit it.
  - An Origin header (a browser's cross-site request) must name an allowed
    host too, and no CORS permission is ever granted.
  - --token TOKEN (or STANDUPGUY_TOKEN, 16 or more printable characters)
    makes every request except /health send `Authorization: Bearer TOKEN` (or
    ?token=TOKEN): without it, a 401.

To reach the bridge from another machine (Activepieces or n8n elsewhere, or in
Docker) it must listen on a network address AND have a token:

    STANDUPGUY_TOKEN=<random> standupguy serve --host 0.0.0.0

`serve` refuses to start on a non-loopback address without a token (exit 2)
unless --insecure-allow-lan is given, and then it warns on stderr. A caller
that uses a DNS name (host.docker.internal, devbox.lan) needs --allow-host;
a Host that is an IP address is accepted on a network bind.
"""

from __future__ import annotations

import errno
import hmac
import ipaddress
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote_plus

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

#: The names of this computer a request may use in its Host header.
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "[::1]")

# --host values that mean "every network interface".
_WILDCARD_HOSTS = ("", "0.0.0.0", "::")

#: A token is at least this many characters.
MIN_TOKEN_CHARS = 16

# host or [ipv6], optionally :port. Nothing else is a Host header.
_HOST_RE = re.compile(r"([A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::([0-9]{1,5}))?")


class ServeConfigError(ValueError):
    """`serve` was asked for something unsafe or invalid: a token that is too
    short, a network address without a token, a nonsense --allow-host. The
    message is one line for a person and never contains the token."""


def parse_host(value):
    """(name, port) of a Host header or an --allow-host entry: the name
    lower-cased and without one trailing dot, the port an int or None. None
    when the text is not a host[:port] (userinfo, a path, spaces ...)."""
    m = _HOST_RE.fullmatch(value or "")
    if not m:
        return None
    name, port = m.group(1).lower(), m.group(2)
    if not name.startswith("[") and name.endswith("."):
        name = name[:-1]
    if not name:
        return None
    if port is not None:
        port = int(port)
        if not 0 < port < 65536:
            return None
    return name, port


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name[1:-1] if name.startswith("[") else name)
    except ValueError:
        return False
    return True


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _check_token(token):
    """The token as bytes (or None for no token); ServeConfigError when it
    is too weak to guard a network address."""
    if token is None:
        return None
    if not (len(token) >= MIN_TOKEN_CHARS and token.isascii()
            and token.isprintable() and " " not in token):
        raise ServeConfigError(
            f"the token must be at least {MIN_TOKEN_CHARS} printable ASCII "
            "characters without spaces (make one with: python -c "
            "\"import secrets; print(secrets.token_urlsafe(24))\")")
    return token.encode("ascii")


def _check_allow_hosts(entries) -> list:
    parsed = []
    for entry in entries or ():
        item = parse_host(entry.strip()) if isinstance(entry, str) else None
        if item is None:
            raise ServeConfigError(
                f"--allow-host {entry!r} is not a host name (like "
                "devbox.lan or host.docker.internal:8765)")
        parsed.append(item)
    return parsed


class Guard:
    """Who may ask one server: the Host, Origin and token rules."""

    def __init__(self, port: int, bind_host: str, lan: bool, token, allowed):
        self.port = port
        self.lan = lan                  # listening beyond this computer
        self.token = token              # bytes, or None for no token
        self.names = set(LOOPBACK_NAMES)
        if bind_host.lower() not in _WILDCARD_HOSTS:
            bound = parse_host(bind_host)
            if bound and bound[1] is None:
                self.names.add(bound[0])
        self.any_port = {n for n, p in allowed if p is None}
        self.with_port = {(n, p) for n, p in allowed if p is not None}

    def host_ok(self, value) -> bool:
        parsed = parse_host(value)
        if parsed is None:
            return False
        name, port = parsed
        if port is None:
            port = 80                   # plain HTTP: no port in Host means 80
        if (name in self.names or (self.lan and _is_ip(name))) \
                and port == self.port:
            # our own names, and on a network bind any IP address (a page
            # cannot rebind an address, only a name): on OUR port
            return True
        # What --allow-host vouches for, this computer's own names included:
        # a port forwarded to the bridge arrives as localhost:<that port>.
        return name in self.any_port or (name, port) in self.with_port

    def origin_ok(self, value) -> bool:
        m = re.fullmatch(r"https?://([^/?#]+)", value or "", re.IGNORECASE)
        return bool(m) and self.host_ok(m.group(1))

    def token_ok(self, authorization, queried) -> bool:
        if self.token is None:
            return True
        given = list(queried)
        scheme, _, rest = (authorization or "").partition(" ")
        if scheme.lower() == "bearer":
            given.append(rest.strip())
        ok = False
        for candidate in given:         # every one is compared, in constant time
            ok = hmac.compare_digest(candidate.encode("utf-8", "replace"),
                                     self.token) or ok
        return ok


def _redact_token(target: str) -> str:
    """The request target with the value of every `token` parameter hidden:
    the query string may carry the token and the operator's log must not."""
    path, sep, query = target.partition("?")
    if not sep:
        return target
    items = []
    for item in query.split("&"):
        name, eq, _value = item.partition("=")
        if eq and unquote_plus(name).lower() == "token":
            item = f"{name}=***"
        items.append(item)
    return path + "?" + "&".join(items)


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
    guard: "Guard"           # who may ask (set by make_server)
    lan_without_token: bool  # listening beyond this computer, no token

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


def make_server(root, host: str = "127.0.0.1", port: int = 8765, *,
                token=None, allow_hosts=(),
                allow_lan: bool = False) -> ThreadedBridgeServer:
    """Build the configured bridge server (used by serve() and by tests).
    `root` is one repo path or a list of them. The roots live on the
    server, not on the Handler class, so two servers never share them.

    The address is bound first and only then judged: a bridge that would
    answer beyond this computer (any non-loopback address) needs a `token`,
    or `allow_lan=True`; otherwise ServeConfigError is raised and nothing
    ever listens. `allow_hosts` adds Host names (see Guard)."""
    secret = _check_token(token)
    allowed = _check_allow_hosts(allow_hosts)
    server = ThreadedBridgeServer((host, port), Handler,
                                  bind_and_activate=False)
    try:
        server.server_bind()
        address = server.server_address[0]
        lan = not _is_loopback(address)
        if lan and secret is None and not allow_lan:
            raise ServeConfigError(
                f"refusing to listen on {address}: anyone who can reach this "
                "port could read your commit history. Set a token "
                "(STANDUPGUY_TOKEN=<16+ random characters>, or --token) and "
                "send it as 'Authorization: Bearer <token>', or pass "
                "--insecure-allow-lan to serve without one")
        server.server_activate()
    except BaseException:
        server.server_close()
        raise
    server.roots = [root] if isinstance(root, str) else list(root)
    server.guard = Guard(server.server_address[1], host, lan, secret, allowed)
    server.lan_without_token = lan and secret is None
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
        in_progress = gitlog_mod.uncommitted_for(root, author)
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
    def _send(self, status: int, ctype: str, body: str,
              headers=()) -> None:
        data = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for name, value in headers:
                self.send_header(name, value)
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
            f"error: {self.command} {_redact_token(self.path)}: {detail}"),
            file=sys.stderr)

    def _refuse(self, status: int, message: str, why: str,
                headers=()) -> None:
        """Answer a request that may not be served: the client gets a JSON
        error, the operator one line on stderr. The line carries the path
        only - the query string may hold the token - and what the client
        sent comes out with its control characters made visible."""
        path = urlparse(self.path).path
        print(stdio_mod.printable(" ".join(
            f"refused: {self.command} {path}: {why}".split())),
            file=sys.stderr)
        self._send(status, "application/json",
                   json.dumps({"error": message}), headers)

    def _admit(self, path: str, query: dict) -> bool:
        """May this request be served? Host, then Origin, then the token."""
        guard = self.server.guard
        hosts = self.headers.get_all("Host") or []
        # HTTP/1.1 needs exactly one Host header. An old HTTP/1.0 client may
        # leave it out; a browser (the DNS-rebinding risk) never does.
        legacy = self.request_version in ("HTTP/0.9", "HTTP/1.0")
        if len(hosts) > 1 or (not hosts and not legacy):
            self._refuse(400, "exactly one Host header is required",
                         "no Host header, or more than one")
            return False
        if hosts and not guard.host_ok(hosts[0]):
            shown = stdio_mod.printable(hosts[0])[:100]
            self._refuse(
                403, f"host not allowed: {shown!r} (to permit it, start the "
                     "bridge with --allow-host NAME, or --allow-host "
                     "NAME:PORT for that port only)",
                f"host not allowed ({shown!r})")
            return False
        origins = self.headers.get_all("Origin") or []
        if origins and (len(origins) != 1 or not guard.origin_ok(origins[0])):
            shown = stdio_mod.printable(origins[0])[:100]
            self._refuse(403, "origin not allowed",
                         f"origin not allowed ({shown!r})")
            return False
        if path != "/health" and not guard.token_ok(
                self.headers.get("Authorization", ""),
                query.get("token", [])):
            self._refuse(
                401, "a token is required: send 'Authorization: Bearer "
                     "<token>' (or ?token=<token>)",
                "missing or wrong token",
                [("WWW-Authenticate", 'Bearer realm="standupguy"')])
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._admit(parsed.path, query):
            return
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


def serve(root, host: str = "127.0.0.1", port: int = 8765, *,
          token=None, allow_hosts=(), allow_lan: bool = False) -> int:
    """Serve one repo path or a list of them until Ctrl-C. Returns 2 (with
    a one-line error) when the address cannot be bound or the setup is not
    safe (see make_server)."""
    try:
        server = make_server(root, host, port, token=token,
                             allow_hosts=allow_hosts, allow_lan=allow_lan)
    except ServeConfigError as e:           # unsafe or invalid setup
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, OverflowError) as e:   # in use, not ours, bad port ...
        print(f"error: {_listen_error(e, host, port)}", file=sys.stderr)
        return 2
    roots = server.roots
    shown = roots[0] if len(roots) == 1 else \
        f"{len(roots)} repos ({', '.join(roots)})"
    if server.lan_without_token:
        print("warning: serving on a network address WITHOUT a token "
              "(--insecure-allow-lan): anyone who can reach this port can "
              f"read the commit history (authors, e-mail addresses, "
              f"messages) of {shown}", file=sys.stderr)
    try:
        # inside the try: with nobody to read the banner (stdout is a closed
        # pipe) we leave, and give the port back on the way out
        print(f"standupguy serving {shown} on "
              f"http://{host}:{server.server_address[1]} "
              f"(/digest, /week, /health)"
              + (" - token required" if server.guard.token else ""),
              flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
