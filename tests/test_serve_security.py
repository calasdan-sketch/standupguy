"""Review round 7: who may ask the HTTP bridge for the commit history.

S-1  `standupguy serve` hands the whole history (every author, e-mail address
     and commit subject) to anything that can reach it. It now answers only a
     request that names it: the Host header must be this computer's name
     (localhost, 127.0.0.1, [::1], or the --host address) on the bridge's own
     port, and an Origin header (a browser's cross-site request) must name an
     allowed host too, so a web page cannot read the history by pointing its
     own name at your computer (DNS rebinding). Optional --token on top.
     Listening on a non-loopback address WITHOUT a token is refused unless
     --insecure-allow-lan is given. The default stays 127.0.0.1.
"""

import contextlib
import http.client
import inspect
import io
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from unittest import mock

from test_standupguy import make_repo  # isolates git/HOME
from test_round5 import run_cli

from standupguy import cli as cli_mod
from standupguy import serve as serve_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "tok-" + "abcdefghij0123456789"      # 24 printable characters
# what make_repo() holds that a refused request must never see
LEAKS = ("t@example.com", "Teammate", "login flow", "update readme")
DIGEST = "/digest?all=1&since=2000-01-01&format=json"


def start(root, host="127.0.0.1", **kwargs):
    server = serve_mod.make_server(root, host, 0, **kwargs)
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    return server


def ask(server, path, headers=None):
    """(status, response, body) of one GET. http.client sends its own Host
    header unless `headers` has one, and the test replaces any header."""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                      timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def raw(server, data: bytes) -> bytes:
    """Send these exact bytes, return everything the server answers."""
    with socket.create_connection(("127.0.0.1", server.server_address[1]),
                                  timeout=10) as client:
        client.sendall(data)
        reply = b""
        while chunk := client.recv(4096):
            reply += chunk
    return reply


class Base(unittest.TestCase):
    def setUp(self):
        # a refused request is one line on stderr: keep the test run quiet
        # (a test that looks at it redirects stderr itself, inside this)
        quiet = mock.patch.object(sys, "stderr", io.StringIO())
        quiet.start()
        self.addCleanup(quiet.stop)

    def serve(self, host="127.0.0.1", **kwargs):
        server = start(self.repo, host, **kwargs)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        return server

    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()

    def assert_nothing_leaked(self, body):
        for leak in LEAKS:
            self.assertNotIn(leak, body)


# ------------------------------------------------------------------ Host


class TestHostCheck(Base):
    def setUp(self):
        super().setUp()
        self.server = self.serve()

    def test_another_name_gets_a_403_and_no_history(self):
        # the finding's repro: Host: evil.example used to get a 200 with every
        # author, e-mail address and subject
        status, _, body = ask(self.server, DIGEST, {"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assert_nothing_leaked(body)
        self.assertIn("error", json.loads(body))

    def test_a_rebound_name_with_the_right_port_is_refused_too(self):
        # DNS rebinding: the page's own name now points at 127.0.0.1
        status, _, body = ask(self.server, DIGEST,
                              {"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)
        self.assert_nothing_leaked(body)

    def test_every_endpoint_is_guarded(self):
        for path in ("/health", "/digest", "/week", "/nope"):
            with self.subTest(path):
                status, _, body = ask(self.server, path,
                                      {"Host": "evil.example"})
                self.assertEqual(status, 403)

    def test_this_computers_own_names_work(self):
        for host in (f"localhost:{self.port}", f"127.0.0.1:{self.port}",
                     f"[::1]:{self.port}", f"LocalHost:{self.port}",
                     f"localhost.:{self.port}"):
            with self.subTest(host):
                status, _, body = ask(self.server, DIGEST, {"Host": host})
                self.assertEqual(status, 200)
                self.assertIn("login flow", body)

    def test_the_wrong_port_or_no_port_is_refused(self):
        for host in (f"localhost:{self.port + 1}", "localhost",
                     "127.0.0.1", f"localhost:{self.port}0"):
            with self.subTest(host):
                status, _, body = ask(self.server, DIGEST, {"Host": host})
                self.assertEqual(status, 403)
                self.assert_nothing_leaked(body)

    def test_an_address_that_is_not_ours_is_refused_on_a_loopback_bind(self):
        status, _, _ = ask(self.server, DIGEST,
                           {"Host": f"192.168.5.5:{self.port}"})
        self.assertEqual(status, 403)

    def test_the_address_the_bridge_was_told_to_bind_is_a_name_of_its_own(self):
        # a loopback alias (127.0.0.2), or a name, as --host
        guard = serve_mod.Guard(8765, "127.0.0.2", False, None, [])
        self.assertTrue(guard.host_ok("127.0.0.2:8765"))
        self.assertFalse(guard.host_ok("127.0.0.3:8765"))
        self.assertFalse(guard.host_ok("127.0.0.2:9"))
        named = serve_mod.Guard(8765, "Devbox.LAN", True, b"x" * 16, [])
        self.assertTrue(named.host_ok("devbox.lan:8765"))
        self.assertFalse(named.host_ok("evil.example:8765"))
        # a wildcard bind names nothing but the loopback names
        wild = serve_mod.Guard(8765, "0.0.0.0", True, b"x" * 16, [])
        self.assertFalse(wild.host_ok("devbox.lan:8765"))
        self.assertTrue(wild.host_ok("localhost:8765"))

    def test_a_host_header_that_is_not_a_host_is_refused(self):
        for host in ("", " ", f"user@localhost:{self.port}",
                     f"localhost:{self.port}/x", f"localhost:{self.port} a.b",
                     "localhost:abc", "[::1", "localhost:-1",
                     "localhost:99999", f"localhost:{self.port}:1",
                     "local\xa0host"):
            with self.subTest(host):
                status, _, body = ask(self.server, DIGEST, {"Host": host})
                self.assertIn(status, (400, 403))
                self.assert_nothing_leaked(body)

    def test_http_1_1_without_a_host_is_a_400(self):
        reply = raw(self.server, b"GET /digest?all=1&since=2000-01-01 "
                                 b"HTTP/1.1\r\nConnection: close\r\n\r\n")
        self.assertIn(b" 400 ", reply.split(b"\r\n")[0])
        self.assert_nothing_leaked(reply.decode("utf-8", "replace"))

    def test_an_old_http_1_0_client_may_leave_the_host_out(self):
        # no browser does: the rebinding risk needs a name in the header
        reply = raw(self.server, b"GET /health HTTP/1.0\r\n\r\n")
        self.assertIn(b" 200 ", reply.split(b"\r\n")[0])
        # ...but a Host it does send is still checked
        reply = raw(self.server, b"GET /health HTTP/1.0\r\n"
                                 b"Host: evil.example\r\n\r\n")
        self.assertIn(b" 403 ", reply.split(b"\r\n")[0])

    def test_two_host_headers_are_refused(self):
        reply = raw(self.server, (
            f"GET /digest?all=1&since=2000-01-01 HTTP/1.1\r\n"
            f"Host: localhost:{self.port}\r\nHost: evil.example\r\n"
            f"Connection: close\r\n\r\n").encode())
        self.assertIn(b" 400 ", reply.split(b"\r\n")[0])
        self.assert_nothing_leaked(reply.decode("utf-8", "replace"))

    def test_the_refusal_is_logged_for_the_operator_without_the_query(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ask(self.server, "/digest?token=SECRET-in-the-query",
                {"Host": "evil.example"})
        line = err.getvalue().strip()
        self.assertEqual(len(line.splitlines()), 1, err.getvalue())
        self.assertIn("evil.example", line)
        self.assertIn("/digest", line)
        self.assertNotIn("SECRET", line)

    def test_what_a_client_sends_cannot_drive_the_operators_terminal(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            raw(self.server, b"GET /digest HTTP/1.0\r\n"
                             b"Host: \x1b[2Jevil\x07.example\r\n\r\n")
        self.assertNotIn("\x1b", err.getvalue())
        self.assertNotIn("\x07", err.getvalue())

    def test_a_good_request_logs_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, _, _ = ask(self.server, DIGEST)
        self.assertEqual(status, 200)
        self.assertEqual(err.getvalue(), "")


# ---------------------------------------------------------------- Origin


class TestOriginCheck(Base):
    def setUp(self):
        super().setUp()
        self.server = self.serve()

    def test_a_cross_site_origin_is_refused(self):
        for origin in ("https://evil.example", "http://evil.example",
                       f"http://evil.example:{self.port}", "null",
                       f"http://localhost:{self.port + 1}",
                       f"ftp://localhost:{self.port}", "localhost"):
            with self.subTest(origin):
                status, _, body = ask(self.server, DIGEST, {"Origin": origin})
                self.assertEqual(status, 403)
                self.assert_nothing_leaked(body)

    def test_the_servers_own_origin_is_fine(self):
        for origin in (f"http://localhost:{self.port}",
                       f"http://127.0.0.1:{self.port}"):
            with self.subTest(origin):
                status, _, _ = ask(self.server, DIGEST, {"Origin": origin})
                self.assertEqual(status, 200)

    def test_a_request_without_origin_is_the_normal_case(self):
        # curl, n8n, Activepieces: no Origin header at all
        self.assertEqual(ask(self.server, DIGEST)[0], 200)

    def test_cors_is_never_granted(self):
        for path, headers in ((DIGEST, {}), ("/health", {}),
                              (DIGEST, {"Host": "evil.example"}),
                              (DIGEST, {"Origin": "https://evil.example"})):
            _, resp, _ = ask(self.server, path, headers)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))


# ----------------------------------------------------------------- Token


class TestToken(Base):
    def setUp(self):
        super().setUp()
        self.server = self.serve(token=TOKEN)

    def test_no_token_is_a_401_and_no_history(self):
        status, resp, body = ask(self.server, DIGEST)
        self.assertEqual(status, 401)
        self.assertIn("Bearer", resp.getheader("WWW-Authenticate"))
        self.assert_nothing_leaked(body)
        self.assertNotIn(TOKEN, body)

    def test_a_wrong_token_is_a_401(self):
        for auth in (f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}", "Bearer ",
                     "Bearer", f"Basic {TOKEN}", TOKEN, "Bearer " + "y" * 24,
                     f"Bearer  {TOKEN} extra"):
            with self.subTest(auth):
                status, _, body = ask(self.server, DIGEST,
                                      {"Authorization": auth})
                self.assertEqual(status, 401)
                self.assert_nothing_leaked(body)

    def test_the_bearer_header_works(self):
        for scheme in ("Bearer", "bearer", "BEARER"):
            with self.subTest(scheme):
                status, _, body = ask(
                    self.server, DIGEST,
                    {"Authorization": f"{scheme} {TOKEN}"})
                self.assertEqual(status, 200)
                self.assertIn("login flow", body)

    def test_the_query_form_works(self):
        status, _, body = ask(self.server, DIGEST + f"&token={TOKEN}")
        self.assertEqual(status, 200)
        self.assertIn("login flow", body)

    def test_a_wrong_query_token_is_a_401(self):
        for token in ("", "nope", TOKEN + "x", TOKEN[:-1], "%FF%FE%00"):
            with self.subTest(token):
                status, _, body = ask(self.server, DIGEST + f"&token={token}")
                self.assertEqual(status, 401)
                self.assert_nothing_leaked(body)

    def test_health_needs_no_token(self):
        # liveness only: it says nothing about the repo
        status, _, body = ask(self.server, "/health")
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))

    def test_an_unknown_path_does_not_reveal_the_routes(self):
        self.assertEqual(ask(self.server, "/nope")[0], 401)
        self.assertEqual(
            ask(self.server, "/nope", {"Authorization": f"Bearer {TOKEN}"})[0],
            404)

    def test_the_host_is_checked_before_the_token(self):
        status, _, body = ask(self.server, DIGEST,
                              {"Host": "evil.example",
                               "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 403)
        self.assert_nothing_leaked(body)

    def test_the_origin_is_checked_before_the_token(self):
        status, _, _ = ask(self.server, DIGEST,
                           {"Origin": "https://evil.example",
                            "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 403)

    def test_the_token_never_reaches_the_operators_log(self):
        # a request that works, one that fails inside, one that is refused
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ask(self.server, DIGEST + f"&token={TOKEN}")
            ask(self.server, DIGEST + f"&token={TOKEN}",
                {"Host": "evil.example"})
            self.server.cache.clear()       # the first request was cached
            with mock.patch.object(serve_mod, "_payload",
                                   side_effect=RuntimeError("boom")):
                status, _, _ = ask(self.server, DIGEST + f"&token={TOKEN}")
        self.assertEqual(status, 500)
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertIn("boom", err.getvalue())      # the detail is still there

    def test_the_percent_encoded_name_is_redacted_too(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with mock.patch.object(serve_mod, "_payload",
                                   side_effect=RuntimeError("boom")):
                ask(self.server, DIGEST + f"&%74oken={TOKEN}")
        self.assertNotIn(TOKEN, err.getvalue())

    def test_a_request_that_works_is_unchanged(self):
        status, _, body = ask(self.server, DIGEST,
                              {"Authorization": f"Bearer {TOKEN}"})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["commits"]), 4)


class TestTokenRules(unittest.TestCase):
    def test_a_token_must_be_long_and_printable(self):
        for bad in ("", "short", "x" * 15, "has space in it 1234567",
                    "tab\tinside-1234567890", "caf\xe9-1234567890123",
                    "new\nline-12345678901"):
            with self.subTest(bad):
                with self.assertRaises(serve_mod.ServeConfigError):
                    serve_mod.make_server(make_repo(), "127.0.0.1", 0,
                                          token=bad)

    def test_the_refusal_does_not_echo_the_token(self):
        with self.assertRaises(serve_mod.ServeConfigError) as cm:
            serve_mod.make_server(make_repo(), "127.0.0.1", 0,
                                  token="Zq9 secret")
        self.assertNotIn("Zq9", str(cm.exception))
        self.assertIn("16", str(cm.exception))

    def test_sixteen_characters_is_enough(self):
        server = serve_mod.make_server(make_repo(), "127.0.0.1", 0,
                                       token="a" * 16)
        server.server_close()


# ------------------------------------------------------------ --allow-host


class TestAllowHost(Base):
    def test_a_name_is_refused_until_it_is_allowed(self):
        server = self.serve()
        host = f"host.docker.internal:{self.port}"
        self.assertEqual(ask(server, DIGEST, {"Host": host})[0], 403)
        server = self.serve(allow_hosts=["host.docker.internal"])
        self.assertEqual(ask(server, DIGEST, {"Host": host})[0], 200)

    def test_a_bare_name_matches_any_port_and_name_port_only_that_one(self):
        server = self.serve(allow_hosts=["a.example", "b.example:4000"])
        for host, want in (("a.example", 200), ("A.Example:1234", 200),
                           ("b.example:4000", 200), ("b.example:4001", 403),
                           ("b.example", 403), ("c.example", 403)):
            with self.subTest(host):
                self.assertEqual(
                    ask(server, DIGEST, {"Host": host})[0], want)

    def test_an_allowed_name_is_an_allowed_origin_too(self):
        server = self.serve(allow_hosts=["a.example"])
        status, _, _ = ask(server, DIGEST, {"Host": "a.example",
                                            "Origin": "http://a.example"})
        self.assertEqual(status, 200)

    def test_nonsense_entries_are_refused_at_start(self):
        for bad in ("", "a/b", "http://a.example", "a b", "a.example:",
                    "a.example:99999", "*", "@a"):
            with self.subTest(bad):
                with self.assertRaises(serve_mod.ServeConfigError):
                    serve_mod.make_server(make_repo(), "127.0.0.1", 0,
                                          allow_hosts=[bad])


class TestAllowHostForThisComputersOwnNames(Base):
    """Docker's `-p 9999:8765` and `ssh -L 9999:127.0.0.1:8765` reach the
    bridge as localhost:9999: this computer's name, but not the bridge's own
    port. That is refused until the user allows it, and --allow-host, which
    the refusal points to, has to be the way (round-2 panel finding)."""

    def code(self, server, host, **more):
        return ask(server, DIGEST, {"Host": host, **more})[0]

    def test_another_port_of_this_computer_is_refused_by_default(self):
        server = self.serve()
        for host in ("localhost:9999", "127.0.0.1:9999", "[::1]:9999",
                     "localhost"):
            with self.subTest(host):
                self.assertEqual(self.code(server, host), 403)

    def test_name_and_port_allow_that_name_on_that_port_only(self):
        server = self.serve(allow_hosts=["localhost:9999"])
        for host, want in (("localhost:9999", 200), ("LocalHost:9999", 200),
                           ("localhost.:9999", 200),
                           (f"localhost:{self.port}", 200),   # its own, still
                           ("localhost:9998", 403), ("localhost", 403),
                           ("127.0.0.1:9999", 403), ("[::1]:9999", 403)):
            with self.subTest(host):
                self.assertEqual(self.code(server, host), want)

    def test_a_bare_name_allows_every_port_of_that_name(self):
        server = self.serve(allow_hosts=["localhost"])
        for host, want in (("localhost", 200), ("localhost:9999", 200),
                           ("localhost:1", 200),
                           (f"localhost:{self.port}", 200),
                           ("127.0.0.1:9999", 403), ("[::1]:9999", 403)):
            with self.subTest(host):
                self.assertEqual(self.code(server, host), want)

    def test_every_spelling_of_this_computer_can_be_allowed(self):
        for entry, host, other in (
                ("127.0.0.1:9999", "127.0.0.1:9999", "localhost:9999"),
                ("[::1]:9999", "[::1]:9999", "127.0.0.1:9999")):
            with self.subTest(entry):
                server = self.serve(allow_hosts=[entry])
                self.assertEqual(self.code(server, host), 200)
                self.assertEqual(self.code(server, other), 403)

    def test_a_host_without_a_port_means_port_80(self):
        server = self.serve(allow_hosts=["localhost:80"])
        self.assertEqual(self.code(server, "localhost"), 200)
        self.assertEqual(self.code(server, "localhost:80"), 200)
        self.assertEqual(self.code(server, "localhost:8080"), 403)

    def test_the_origin_rule_does_not_relax(self):
        server = self.serve(allow_hosts=["localhost:9999"])
        host = "localhost:9999"
        self.assertEqual(self.code(server, host, Origin="http://localhost:9999"),
                         200)
        for origin in ("https://evil.example", "http://localhost:9998",
                       "http://evil.example:9999", "null"):
            with self.subTest(origin):
                self.assertEqual(self.code(server, host, Origin=origin), 403)

    def test_the_token_rule_does_not_relax(self):
        server = self.serve(allow_hosts=["localhost:9999"], token=TOKEN)
        self.assertEqual(self.code(server, "localhost:9999"), 401)
        self.assertEqual(self.code(server, "localhost:9999",
                                   Authorization="Bearer wrong-wrong-wrong-wrong"),
                         401)
        self.assertEqual(self.code(server, "localhost:9999",
                                   Authorization=f"Bearer {TOKEN}"), 200)

    def test_an_allowed_port_of_this_computer_leaks_nothing_elsewhere(self):
        server = self.serve(allow_hosts=["localhost:9999"])
        status, _, body = ask(server, DIGEST, {"Host": "evil.example:9999"})
        self.assertEqual(status, 403)
        self.assert_nothing_leaked(body)

    def test_on_a_network_bind_an_ip_address_can_be_allowed_on_another_port(self):
        token = TOKEN.encode()
        guard = serve_mod.Guard(8765, "0.0.0.0", True, token,
                                [("192.168.1.5", 9999)])
        self.assertTrue(guard.host_ok("192.168.1.5:9999"))
        self.assertTrue(guard.host_ok("192.168.1.9:8765"))   # any address, our port
        self.assertFalse(guard.host_ok("192.168.1.9:9999"))
        self.assertFalse(guard.host_ok("192.168.1.5:9998"))

    def test_the_refusal_names_the_way_out_and_it_works(self):
        server = self.serve()
        status, _, body = ask(server, DIGEST, {"Host": "localhost:9999"})
        self.assertEqual(status, 403)
        message = json.loads(body)["error"]
        self.assertIn("--allow-host", message)
        self.assertIn("NAME:PORT", message)      # the port-pinning form
        self.assertIn("localhost:9999", message)
        # ...and taking the advice, literally, opens exactly that door
        again = self.serve(allow_hosts=["localhost:9999"])
        self.assertEqual(self.code(again, "localhost:9999"), 200)

    def test_the_refusal_never_suggests_a_door_it_cannot_open(self):
        # nonsense in the Host header gets the advice without an entry to copy
        server = self.serve()
        for host in ("user@localhost:9999", "a b", "localhost:99999"):
            with self.subTest(host):
                status, _, body = ask(server, DIGEST, {"Host": host})
                self.assertIn(status, (400, 403))
                if status == 403:
                    self.assertNotIn(f"--allow-host {host}",
                                     json.loads(body)["error"])


class TestDuplicateHeaders(Base):
    """A browser sends one Host and one Origin. A request with two of either is
    refused whatever they say and in whatever order; of two Authorization
    headers only the first counts (round-2 panel: the second Origin was
    unpinned, so reading only the first passed every test)."""

    def setUp(self):
        super().setUp()
        self.server = self.serve(token=TOKEN)
        self.host = f"Host: localhost:{self.port}"
        self.origin = f"Origin: http://localhost:{self.port}"
        self.evil_host = "Host: evil.example"
        self.evil_origin = "Origin: https://evil.example"
        self.auth = f"Authorization: Bearer {TOKEN}"

    def send(self, *headers):
        request = "\r\n".join([f"GET {DIGEST} HTTP/1.1", *headers,
                               "Connection: close", "", ""])
        reply = raw(self.server, request.encode()).decode("utf-8", "replace")
        return int(reply.split(" ", 2)[1]), reply

    def test_the_request_the_cases_below_start_from_works(self):
        status, reply = self.send(self.host, self.origin, self.auth)
        self.assertEqual(status, 200)
        self.assertIn("login flow", reply)

    def test_two_host_headers_are_a_400_whatever_they_say(self):
        for pair in ((self.host, self.evil_host), (self.evil_host, self.host),
                     (self.host, self.host)):
            with self.subTest(pair):
                status, reply = self.send(*pair, self.auth)
                self.assertEqual(status, 400)
                self.assert_nothing_leaked(reply)

    def test_two_origin_headers_are_a_403_whatever_they_say(self):
        for pair in ((self.origin, self.evil_origin),
                     (self.evil_origin, self.origin),
                     (self.origin, self.origin),
                     (self.evil_origin, self.evil_origin)):
            with self.subTest(pair):
                status, reply = self.send(self.host, *pair, self.auth)
                self.assertEqual(status, 403)
                self.assertIn("origin not allowed", reply)
                self.assert_nothing_leaked(reply)

    def test_the_first_authorization_header_is_the_one_that_counts(self):
        wrong = "Authorization: Bearer " + "y" * 24
        for pair, want in (((self.auth, wrong), 200), ((wrong, self.auth), 401),
                           ((wrong, wrong), 401)):
            with self.subTest(pair):
                status, reply = self.send(self.host, *pair)
                self.assertEqual(status, want)
                if want == 401:
                    self.assert_nothing_leaked(reply)


# --------------------------------------------------------------- exposure


class NeverListens:
    """Fails the test if a socket was put into listening state."""

    def __enter__(self):
        self.patch = mock.patch.object(
            serve_mod.ThreadedBridgeServer, "server_activate",
            side_effect=AssertionError("listen() was called"))
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class TestExposure(Base):
    def test_a_non_loopback_bind_without_a_token_is_refused(self):
        for host in ("0.0.0.0", "", "::"):
            with self.subTest(host), NeverListens():
                try:
                    serve_mod.make_server(self.repo, host, 0)
                except serve_mod.ServeConfigError as e:
                    msg = str(e)
                    self.assertIn("--token", msg)
                    self.assertIn("--insecure-allow-lan", msg)
                except OSError:
                    if host == "::":
                        continue       # no IPv6 here: nothing was exposed
                    raise
                else:
                    self.fail("served the history to the network")

    def test_the_loopback_names_need_no_token(self):
        for host in ("127.0.0.1", "localhost", "127.0.0.2"):
            with self.subTest(host):
                server = serve_mod.make_server(self.repo, host, 0)
                server.server_close()

    def test_a_token_makes_a_wildcard_bind_acceptable(self):
        server = self.serve("0.0.0.0", token=TOKEN)
        port = server.server_address[1]
        auth = {"Authorization": f"Bearer {TOKEN}"}
        # an IP address as the Host: fine (no DNS name for a page to rebind)
        for host in (f"127.0.0.1:{port}", f"192.168.5.5:{port}",
                     f"[fe80::1]:{port}"):
            with self.subTest(host):
                self.assertEqual(
                    ask(server, DIGEST, {**auth, "Host": host})[0], 200)
        # a DNS name has to be allowed first
        self.assertEqual(
            ask(server, DIGEST, {**auth, "Host": f"devbox.lan:{port}"})[0],
            403)
        self.assertEqual(
            ask(server, DIGEST, {**auth, "Host": f"127.0.0.1:{port + 1}"})[0],
            403)
        # and still no token, no history
        self.assertEqual(ask(server, DIGEST)[0], 401)

    def test_a_dns_name_can_be_allowed_on_a_wildcard_bind(self):
        server = self.serve("0.0.0.0", token=TOKEN,
                            allow_hosts=["devbox.lan"])
        port = server.server_address[1]
        status, _, _ = ask(server, DIGEST,
                           {"Authorization": f"Bearer {TOKEN}",
                            "Host": f"devbox.lan:{port}"})
        self.assertEqual(status, 200)

    def test_the_lan_flag_allows_it_without_a_token(self):
        server = self.serve("0.0.0.0", allow_lan=True)
        self.assertEqual(ask(server, DIGEST)[0], 200)
        self.assertTrue(server.lan_without_token)

    def test_the_default_is_this_computer_only(self):
        # a mutation to 0.0.0.0 used to pass every serve test
        self.assertEqual(
            inspect.signature(serve_mod.make_server).parameters["host"].default,
            "127.0.0.1")
        self.assertEqual(
            inspect.signature(serve_mod.serve).parameters["host"].default,
            "127.0.0.1")
        args = cli_mod.build_parser().parse_args(["serve"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertIsNone(args.token)
        self.assertEqual(args.allow_host or [], [])
        self.assertFalse(args.insecure_allow_lan)
        # ...and what a plain `standupguy serve` really binds
        bound = []

        def record(server, *a, **k):
            bound.append(server.server_address[0])

        with mock.patch.object(serve_mod.ThreadedBridgeServer,
                               "serve_forever", record), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli_mod.main(["--root", self.repo, "serve", "--port", "0"])
        self.assertEqual((code, bound), (0, ["127.0.0.1"]))


class TestServeCommandLine(Base):
    def run_serve(self, *argv, env=None):
        """(code, stdout, stderr) of `standupguy serve ...` with the server
        itself stubbed out; also what the server was built with."""
        built = []
        real = serve_mod.make_server

        def spy(*args, **kwargs):
            built.append(real(*args, **kwargs))
            return built[-1]

        out, err = io.StringIO(), io.StringIO()
        clean = {k: v for k, v in os.environ.items()
                 if k != "STANDUPGUY_TOKEN"}
        clean.update(env or {})
        with mock.patch.dict(os.environ, clean, clear=True), \
                mock.patch.object(serve_mod, "make_server", spy), \
                mock.patch.object(serve_mod.ThreadedBridgeServer,
                                  "serve_forever"), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli_mod.main(["--root", self.repo, "serve", "--port", "0",
                                 *argv])
        return code, out.getvalue(), err.getvalue(), built

    def test_a_lan_bind_without_a_token_exits_2_with_the_way_out(self):
        code, out, err, built = self.run_serve("--host", "0.0.0.0")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")                       # no banner
        self.assertTrue(err.startswith("error: "), err)
        for word in ("--token", "STANDUPGUY_TOKEN", "--insecure-allow-lan"):
            self.assertIn(word, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(built, [])

    def test_a_token_from_the_flag_or_the_environment_is_enough(self):
        for argv, env in ((("--token", TOKEN), {}),
                          ((), {"STANDUPGUY_TOKEN": TOKEN})):
            with self.subTest(argv, env=bool(env)):
                code, out, err, built = self.run_serve(
                    "--host", "0.0.0.0", *argv, env=env)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("token", out)
                self.assertNotIn(TOKEN, out)            # never printed
                self.assertEqual(built[0].guard.token, TOKEN.encode())

    def test_the_flag_wins_over_the_environment(self):
        other = "e" * 24
        code, _, _, built = self.run_serve(
            "--token", TOKEN, env={"STANDUPGUY_TOKEN": other})
        self.assertEqual(built[0].guard.token, TOKEN.encode())

    def test_an_empty_environment_variable_means_no_token(self):
        code, _, err, built = self.run_serve(env={"STANDUPGUY_TOKEN": ""})
        self.assertEqual((code, err), (0, ""))
        self.assertIsNone(built[0].guard.token)

    def test_a_bad_token_is_a_one_line_error_that_does_not_echo_it(self):
        for bad in ("", "Zq9x!"):
            with self.subTest(bad):
                code, out, err, built = self.run_serve("--token", bad)
                self.assertEqual(code, 2)
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertIn("16", err)
                self.assertNotIn("Zq9x", err)

    def test_the_lan_flag_warns_loudly_on_stderr(self):
        code, out, err, built = self.run_serve(
            "--host", "0.0.0.0", "--insecure-allow-lan")
        self.assertEqual(code, 0)
        self.assertIn("warning", err)
        self.assertIn("WITHOUT a token", err)
        self.assertTrue(built[0].lan_without_token)

    def test_allow_host_is_repeatable(self):
        code, _, err, built = self.run_serve(
            "--allow-host", "a.example", "--allow-host", "b.example:80")
        self.assertEqual((code, err), (0, ""))
        guard = built[0].guard
        self.assertTrue(guard.host_ok("a.example:1"))
        self.assertTrue(guard.host_ok("b.example:80"))

    def test_a_bad_allow_host_is_a_one_line_error(self):
        code, out, err, built = self.run_serve("--allow-host", "a/b")
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("--allow-host", err)

    def test_help_documents_the_new_flags(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli_mod.main(["--help"])
        for flag in ("--token", "--allow-host", "--insecure-allow-lan"):
            self.assertIn(flag, out.getvalue())
        self.assertIn("STANDUPGUY_TOKEN", out.getvalue())


class TestInARealProcess(Base):
    def env(self):
        # the sandbox sets PYTHONUNBUFFERED, which hides flush mistakes
        return {k: v for k, v in os.environ.items()
                if k not in ("PYTHONUNBUFFERED", "STANDUPGUY_TOKEN")}

    def test_the_refusal_exits_2_before_anything_listens(self):
        proc = subprocess.run(
            [sys.executable, "-m", "standupguy", "--root", self.repo,
             "serve", "--host", "0.0.0.0", "--port", "0"],
            capture_output=True, cwd=REPO, timeout=60, env=self.env())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, b"")
        err = proc.stderr.decode("utf-8", "replace")
        self.assertIn("--token", err)
        self.assertNotIn("Traceback", err)

    def test_a_token_server_end_to_end(self):
        env = self.env()
        env["STANDUPGUY_TOKEN"] = TOKEN
        proc = subprocess.Popen(
            [sys.executable, "-m", "standupguy", "--root", self.repo,
             "serve", "--port", "0"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=REPO, env=env)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        timer = threading.Timer(30, proc.kill)
        timer.start()
        self.addCleanup(timer.cancel)
        banner = proc.stdout.readline().decode("utf-8", "replace")
        self.assertIn("http://127.0.0.1:", banner, banner)
        self.assertNotIn(TOKEN, banner)
        port = int(banner.split("http://127.0.0.1:")[1].split()[0])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        for headers, want in (({}, 401),
                              ({"Authorization": f"Bearer {TOKEN}"}, 200),
                              ({"Authorization": f"Bearer {TOKEN}",
                                "Host": "evil.example"}, 403)):
            conn.request("GET", DIGEST, headers=headers)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")
            self.assertEqual(resp.status, want, body)
            if want != 200:
                self.assert_nothing_leaked(body)
            conn.close()


class TestWorkflowAdvice(unittest.TestCase):
    PATH = os.path.join(REPO, "workflows", "activepieces-standup-digest.json")

    def setUp(self):
        with open(self.PATH, encoding="utf-8") as f:
            self.wf = json.load(f)
        self.notes = self.wf["setup_notes"]

    def test_the_remote_advice_asks_for_a_token(self):
        # note 2 used to say "serve with --host 0.0.0.0" and nothing else
        remote = [n for n in self.notes if "0.0.0.0" in n]
        self.assertTrue(remote)
        for note in remote:
            self.assertIn("STANDUPGUY_TOKEN", note)
            self.assertIn("Authorization: Bearer", note)

    def test_the_docker_name_needs_allowing(self):
        text = " ".join(self.notes)
        self.assertIn("--allow-host", text)

    def test_the_http_step_has_the_header_slot(self):
        http_step = next(s for s in self.wf["steps"] if "HTTP" in s["piece"])
        header = http_step["config"]["headers"]["Authorization"]
        self.assertTrue(header.startswith("Bearer "))
        self.assertIn("token", header.lower())


class TestReadmeSaysIt(unittest.TestCase):
    def test_the_readme_documents_who_may_ask(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            text = f.read()
        for word in ("--token", "STANDUPGUY_TOKEN", "--allow-host",
                     "--insecure-allow-lan", "Host", "403", "401"):
            self.assertTrue(word in text, f"the README never says {word}")
        # the advice to bind every interface never comes without the token
        for para in text.split("\n\n"):
            if "--host 0.0.0.0" in para:
                self.assertTrue("TOKEN" in para, "0.0.0.0 without a token: "
                                + para[:80])

    def test_the_readme_shows_the_forwarded_port_examples(self):
        # the refusal points to --allow-host, so the README has to show the
        # two ways a forwarded port reaches the bridge and what to allow
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            flat = " ".join(f.read().split())
        for phrase in ("-p 9999:8765", "ssh -L 9999:127.0.0.1:8765",
                       "--allow-host localhost:9999",
                       "every port", "that port only", "port 80"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")
        helptext = " ".join(cli_mod.build_parser().format_help().split())
        for phrase in ("localhost", "NAME:PORT", "-p 9999:8765"):
            self.assertTrue(phrase in helptext, f"--help never says {phrase!r}")


if __name__ == "__main__":
    unittest.main()
