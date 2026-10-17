"""HTTP bridge + Activepieces workflow template tests."""

import json
import os
import socket
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from standupguy import serve as serve_mod
from test_standupguy import make_repo


def start_server(root: str):
    server = serve_mod.make_server(root, "127.0.0.1", 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def get(port: int, path: str):
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
        return resp.status, resp.headers.get("Content-Type"), \
            resp.read().decode()


class TestServe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = make_repo()
        cls.server, cls.port = start_server(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_health(self):
        status, ctype, body = get(self.port, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_digest_json(self):
        status, ctype, body = get(
            self.port, "/digest?since=2026-10-14&author=dan@example.com")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        data = json.loads(body)
        subjects = [c["subject"] for c in data["commits"]]
        self.assertIn("feat: add login flow", subjects)
        self.assertIn("fix: null pointer on logout", subjects)
        self.assertIn("wip.py", data["in_progress"])
        self.assertIn("## Shipped", data["markdown"])

    def test_digest_markdown(self):
        status, ctype, body = get(self.port, "/digest?format=markdown")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", ctype)
        self.assertTrue(body.startswith("# Standup"))

    def test_digest_defaults(self):
        # no params: since=yesterday, format=json — must not 500
        status, _, body = get(self.port, "/digest")
        self.assertEqual(status, 200)
        json.loads(body)

    def test_digest_until_range(self):
        status, _, body = get(
            self.port, "/digest?since=2026-10-01&until=2026-10-12&format=json")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual([c["subject"] for c in data["commits"]],
                         ["chore: bump deps"])
        self.assertEqual(data["label"], "2026-10-01 → 2026-10-12")

    def test_week(self):
        status, _, body = get(self.port, "/week?author=dan@example.com")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["label"], "week")

    def test_404(self):
        try:
            get(self.port, "/nope")
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_server_is_threaded(self):
        # the Round 2 finding: the old single-threaded HTTPServer wedged on
        # one stalled connection
        self.assertIsInstance(self.server, ThreadingHTTPServer)

    def test_stalled_connection_does_not_block_second_request(self):
        # open a connection that never sends a request; the bridge must
        # still answer a second connection promptly
        stall = socket.create_connection(("127.0.0.1", self.port),
                                         timeout=10)
        try:
            start = time.monotonic()
            status, _, body = get(self.port, "/health")
            elapsed = time.monotonic() - start
        finally:
            stall.close()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        # generous upper bound: with the old single-threaded server this
        # request would hang until the 30s per-connection timeout (or until
        # the stalled connection closes)
        self.assertLess(elapsed, 10)


class TestWorkflow(unittest.TestCase):
    PATH = os.path.join(os.path.dirname(__file__), "..", "workflows",
                        "activepieces-standup-digest.json")

    def test_valid_activepieces_flow(self):
        with open(self.PATH, encoding="utf-8") as f:
            wf = json.load(f)
        steps = {s["id"]: s for s in wf["steps"]}
        pieces = [s["piece"] for s in wf["steps"]]
        # required steps present: schedule trigger, HTTP, branch, Slack
        self.assertTrue(any("Schedule" in p for p in pieces),
                        "missing Schedule trigger step")
        self.assertTrue(any("HTTP" in p for p in pieces),
                        "missing HTTP Request step")
        self.assertTrue(any("Branch" in p for p in pieces),
                        "missing Branch step")
        self.assertTrue(any("Slack" in p for p in pieces),
                        "missing Slack step")
        # every step documents its piece, action, and config
        for s in wf["steps"]:
            for field in ("id", "name", "piece", "action", "config"):
                self.assertIn(field, s,
                              f"step {s.get('id')} missing {field}")
        # http step points at the serve bridge
        http = next(s for s in wf["steps"] if "HTTP" in s["piece"])
        self.assertIn("8765/digest", http["config"]["url"])
        self.assertEqual(http["config"]["method"], "GET")
        # schedule runs weekday mornings
        sched = next(s for s in wf["steps"] if "Schedule" in s["piece"])
        self.assertEqual(sched["config"]["cron"], "0 9 * * 1-5")
        # setup notes embedded for the human importing the flow
        self.assertIn("setup_notes", wf)
        notes = " ".join(wf["setup_notes"])
        self.assertIn("standupguy serve", notes)
        self.assertIn("Slack", notes)
        self.assertIn("channel", notes.lower())


if __name__ == "__main__":
    unittest.main()
