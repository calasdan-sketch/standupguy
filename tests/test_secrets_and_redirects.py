"""--draft and --push-slack never send the key where it was not meant, and no
error message ever repeats a secret.

Panel findings:
SG-2  urllib forwarded `Authorization: Bearer <key>` on a 302 to another
      origin: a fake endpoint answering 302 to a second localhost port got the
      key. Now a redirect to another origin is not followed at all.
SG-3  `AUTOSTANDUP_API_KEY=$'sk-test-0000\\n' standupguy --draft` printed
      `Invalid header value b'Bearer sk-test-0000\\n'`, and a Slack webhook
      with a stray space ended in a traceback quoting the token path. Now the
      messages name the problem and never the value; URL credentials and query
      strings are not shown either.
"""

import http.client
import io
import json
import os
import socket
import sys
import threading
import traceback
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from test_standupguy import make_repo  # isolates git/HOME
from test_round5 import run_cli

from standupguy import draft as draft_mod
from standupguy import push as push_mod

KEY = "sk-test-NEVER-REPEAT-ME-0451"
CURLY = chr(0x2019)
EMOJI = chr(0x1F331)
ESC = chr(27)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def traceback_text(exc):
    """What an uncaught `exc` would print, the chain of earlier errors included."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def completion(text="Yesterday:\n- did things"):
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def answer(h, status=200, body=b"", headers=()):
    h.send_response(status)
    for name, value in headers:
        h.send_header(name, value)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def answers_with(text="Yesterday:\n- did things"):
    return lambda h: answer(h, 200, completion(text),
                            [("Content-Type", "application/json")])


def redirects_to(location, code=302):
    return lambda h: answer(h, code, b"", [("Location", location)])


class Fake:
    """A fake HTTP service on 127.0.0.1: records every request it gets and
    answers as `responder(handler)` says."""

    def __init__(self, responder=None):
        self.requests = []
        self.responder = responder or answers_with()
        outer = self

        class H(BaseHTTPRequestHandler):
            def _any(h):
                length = int(h.headers.get("Content-Length") or 0)
                outer.requests.append({
                    "method": h.command, "path": h.path,
                    "headers": {k.lower(): v for k, v in h.headers.items()},
                    "body": h.rfile.read(length) if length else b""})
                outer.responder(h)

            do_GET = do_POST = _any

            def log_message(h, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *a, **k: None
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.url = self.origin + "/v1"
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def asks(base_url, **kw):
    kw.setdefault("api_key", KEY)
    kw.setdefault("model", "m")
    kw.setdefault("timeout", 5)
    return draft_mod.draft("digest", base_url=base_url, **kw)


class TestTheKeyStaysWithTheEndpoint(unittest.TestCase):
    def pair(self, code, target_path="/steal?token=abc"):
        target = Fake()
        origin = Fake(redirects_to(target.origin + target_path, code))
        self.addCleanup(target.stop)
        self.addCleanup(origin.stop)
        return origin, target

    def test_a_redirect_to_another_origin_is_not_followed(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                origin, target = self.pair(code)
                with self.assertRaises(draft_mod.DraftError) as cm:
                    asks(origin.url)
                self.assertEqual(target.requests, [],
                                 "something was sent to the other server")
                self.assertEqual(len(origin.requests), 1)
                msg = str(cm.exception)
                if code == 308 and sys.version_info < (3, 11):
                    # urllib before 3.11 does not follow a 308 at all, so
                    # there is nothing for us to refuse; the target got nothing
                    self.assertNotIn(KEY, msg)
                    continue
                self.assertIn(f"127.0.0.1:{target.port}", msg)
                self.assertIn("redirect", msg)
                self.assertNotIn(KEY, msg)
                self.assertNotIn("\n", msg)
                # only where it points, not the path or the query
                self.assertNotIn("steal", msg)
                self.assertNotIn("token=abc", msg)

    def test_a_different_port_or_host_or_scheme_is_another_origin(self):
        same = draft_mod._same_origin
        base = "http://localhost:11434/v1/chat/completions"
        self.assertTrue(same(base, "http://localhost:11434/other?x=1"))
        self.assertTrue(same(base, "HTTP://LOCALHOST:11434/"))
        self.assertTrue(same("http://example.test/a", "http://example.test:80/b"))
        self.assertTrue(same("https://example.test/a", "https://example.test:443/b"))
        self.assertFalse(same(base, "http://localhost:11435/v1"))
        self.assertFalse(same(base, "http://127.0.0.1:11434/v1"))
        self.assertFalse(same(base, "https://localhost:11434/v1"))
        self.assertFalse(same("https://example.test/a", "http://example.test:443/a"))
        self.assertFalse(same("http://example.test/a", "https://example.test:80/a"))
        self.assertFalse(same("https://example.test/a", "http://example.test/a"))

    def test_the_answer_that_is_refused_is_closed(self):
        class Answer:
            closed = False

            def close(self):
                self.closed = True

        handler = draft_mod._StayOnOrigin()
        req = urllib.request.Request("http://a.test/v1/chat/completions",
                                     data=b"x")
        fp = Answer()
        with self.assertRaises(draft_mod._Redirected) as cm:
            handler.redirect_request(req, fp, 302, "Found", {}, "http://b.test/")
        self.assertEqual(cm.exception.where, "http://b.test/")
        self.assertTrue(fp.closed, "the refused answer was left open")
        # one it follows is urllib's to close
        fp = Answer()
        handler.redirect_request(req, fp, 302, "Found", {}, "http://a.test/other")
        self.assertFalse(fp.closed)

    def test_a_redirect_inside_the_same_origin_is_still_followed(self):
        origin = Fake()
        self.addCleanup(origin.stop)

        def responder(h):
            if h.command == "POST":
                redirects_to(origin.origin + "/v1/answer")(h)
            else:
                answers_with("Yesterday:\n- followed")(h)

        origin.responder = responder
        self.assertEqual(asks(origin.url), "Yesterday:\n- followed")
        # the key went to the same server both times, as it always did
        self.assertEqual([r["headers"].get("authorization")
                          for r in origin.requests], [f"Bearer {KEY}"] * 2)

    def test_the_command_line(self):
        origin, target = self.pair(302)
        with mock.patch.dict(os.environ, {"AUTOSTANDUP_API_KEY": KEY}):
            code, out, err = run_cli("--root", make_repo(), "--since",
                                     "2026-10-14", "--draft", "--base-url",
                                     origin.url)
        self.assertEqual(code, 2)
        self.assertEqual(target.requests, [])
        self.assertTrue(err.startswith("error: "), err)
        self.assertNotIn(KEY, err)
        self.assertEqual(len(err.strip().splitlines()), 2, err)  # error + note
        self.assertIn("add login flow", out)                     # digest still shown


class TestAnErrorReplyIsLetGo(unittest.TestCase):
    """The reply an HTTP error carries is an open connection. Whoever reports
    the error lets go of it there and then. Left to the garbage collector it
    ends as a ResourceWarning in whatever stderr is being captured by then (a
    test flaked on Python 3.10 that way), and a long-running `mcp` or `serve`
    keeps the socket until the collector runs."""

    def test_a_failed_draft_closes_it(self):
        reply = io.BytesIO(b'{"error": "no"}')
        error = urllib.error.HTTPError("http://a.test/v1", 500, "boom", {}, reply)

        def fail(self, req, *a, **k):
            raise error

        with mock.patch.object(urllib.request.OpenerDirector, "open", fail):
            with self.assertRaises(draft_mod.DraftError):
                asks("http://127.0.0.1:1/v1")
        self.assertTrue(reply.closed)

    def test_a_failed_push_closes_it(self):
        reply = io.BytesIO(b"no_service")
        error = urllib.error.HTTPError("https://hooks.slack.com/services/T0/B0/X",
                                       404, "gone", {}, reply)

        def fake_urlopen(req, timeout=30):      # no network in a test
            raise error

        with mock.patch.object(push_mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(push_mod.PushError):
                push_mod.push_slack("https://hooks.slack.com/services/T0/B0/X",
                                    "hi")
        self.assertTrue(reply.closed)

    def test_every_reply_a_real_server_gave_is_closed(self):
        seen, kept = [], []
        real_init = http.client.HTTPResponse.__init__

        def record(self, *a, **k):
            seen.append(self)
            real_init(self, *a, **k)

        # 308: urllib before 3.11 has no handler for it and raises it as an error
        for status in (500, 404, 308):
            server = Fake(lambda h, s=status: answer(h, s, b"{}"))
            self.addCleanup(server.stop)
            with mock.patch.object(http.client.HTTPResponse, "__init__", record):
                with self.assertRaises(draft_mod.DraftError) as cm:
                    asks(server.url)
            kept.append(cm.exception)   # as a caller that keeps the error would
        self.assertEqual(len(seen), 3)
        self.assertEqual([r.status for r in seen if not r.isclosed()], [])


class TestNoSecretIsEverEchoed(unittest.TestCase):
    BAD_KEYS = {
        "a trailing line feed": "sk-test-0000\n",
        "a line feed inside": "sk-test-0000\nX-Evil: 1",
        "a carriage return": "sk-test\rX",
        "a NUL": "sk-\x00test",
        "an escape": "sk-" + ESC + "[2Jtest",
        "a tab": "sk-\ttest",
        "a curly quote": "sk-" + CURLY + "test-0000",
        "an emoji": "sk-" + EMOJI + "-0000",
    }

    def test_nothing_is_sent_for_such_a_key(self):
        for label, key in self.BAD_KEYS.items():
            server = Fake()
            self.addCleanup(server.stop)
            with self.subTest(label):
                with self.assertRaises(draft_mod.DraftError) as cm:
                    asks(server.url, api_key=key)
                msg = str(cm.exception)
                self.assertEqual(server.requests, [], "something was sent")
                self.assertNotIn(key.strip(), msg)
                self.assertNotIn("sk-test", msg)
                self.assertNotIn("Invalid header value", msg)
                self.assertNotIn("\n", msg)
                self.assertTrue(msg.isprintable(), ascii(msg))
                self.assertIn("API key", msg)

    def test_the_reason_is_named(self):
        server = Fake()
        self.addCleanup(server.stop)
        with self.assertRaises(draft_mod.DraftError) as cm:
            asks(server.url, api_key="sk-" + CURLY + "x")
        self.assertIn("Latin-1", str(cm.exception))
        with self.assertRaises(draft_mod.DraftError) as cm:
            asks(server.url, api_key="sk-x\n")
        self.assertIn("not printable", str(cm.exception))

    def test_an_ordinary_key_still_works(self):
        server = Fake()
        self.addCleanup(server.stop)
        self.assertEqual(asks(server.url, api_key="caf" + chr(0xE9) + "-key-1"),
                         "Yesterday:\n- did things")
        self.assertEqual(server.requests[0]["headers"]["authorization"],
                         "Bearer caf" + chr(0xE9) + "-key-1")

    def test_the_environment_variable_too_on_the_command_line(self):
        server = Fake()
        self.addCleanup(server.stop)
        with mock.patch.dict(os.environ, {"AUTOSTANDUP_API_KEY": "sk-test-0000\n"}):
            code, out, err = run_cli("--root", make_repo(), "--since",
                                     "2026-10-14", "--draft", "--base-url",
                                     server.url)
        self.assertEqual(code, 2)
        self.assertEqual(server.requests, [])
        self.assertNotIn("sk-test-0000", err)
        self.assertNotIn("Invalid header value", err)
        self.assertIn("add login flow", out)

    def test_whatever_else_the_http_library_refuses_is_not_quoted(self):
        # the safety net behind the check above
        def refuse(self, req, *a, **k):
            raise ValueError(f"Invalid header value b'Bearer {KEY}'")

        server = Fake()
        self.addCleanup(server.stop)
        with mock.patch.object(urllib.request.OpenerDirector, "open", refuse):
            with self.assertRaises(draft_mod.DraftError) as cm:
                asks(server.url)
        self.assertNotIn(KEY, str(cm.exception))
        self.assertNotIn("Invalid header value", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)

    def test_what_the_library_said_is_one_short_printable_line(self):
        noisy = "reset" + ESC + "[2J by\npeer " + "x" * 400
        failures = {
            "an unreachable host": urllib.error.URLError(noisy),
            "a hang-up": http.client.HTTPException(noisy),
            "a reset": ConnectionResetError(noisy),
            "an http error": urllib.error.HTTPError(
                "http://a.test/v1", 500, noisy, {}, None),
        }
        for label, exc in failures.items():
            def fail(self, req, *a, _exc=exc, **k):
                raise _exc

            with self.subTest(label), mock.patch.object(
                    urllib.request.OpenerDirector, "open", fail):
                with self.assertRaises(draft_mod.DraftError) as cm:
                    asks("http://127.0.0.1:1/v1")
            msg = str(cm.exception)
            self.assertTrue(msg.isprintable(), ascii(msg))
            self.assertNotIn(ESC, msg)
            self.assertLess(len(msg), 250)
            self.assertIn("reset", msg)             # it still says what happened
            self.assertTrue(cm.exception.__suppress_context__)

    def test_credentials_and_the_query_of_the_model_url_are_not_shown(self):
        url = "http://user:pa55word@127.0.0.1:1/v1?key=sekrit#frag"
        # urllib hands `user:pa55word@127.0.0.1` to name resolution as if it
        # were a host name. The lookup is faked here, so that no resolver is
        # ever asked about it, whatever the platform.
        gone = socket.gaierror(-2, "Name or service not known")
        with mock.patch.object(http.client.HTTPConnection, "connect",
                               side_effect=gone):
            with self.assertRaises(draft_mod.DraftError) as cm:
                asks(url, timeout=2)
        msg = str(cm.exception)
        for secret in ("pa55word", "user:", "sekrit", "frag"):
            self.assertNotIn(secret, msg)
        self.assertIn("127.0.0.1:1/v1", msg)
        self.assertIn("Name or service not known", msg)     # still says why

    def test_a_url_with_a_space_does_not_quote_the_path(self):
        # http.client's InvalidURL message holds the whole request path
        with mock.patch.object(http.client.HTTPConnection, "connect",
                               side_effect=AssertionError("connected")):
            with self.assertRaises(draft_mod.DraftError) as cm:
                asks("http://127.0.0.1:1/v1?token=sekrit more")
        msg = str(cm.exception)
        self.assertNotIn("sekrit", msg)
        self.assertNotIn("token", msg)
        self.assertNotIn("\n", msg)
        # nor does the traceback of an uncaught one: the original is not chained
        self.assertNotIn("sekrit", traceback_text(cm.exception))

    def test_the_shown_url(self):
        shown = draft_mod._shown_url
        self.assertEqual(shown("http://localhost:11434/v1"),
                         "http://localhost:11434/v1")
        self.assertEqual(shown("https://u:p@api.example.test/v1?key=k#f"),
                         "https://api.example.test/v1")
        self.assertEqual(shown("https://tok@api.example.test/v1"),
                         "https://api.example.test/v1")
        self.assertEqual(shown("http://[::1/v1"), "http://[::1/v1")
        self.assertEqual(shown("api.example.test/v1?key=k"), "api.example.test/v1")
        self.assertEqual(shown("http://a/" + ESC + "[31mred\n"),
                         "http://a/?[31mred?")
        self.assertEqual(shown("http://a.test/v1#frag"), "http://a.test/v1")
        # a password with an @ in it: the login ends at the last one
        self.assertEqual(shown("http://user:p@ss@a.test:80/v1"),
                         "http://a.test:80/v1")
        # an @ later in the path is not a login
        self.assertEqual(shown("http://a.test/v1/@scope/x"), "http://a.test/v1/@scope/x")


class TestSlackPushNeverEchoesTheWebhook(unittest.TestCase):
    WEBHOOK = "https://hooks.slack.com/services/T0000/B0000/SECRETTOKEN"

    def raising(self, exc):
        def fake_urlopen(req, timeout=30):
            raise exc
        return mock.patch.object(push_mod.urllib.request, "urlopen", fake_urlopen)

    def test_every_way_the_call_can_fail_is_a_push_error_without_the_url(self):
        failures = {
            "a space in the url": http.client.InvalidURL(
                "URL can't contain control characters. "
                "'/services/T0000/B0000/SECRETTOKEN x' (found at least ' ')"),
            "a value error": ValueError("unknown url type: 'SECRETTOKEN x'"),
            "a unicode error": UnicodeEncodeError(
                "ascii", "SECRETTOKEN" + chr(0xE9), 11, 12, "ordinal not in range"),
            "hung up": http.client.RemoteDisconnected(
                "Remote end closed connection without response"),
            "half an answer": http.client.IncompleteRead(b"abc", 100),
            "bad status line": http.client.BadStatusLine("garbage"),
            "timed out": TimeoutError("timed out"),
            "reset": ConnectionResetError(104, "Connection reset by peer"),
            "unreachable": urllib.error.URLError(
                OSError(101, "Network is unreachable")),
            "http 404": urllib.error.HTTPError(
                "https://hooks.slack.com/services/T0000/B0000/SECRETTOKEN",
                404, "Not Found", {}, None),
            "http 500": urllib.error.HTTPError(
                "https://hooks.slack.com/services/T0000/B0000/SECRETTOKEN",
                500, "Server Error", {}, None),
        }
        for label, exc in failures.items():
            with self.subTest(label), self.raising(exc):
                with self.assertRaises(push_mod.PushError) as cm:
                    push_mod.push_slack(self.WEBHOOK, "notes")
                msg = str(cm.exception)
                self.assertNotIn("SECRETTOKEN", msg)
                self.assertNotIn("services", msg)
                self.assertNotIn("\n", msg)
                self.assertTrue(msg.startswith("Slack push failed"), msg)
                # nor does the traceback of an uncaught one: the library's own
                # error (which quotes the path) is not chained to it
                self.assertNotIn("SECRETTOKEN", traceback_text(cm.exception))
                self.assertTrue(cm.exception.__suppress_context__)

    def test_what_the_library_said_is_one_short_printable_line(self):
        noisy = "reset" + ESC + "[2J by\npeer " + "x" * 400
        failures = {
            "an unreachable host": urllib.error.URLError(noisy),
            "a hang-up": http.client.HTTPException(noisy),
            "a reset": ConnectionResetError(noisy),
            "an http error": urllib.error.HTTPError(
                self.WEBHOOK, 500, noisy, {}, None),
        }
        for label, exc in failures.items():
            with self.subTest(label), self.raising(exc):
                with self.assertRaises(push_mod.PushError) as cm:
                    push_mod.push_slack(self.WEBHOOK, "notes")
            msg = str(cm.exception)
            self.assertTrue(msg.isprintable(), ascii(msg))
            self.assertNotIn(ESC, msg)
            self.assertLess(len(msg), 250)
            self.assertIn("reset", msg)             # it still says what happened
            self.assertNotIn("SECRETTOKEN", msg)
            self.assertTrue(cm.exception.__suppress_context__)

    def test_the_http_status_is_still_reported(self):
        exc = urllib.error.HTTPError(self.WEBHOOK, 404, "Not Found", {}, None)
        with self.raising(exc):
            with self.assertRaises(push_mod.PushError) as cm:
                push_mod.push_slack(self.WEBHOOK, "notes")
        self.assertIn("404", str(cm.exception))

    def test_a_stray_space_for_real(self):
        # no fake urlopen: http.client itself refuses the path, before any
        # connection is made
        with mock.patch.object(http.client.HTTPConnection, "connect",
                               side_effect=AssertionError("connected")), \
                mock.patch.object(http.client.HTTPSConnection, "connect",
                                  side_effect=AssertionError("connected")):
            with self.assertRaises(push_mod.PushError) as cm:
                push_mod.push_slack(self.WEBHOOK + " BAD", "notes")
        self.assertNotIn("SECRETTOKEN", str(cm.exception))
        self.assertNotIn("SECRETTOKEN", traceback_text(cm.exception))

    def test_the_command_line(self):
        with mock.patch.object(http.client.HTTPConnection, "connect",
                               side_effect=AssertionError("connected")), \
                mock.patch.object(http.client.HTTPSConnection, "connect",
                                  side_effect=AssertionError("connected")):
            code, out, err = run_cli("--root", make_repo(), "--since",
                                     "2026-10-14", "--push-slack",
                                     self.WEBHOOK + " BAD")
            self.assertEqual(code, 2)
            self.assertTrue(err.startswith("error: "), err)
            self.assertNotIn("SECRETTOKEN", err + out)
            self.assertNotIn("Traceback", err)
            with mock.patch.dict(os.environ, {
                    "AUTOSTANDUP_SLACK_WEBHOOK": self.WEBHOOK + " BAD"}):
                code, out, err = run_cli("--root", make_repo(), "--since",
                                         "2026-10-14", "--push-slack")
            self.assertEqual(code, 2)
            self.assertNotIn("SECRETTOKEN", err + out)


class TestReadmeSaysSo(unittest.TestCase):
    def test_the_draft_and_push_paragraphs(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            flat = " ".join(f.read().split())
        for phrase in ("does not follow a redirect to another address",
                       "never shown in an error message"):
            self.assertTrue(phrase in flat, f"the README does not say: {phrase!r}")


if __name__ == "__main__":
    unittest.main()
