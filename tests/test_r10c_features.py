"""Round 10c: git hardening, response cap, serve slots and cache, tickets
and the CSV time sheet."""

import csv
import io
import json
import os
import subprocess
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from standupguy import cli as cli_mod
from standupguy import digest as digest_mod
from standupguy import draft as draft_mod
from standupguy import gitlog as gitlog_mod
from standupguy import serve as serve_mod

from test_round7 import git_words
import test_round7
make_repo = test_round7.make_repo


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def C(sha, subject, date="2026-10-14"):
    return gitlog_mod.Commit(sha, "A", "a@x", date, subject, [], None)


class TestGitHardening(unittest.TestCase):
    def test_every_call_disables_fsmonitor_and_hooks_and_optional_locks(self):
        seen = []
        real = subprocess.run

        def spy(cmd, *a, **kw):
            seen.append((list(cmd), dict(kw.get("env") or {})))
            return real(cmd, *a, **kw)

        repo = make_repo()
        with mock.patch.object(gitlog_mod.subprocess, "run", spy):
            gitlog_mod._git(repo, "rev-parse", "--git-dir")
        cmd, env = seen[0]
        self.assertEqual(cmd[:5], ["git", "-c", "core.fsmonitor=false", "-c",
                                   f"core.hooksPath={os.devnull}"])
        self.assertEqual(git_words(cmd), ["rev-parse", "--git-dir"])
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")

    def test_a_repo_that_names_an_fsmonitor_program_does_not_run_it(self):
        repo = make_repo()
        marker = os.path.join(repo, "ran.txt")
        script = os.path.join(repo, "fsm.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"open({marker!r}, 'w').write('x')\n")
        import sys
        cfg = ["git", "-C", repo, "config", "core.fsmonitor",
               f"{sys.executable} {script}"]
        subprocess.run(cfg, check=True)
        gitlog_mod.uncommitted_for(repo, None)
        self.assertFalse(os.path.exists(marker))


class TestResponseCap(unittest.TestCase):
    def test_a_huge_reply_is_dropped(self):
        class R:
            length = 0
            def __init__(self):
                self.asked = None
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=None):
                self.asked = n
                return b"x" * n

        r = R()

        class O:
            def open(self, req, timeout=None): return r
        with mock.patch.object(draft_mod.urllib.request, "build_opener",
                               return_value=O()):
            with self.assertRaises(draft_mod.DraftError) as cm:
                draft_mod.draft("# d", base_url="http://127.0.0.1:1/v1",
                                api_key="k" * 4, model="m")
        self.assertIn("4 MB", str(cm.exception))
        self.assertEqual(r.asked, draft_mod.MAX_RESPONSE_BYTES + 1)


class TestServeSlotsAndCache(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.server = serve_mod.make_server(self.repo, port=0)
        self.addCleanup(self.server.server_close)

    def handler(self):
        h = serve_mod.Handler.__new__(serve_mod.Handler)
        h.server = self.server
        return h

    def test_the_same_query_is_built_once_inside_the_window(self):
        calls = []
        real = serve_mod._payload

        def counting(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        clock = [100.0]
        q = {"since": ["2026-10-01"], "all": ["1"], "token": ["secret-value"]}
        with mock.patch.object(serve_mod, "_payload", counting), \
                mock.patch.object(serve_mod, "_now", lambda: clock[0]):
            h = self.handler()
            first = h._cached_payload(q, False)
            second = h._cached_payload(dict(q, token=["other"]), False)
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            clock[0] += serve_mod.CACHE_SECONDS + 1
            h._cached_payload(q, False)
            self.assertEqual(len(calls), 2)
        self.assertFalse(any("secret-value" in repr(k)
                             for k in self.server.cache))

    def test_errors_are_not_cached(self):
        with mock.patch.object(serve_mod, "_payload",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.handler()._cached_payload({}, False)
        self.assertEqual(self.server.cache, {})

    def test_at_most_four_digests_run_at_once(self):
        self.assertEqual(serve_mod.MAX_CONCURRENT, 4)
        running, peak, lock = [0], [0], threading.Lock()
        gate = threading.Event()

        def slow(*a, **kw):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            gate.wait(5)
            with lock:
                running[0] -= 1
            return 200, "text/plain", "x"
        results = []
        with mock.patch.object(serve_mod, "_payload", slow), \
                mock.patch.object(serve_mod, "CACHE_SECONDS", 0):
            ts = [threading.Thread(
                target=lambda i=i: results.append(
                    self.handler()._cached_payload({"n": [str(i)]}, False)))
                for i in range(7)]
            for t in ts:
                t.start()
            deadline = 0
            while peak[0] < 4 and deadline < 200:
                threading.Event().wait(0.02)
                deadline += 1
            gate.set()
            for t in ts:
                t.join(10)
        self.assertEqual(peak[0], 4)
        self.assertEqual(len(results), 7)

    def test_a_busy_bridge_says_503(self):
        for _ in range(serve_mod.MAX_CONCURRENT):
            self.server.slots.acquire()
        with mock.patch.object(serve_mod, "SLOT_WAIT", 0.01):
            status, _, body = self.handler()._cached_payload({"x": ["1"]}, False)
        self.assertEqual(status, 503)
        self.assertIn("busy", body)


class TestTicketsAndTimesheet(unittest.TestCase):
    def test_ticket_ids(self):
        f = digest_mod.ticket_ids
        self.assertEqual(f("feat: ABC-123 and LIN-9, again ABC-123"),
                         ["ABC-123", "LIN-9"])
        self.assertEqual(f("fix UTF-8 and SHA-256 and ISO-8601 handling"), [])
        self.assertEqual(f("abc-123 lowercase and XABC-1234567 too long"), [])
        self.assertEqual(f("[PROJ-42] subject"), ["PROJ-42"])

    def test_digest_has_a_ticket_rollup(self):
        cs = [C("a" * 40, "feat: ABC-1 add x"), C("b" * 40, "fix: ABC-1 y"),
              C("c" * 40, "docs: nothing")]
        lines = digest_mod.ticket_lines(cs)
        self.assertEqual(lines[0], "## Tickets")
        self.assertIn("ABC-1: 2 commits", lines[1])
        self.assertEqual(digest_mod.ticket_lines([cs[2]]), [])

    def test_csv_rows_and_formula_safety(self):
        cs = [C("a" * 40, "feat: ABC-1 add x", "2026-10-13"),
              C("b" * 40, "fix: ABC-1 y", "2026-10-13"),
              C("c" * 40, "=HYPERLINK(\"http://x\")", "2026-10-14")]
        text = digest_mod.timesheet_csv([("repo", cs)])
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0], ["date", "repo", "ticket", "commits",
                                   "summary", "hours"])
        self.assertEqual(rows[1][:4], ["2026-10-13", "repo", "ABC-1", "2"])
        self.assertEqual(rows[1][5], "")
        self.assertTrue(rows[2][4].startswith("'="))

    def test_cli_csv_and_json_tickets(self):
        repo = make_repo()
        code, out, err = run("--root", repo, "--all", "--since", "2026-10-01",
                             "--format", "csv")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("date,repo,ticket,commits,summary,hours"))
        code, out, _ = run("--root", repo, "--all", "--since", "2026-10-01",
                           "--format", "json")
        self.assertIn("tickets", json.loads(out))

    def test_csv_cannot_be_combined_with_draft(self):
        code, _, err = run("--format", "csv", "--draft")
        self.assertEqual(code, 2)
        self.assertIn("--format csv", err)


if __name__ == "__main__":
    unittest.main()
