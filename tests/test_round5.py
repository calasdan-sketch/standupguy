"""Expert review round 5 regressions.

H-1  Commit text (subject, author name, file name, a lone CR) cannot forge
     or corrupt commit records in the digest.
H-2  serve: a port in use / a path that is not a repo is a clean one-line
     error; several --root are served like the multi-repo digest.
H-3  --draft: an endpoint that answers 200 with something that is not a
     completion is a clean one-line error, and the digest is still shown.
H-4  MCP: a bad request is -32602 (or -32600 / -32700), never -32603 and
     never a dead server; notifications stay silent.
H-5  A reader that goes away (`standupguy | head -1`, a client that hangs
     up mid-response) is not an error: no traceback, no noise at exit.
H-6  pyproject metadata: SPDX licence, classifiers, keywords, project URLs.
"""

import contextlib
import datetime
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

try:
    import tomllib
except ImportError:             # Python 3.10: no TOML reader in the stdlib
    tomllib = None

from test_standupguy import commit, git, make_repo  # isolates git/HOME

from standupguy import cli as cli_mod
from standupguy import digest as digest_mod
from standupguy import draft as draft_mod
from standupguy import gitlog as gitlog_mod
from standupguy import mcp as mcp_mod
from standupguy import push as push_mod
from standupguy import serve as serve_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*argv):
    """(exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ H-1

FAKE_SHA = "deadbeef" * 5


def forged(sha=FAKE_SHA):
    """A whole commit record in the format the OLD parser used:
    RS sha US name US email US date US subject."""
    return (f"\x1e{sha}\x1fCEO Alice\x1fceo@example.com\x1f2026-10-14"
            "\x1ffeat: FORGED launch")


def real_shas(root):
    return set(gitlog_mod._git(root, "rev-list", "HEAD").split())


def head(root):
    return gitlog_mod._git(root, "rev-parse", "HEAD").strip()


class TestForgedRecords(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        git(self.tmp, "init", "-qb", "main")
        commit(self.tmp, "Dan", "dan@example.com", "2026-10-14T10:00:00",
               "feat: real work", {"a.py": "1"})
        self.dan = head(self.tmp)

    def commits(self):
        return gitlog_mod.commits_since(self.tmp, "2026-10-01")

    def assert_only_real_commits(self, cs):
        """Exactly the commits git has: no extra record, none twice."""
        shas = [c.sha for c in cs]
        self.assertEqual(sorted(shas), sorted(real_shas(self.tmp)), shas)
        self.assertNotIn("CEO Alice", {c.author for c in cs})
        md = digest_mod.digest_markdown(self.tmp, "2026-10-01", "x")
        self.assertNotIn("`deadbee`", md)    # no bullet for the fake sha

    def test_subject_cannot_forge_a_record(self):
        subject = "fix: tiny typo" + forged()
        commit(self.tmp, "Mallory", "m@example.com", "2026-10-14T11:00:00",
               subject, {"b.py": "2"})
        cs = self.commits()
        self.assert_only_real_commits(cs)
        c = next(c for c in cs if c.sha == head(self.tmp))
        self.assertEqual(c.subject, subject)      # her own text, verbatim
        self.assertEqual(c.files, ["b.py"])

    def test_author_name_cannot_forge_a_record(self):
        name = "Mallory" + forged()
        commit(self.tmp, name, "m@example.com", "2026-10-14T11:00:00",
               "fix: honest", {"b.py": "2"})
        cs = self.commits()
        self.assert_only_real_commits(cs)
        c = next(c for c in cs if c.sha == head(self.tmp))
        self.assertEqual((c.author, c.email, c.subject, c.files),
                         (name, "m@example.com", "fix: honest", ["b.py"]))

    def test_author_name_cannot_shift_the_fields_of_a_real_commit(self):
        # no extra record here: the REAL sha used to come back with the
        # attacker's email, date and subject
        name = "Mallory\x1fceo@example.com\x1f2026-10-01\x1ffeat: FORGED"
        commit(self.tmp, name, "m@example.com", "2026-10-14T11:00:00",
               "fix: honest", {"b.py": "2"})
        cs = self.commits()
        self.assert_only_real_commits(cs)
        c = next(c for c in cs if c.sha == head(self.tmp))
        self.assertEqual((c.author, c.email, c.date, c.subject),
                         (name, "m@example.com", "2026-10-14", "fix: honest"))

    @unittest.skipIf(os.name == "nt",
                     "control characters are not allowed in Windows paths")
    def test_file_name_cannot_forge_a_record(self):
        fname = "x" + forged()
        commit(self.tmp, "Mallory", "m@example.com", "2026-10-14T11:00:00",
               "fix: honest", {fname: "2"})
        cs = self.commits()
        self.assert_only_real_commits(cs)
        c = next(c for c in cs if c.sha == head(self.tmp))
        self.assertEqual(c.files, [fname])

    def test_forged_record_cannot_reuse_a_real_sha(self):
        # a fake record carrying Dan's REAL sha passes any "is this a real
        # sha" check; it must simply never exist
        commit(self.tmp, "Mallory", "m@example.com", "2026-10-14T11:00:00",
               "fix: honest" + forged(sha=self.dan), {"b.py": "2"})
        cs = self.commits()
        self.assert_only_real_commits(cs)
        dan = next(c for c in cs if c.sha == self.dan)
        self.assertEqual((dan.author, dan.subject, dan.files),
                         ("Dan", "feat: real work", ["a.py"]))

    def test_carriage_return_in_a_subject_stays_in_the_subject(self):
        # text-mode pipes turned a lone CR into a newline: the subject was
        # cut and its tail leaked into the file list
        subject = "fix: honest\rfake.py"
        commit(self.tmp, "Mallory", "m@example.com", "2026-10-14T11:00:00",
               subject, {"b.py": "2"})
        cs = self.commits()
        self.assert_only_real_commits(cs)
        c = next(c for c in cs if c.sha == head(self.tmp))
        self.assertEqual((c.subject, c.files), (subject, ["b.py"]))

    def test_nul_in_a_raw_commit_forges_nothing(self):
        # `git commit` refuses NUL bytes; a crafted object (hash-object
        # --literally) can carry one in the author line and the message
        tree = gitlog_mod._git(self.tmp, "rev-parse", "HEAD^{tree}").strip()
        when = int(datetime.datetime(2026, 10, 14, 12,
                                     tzinfo=datetime.timezone.utc).timestamp())
        raw = (f"tree {tree}\nparent {self.dan}\n"
               f"author Mal\x00lory <m@example.com> {when} +0000\n"
               f"committer Mallory <m@example.com> {when} +0000\n\n"
               "fix: subject\x00" + forged() + "\nbody\n").encode()
        sha = subprocess.run(
            ["git", "-C", self.tmp, "hash-object", "-t", "commit", "-w",
             "--stdin", "--literally"], input=raw, check=True,
            capture_output=True).stdout.decode().strip()
        git(self.tmp, "update-ref", "refs/heads/main", sha)
        cs = self.commits()
        self.assertNotIn("CEO Alice", {c.author for c in cs})
        self.assertIn(self.dan, {c.sha for c in cs})
        self.assertTrue({c.sha for c in cs} <= real_shas(self.tmp))
        # depending on the git version that author line yields no date
        # (the record is malformed and dropped) or a clean one: never junk
        for c in cs:
            self.assertRegex(c.date, r"^\d{4}-\d{2}-\d{2}$")

    def test_malformed_records_are_dropped(self):
        ok = "\x1e" + "a" * 40 + "\x1fA\x1fa@x\x1f2026-10-14\x1fok\na.py\0\0"
        bad_sha = ("\x1e" + "not a sha!"
                   + "\x1fA\x1fa@x\x1f2026-10-14\x1fbad sha\n")
        bad_date = ("\x1e" + "b" * 40
                    + "\x1fA\x1fa@x\x1fyesterday\x1fbad date\0")
        too_short = "\x1e" + "c" * 40 + "\x1fA\x1fa@x\x1f2026-10-14\0"
        cs = gitlog_mod.parse_log(ok + bad_sha + bad_date + too_short)
        self.assertEqual([c.subject for c in cs], ["ok"])

    def test_every_sha_is_checked_against_rev_list(self):
        # belt and braces: even if a fake record got through the parser
        # (here: injected into its result), `git rev-list` for the same
        # range does not know the sha, so it is dropped
        real_parse = gitlog_mod.parse_log

        def parse_with_a_fake(*args, **kwargs):
            return real_parse(*args, **kwargs) + [gitlog_mod.Commit(
                FAKE_SHA, "CEO Alice", "ceo@example.com", "2026-10-14",
                "feat: FORGED launch", [])]

        with mock.patch.object(gitlog_mod, "parse_log", parse_with_a_fake):
            cs = self.commits()
        self.assert_only_real_commits(cs)
        self.assertEqual([c.sha for c in cs], [self.dan])

    def test_the_cross_check_respects_until(self):
        # same range for log and rev-list: a bounded window must not lose
        # (or keep) anything
        commit(self.tmp, "Dan", "dan@example.com", "2026-10-16T10:00:00",
               "fix: later", {"b.py": "2"})
        cs = gitlog_mod.commits_since(self.tmp, "2026-10-01", None,
                                      "2026-10-15")
        self.assertEqual([c.sha for c in cs], [self.dan])
        cs = gitlog_mod.commits_since(self.tmp, "2026-10-15")
        self.assertEqual([c.subject for c in cs], ["fix: later"])

    def test_digest_cli_shows_only_real_commits(self):
        commit(self.tmp, "Mallory", "m@example.com", "2026-10-14T11:00:00",
               "fix: tiny typo" + forged(), {"b.py": "2"})
        code, out, err = run_cli("--root", self.tmp, "--all", "--since",
                                 "2026-10-01", "--format", "json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(sorted(c["sha"] for c in data["commits"]),
                         sorted(real_shas(self.tmp)))


# ------------------------------------------------------------------ H-2


def listening_socket():
    """Something already listening on a free port, the way a running
    `standupguy serve` would be. -> (socket, port)"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s, s.getsockname()[1]


def second_repo(email="bea@example.com", name="Bea"):
    tmp = tempfile.mkdtemp()
    git(tmp, "init", "-qb", "main")
    git(tmp, "config", "user.email", email)
    git(tmp, "config", "user.name", name)
    commit(tmp, name, email, "2026-10-14T12:00:00", "feat: second repo thing",
           {"z.py": "1"})
    return tmp


class TestServeStartup(unittest.TestCase):
    def test_port_in_use_is_a_clean_one_line_error(self):
        blocker, port = listening_socket()
        self.addCleanup(blocker.close)
        code, out, err = run_cli("--root", make_repo(), "serve",
                                 "--port", str(port))
        self.assertEqual(code, 2)
        lines = err.strip().splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("error: "), err)
        self.assertIn(str(port), lines[0])
        self.assertIn("already in use", lines[0])
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")                # no "serving ..." banner

    def test_port_in_use_in_a_real_process(self):
        blocker, port = listening_socket()
        self.addCleanup(blocker.close)
        proc = subprocess.run(
            [sys.executable, "-m", "standupguy", "--root", make_repo(),
             "serve", "--port", str(port)],
            capture_output=True, cwd=REPO, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(proc.stdout, b"")

    def test_other_bind_failures_are_clean_too(self):
        # 192.0.2.1 (TEST-NET-1) is not an address of this machine
        code, out, err = run_cli("--root", make_repo(), "serve",
                                 "--host", "192.0.2.1", "--port", "0")
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("cannot listen on 192.0.2.1", err)

    def test_port_out_of_range_is_refused_by_the_parser(self):
        for bad in ("70000", "-1", "http"):
            with self.subTest(bad):
                with contextlib.redirect_stderr(io.StringIO()) as err, \
                        self.assertRaises(SystemExit) as cm:
                    cli_mod.main(["serve", "--port", bad])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("--port", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())

    def test_a_path_that_is_not_a_repo_never_starts_the_server(self):
        notarepo = tempfile.mkdtemp()
        with mock.patch.object(
                serve_mod.ThreadedBridgeServer, "serve_forever",
                side_effect=AssertionError("served a directory that is "
                                           "not a git repo")):
            code, out, err = run_cli("--root", notarepo, "serve",
                                     "--port", "0")
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), f"error: not a git repo: {notarepo}")
        self.assertEqual(out, "")

    def test_one_bad_root_among_good_ones_is_still_refused(self):
        notarepo = tempfile.mkdtemp()
        with mock.patch.object(
                serve_mod.ThreadedBridgeServer, "serve_forever",
                side_effect=AssertionError("started")):
            code, _, err = run_cli("--root", make_repo(), "--root", notarepo,
                                   "serve", "--port", "0")
        self.assertEqual(code, 2)
        self.assertIn(f"not a git repo: {notarepo}", err)

    def test_not_a_repo_in_a_real_process_exits_instead_of_serving(self):
        notarepo = tempfile.mkdtemp()
        proc = subprocess.run(
            [sys.executable, "-m", "standupguy", "--root", notarepo,
             "serve", "--port", "0"],
            capture_output=True, cwd=REPO, timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(b"not a git repo", proc.stderr)
        self.assertNotIn(b"Traceback", proc.stderr)


class TestServeSeveralRoots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = make_repo()                       # Dan (+ Teammate)
        cls.b = second_repo()                     # Bea
        cls.server = serve_mod.make_server([cls.a, cls.b], "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, port=None):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port or self.port}{path}",
                timeout=10) as resp:
            return resp.headers.get("Content-Type"), resp.read().decode("utf-8")

    def test_cli_hands_every_root_to_the_server(self):
        with mock.patch.object(serve_mod, "serve", return_value=0) as served:
            code, _, _ = run_cli("--root", self.a, "--root", self.b, "serve",
                                 "--port", "0")
        self.assertEqual(code, 0)
        self.assertEqual(served.call_args.args[0], [self.a, self.b])

    def test_json_has_one_entry_per_repo_like_the_cli(self):
        _, body = self.get("/digest?since=2026-10-01&all=1")
        data = json.loads(body)
        self.assertEqual([r["repo"] for r in data["repos"]],
                         [os.path.basename(self.a), os.path.basename(self.b)])
        subjects = [[c["subject"] for c in r["commits"]]
                    for r in data["repos"]]
        self.assertIn("feat: add login flow", subjects[0])
        self.assertEqual(subjects[1], ["feat: second repo thing"])
        # each entry has exactly the shape of `standupguy --format json`
        code, out, _ = run_cli("--root", self.a, "--root", self.b, "--all",
                               "--since", "2026-10-01", "--format", "json")
        self.assertEqual(data["repos"], json.loads(out)["repos"])
        # ...and the same top-level keys a single-repo workflow reads
        md = data["markdown"]
        self.assertEqual(md.count("# Standup — "), 2)
        self.assertIn("add login flow", md)
        self.assertIn("second repo thing", md)
        self.assertIn("second repo thing", data["slack_text"])
        self.assertNotIn("<", data["slack_text"])

    def test_markdown_is_one_section_per_repo(self):
        ctype, body = self.get("/digest?since=2026-10-01&all=1&format=markdown")
        self.assertIn("text/markdown", ctype)
        self.assertEqual(body.count("# Standup — "), 2)
        _, body = self.get("/week?since=2026-10-01&all=1&format=markdown")
        self.assertEqual(body.count("# Weekly summary — "), 2)
        _, body = self.get("/week?since=2026-10-01&all=1")
        self.assertEqual(len(json.loads(body)["repos"]), 2)

    def test_the_default_author_is_each_repos_own_user(self):
        # no author given: Dan's commits from repo a, Bea's from repo b —
        # what the CLI does for `--root a --root b`
        _, body = self.get("/digest?since=2026-10-01")
        authors = [{c["author"] for c in r["commits"]}
                   for r in json.loads(body)["repos"]]
        self.assertEqual(authors, [{"Dan"}, {"Bea"}])

    def test_servers_in_one_process_do_not_share_their_roots(self):
        # the root used to live on the Handler CLASS: the second server
        # silently re-pointed the first
        other = serve_mod.make_server(self.b, "127.0.0.1", 0)
        threading.Thread(target=other.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        _, body = self.get("/digest?since=2026-10-01&all=1",
                           port=other.server_address[1])
        data = json.loads(body)
        self.assertEqual(data["repo"], os.path.basename(self.b))
        self.assertNotIn("repos", data)           # one root: flat, as ever
        _, body = self.get("/digest?since=2026-10-01&all=1")
        self.assertEqual(len(json.loads(body)["repos"]), 2)


# ------------------------------------------------------------------ H-3


class RawModel:
    """A fake OpenAI-style endpoint that answers however the test says:
    `body` bytes with HTTP 200, optionally claiming `declared` bytes (a
    cut-off body), hanging up without an answer, or answering after `delay`
    seconds."""

    def __init__(self, body=b"", *, declared=None, hang_up=False, delay=0.0):
        class H(BaseHTTPRequestHandler):
            def do_POST(h):
                h.rfile.read(int(h.headers.get("Content-Length", 0)))
                if hang_up:
                    h.connection.close()
                    return
                time.sleep(delay)
                h.send_response(200)
                h.send_header("Content-Type", "application/json")
                h.send_header("Content-Length", str(
                    len(body) if declared is None else declared))
                h.end_headers()
                h.wfile.write(body)

            def log_message(h, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *a, **k: None   # client gave up
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def completion(content) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


# what a misbehaving endpoint can answer with HTTP 200, and a word the
# one-line error must contain
BAD_ANSWERS = {
    "html error page": (RawModel, dict(
        body=b"<html><body><h1>502 Bad Gateway</h1></body></html>"),
        "not with JSON"),
    "empty body": (RawModel, dict(body=b""), "empty"),
    "truncated json": (RawModel, dict(
        body=b'{"choices": [{"message": {"con'), "not with JSON"),
    "not utf-8": (RawModel, dict(body=b"\xff\xfe\x00bad"), "not with JSON"),
    "json but not an object": (RawModel, dict(body=b"[]"), "unexpected"),
    "null content": (RawModel, dict(body=completion(None)), "no text"),
    "blank content": (RawModel, dict(body=completion("  \n")), "no text"),
    "body cut short": (RawModel, dict(
        body=b'{"choices"', declared=500), "stopped answering"),
    "hung up": (RawModel, dict(hang_up=True), "stopped answering"),
    "too slow": (RawModel, dict(body=b"{}", delay=2), "stopped answering"),
}


class TestDraftBadAnswers(unittest.TestCase):
    def each(self, skip=()):
        for name, (cls, kwargs, word) in BAD_ANSWERS.items():
            if name in skip:
                continue
            model = cls(**kwargs)
            try:
                yield name, model, word
            finally:
                model.stop()

    def test_every_bad_answer_is_a_draft_error(self):
        for name, model, word in self.each():
            with self.subTest(name):
                with self.assertRaises(draft_mod.DraftError) as cm:
                    draft_mod.draft("digest", base_url=model.url,
                                    api_key="x", model="m", timeout=1)
                msg = str(cm.exception)
                self.assertIn(word, msg)
                self.assertNotIn("\n", msg)       # one line
                self.assertNotIn("Traceback", msg)

    def test_the_error_shows_what_the_endpoint_said(self):
        model = RawModel(b"<html>\n  <h1>502 Bad   Gateway</h1>\x1b[2J</html>")
        try:
            with self.assertRaises(draft_mod.DraftError) as cm:
                draft_mod.draft("digest", base_url=model.url, timeout=5)
        finally:
            model.stop()
        msg = str(cm.exception)
        self.assertIn("502 Bad Gateway", msg)      # whitespace folded
        self.assertNotIn("\x1b", msg)              # no terminal escapes
        self.assertIn(model.url, msg)

    def test_a_url_or_key_http_cannot_send_is_a_clean_error_too(self):
        # both used to escape as a raw ValueError / UnicodeEncodeError
        with self.assertRaises(draft_mod.DraftError) as cm:
            draft_mod.draft("digest", base_url="http://[::1/v1", timeout=2)
        self.assertIn("http://[::1/v1", str(cm.exception))
        model = RawModel(completion("Yesterday:"))
        try:
            with self.assertRaises(draft_mod.DraftError) as cm:
                draft_mod.draft("digest", base_url=model.url, timeout=2,
                                api_key="\u043a\u043b\u044e\u0447")
        finally:
            model.stop()
        self.assertNotIn("\n", str(cm.exception))
        code, out, err = run_cli("--root", make_repo(), "--since", "2026-10-14",
                                 "--draft", "--base-url", "http://[::1/v1")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn("add login flow", out)

    def test_a_good_answer_is_still_returned(self):
        model = RawModel(completion("  Yesterday:\n- did things \n"))
        try:
            out = draft_mod.draft("digest", base_url=model.url, timeout=5)
        finally:
            model.stop()
        self.assertEqual(out, "Yesterday:\n- did things")

    def test_cli_prints_a_clean_error_and_still_shows_the_digest(self):
        repo = make_repo()
        # (the CLI has no --timeout: its 120 s default is not testable)
        for name, model, word in self.each(skip=("too slow",)):
            with self.subTest(name):
                code, out, err = run_cli(
                    "--root", repo, "--since", "2026-10-14", "--draft",
                    "--base-url", model.url)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error: "), err)
                self.assertIn(word, err)
                self.assertNotIn("Traceback", err)
                self.assertLessEqual(len(err.strip().splitlines()), 2, err)
                # the digest needs no model: it is still there
                self.assertIn("add login flow", out)
                self.assertIn("## Shipped", out)

    def test_a_failed_draft_posts_nothing_to_slack(self):
        repo = make_repo()
        model = RawModel(b"<html>nope</html>")
        try:
            with mock.patch.object(push_mod, "push_slack") as push:
                code, out, err = run_cli(
                    "--root", repo, "--since", "2026-10-14", "--draft",
                    "--base-url", model.url, "--push-slack",
                    "https://hooks.slack.com/services/T/x")
        finally:
            model.stop()
        self.assertEqual(code, 2)
        push.assert_not_called()
        self.assertIn("not with JSON", err)
        self.assertIn("Slack", err)                # says nothing was posted
        self.assertIn("add login flow", out)


# ------------------------------------------------------------------ H-4

TOOL = "what_did_i_ship"


def tools_call(params=..., rid=1):
    """A tools/call request; params=... leaves `params` out entirely, and
    rid=... makes it a notification (no id)."""
    req = {"jsonrpc": "2.0", "method": "tools/call"}
    if rid is not ...:
        req["id"] = rid
    if params is not ...:
        req["params"] = params
    return req


def with_args(arguments):
    return {"name": TOOL, "arguments": arguments}


# a client mistake in the params of tools/call
BAD_PARAMS = {
    "params missing": ...,
    "params null": None,
    "params list": [1],
    "params string": "what_did_i_ship",
    "params number": 5,
    "params true": True,
    "params empty": {},
    "name missing": {"arguments": {}},
    "name null": {"name": None},
    "name number": {"name": 5},
    "name list": {"name": [TOOL]},
    "name object": {"name": {"a": 1}},
    "name unknown": {"name": "nope"},
    "arguments list": with_args([1]),
    "arguments empty list": with_args([]),
    "arguments string": with_args("since=today"),
    "arguments number": with_args(5),
    "arguments true": with_args(True),
    "since number": with_args({"since": 5}),
    "since bool": with_args({"since": True}),
    "since list": with_args({"since": ["today"]}),
    "until number": with_args({"until": 20261014}),
    "author object": with_args({"author": {"name": "dan"}}),
    "author number": with_args({"author": 7}),
    "all string": with_args({"all": "true"}),
    "all number": with_args({"all": 1}),
    "root number": with_args({"root": 3}),
    "root list": with_args({"root": ["a"]}),
    "root object": with_args({"root": {}}),
}


class TestMcpInvalidParams(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = make_repo()

    def serve_lines(self, *requests):
        stdin = io.BytesIO("".join(
            (r if isinstance(r, str) else json.dumps(r)) + "\n"
            for r in requests).encode("utf-8"))
        stdout = io.BytesIO()
        self.assertEqual(mcp_mod.serve(self.root, stdin=stdin, stdout=stdout), 0)
        return [json.loads(line) for line in
                stdout.getvalue().decode("utf-8").splitlines() if line]

    def test_every_client_mistake_is_invalid_params(self):
        for name, params in BAD_PARAMS.items():
            with self.subTest(name):
                resp = mcp_mod.handle(tools_call(params, rid=7), self.root)
                self.assertEqual(resp["id"], 7)
                self.assertEqual(resp["error"]["code"], mcp_mod.INVALID_PARAMS)
                msg = resp["error"]["message"]
                self.assertNotIn("attribute", msg)     # no Python internals
                self.assertNotIn("object has no", msg)
                self.assertNotIn("Traceback", msg)
                self.assertNotIn("result", resp)

    def test_the_server_answers_each_one_and_keeps_running(self):
        lines = self.serve_lines(
            *[tools_call(p, rid=i) for i, p in enumerate(BAD_PARAMS.values())],
            {"jsonrpc": "2.0", "id": "after", "method": "ping"})
        self.assertEqual(len(lines), len(BAD_PARAMS) + 1)
        self.assertEqual({r["error"]["code"] for r in lines[:-1]},
                         {mcp_mod.INVALID_PARAMS})       # never -32603
        self.assertEqual([r["id"] for r in lines[:-1]],
                         list(range(len(BAD_PARAMS))))
        self.assertEqual(lines[-1], {"jsonrpc": "2.0", "id": "after",
                                     "result": {}})

    def test_names_the_argument_that_is_wrong(self):
        resp = mcp_mod.handle(tools_call(with_args({"since": 5})), self.root)
        self.assertIn("since", resp["error"]["message"])
        resp = mcp_mod.handle(tools_call(with_args({"all": "yes"})), self.root)
        self.assertIn("all", resp["error"]["message"])
        resp = mcp_mod.handle(tools_call({"arguments": {}}), self.root)
        self.assertIn("name", resp["error"]["message"])

    def test_good_calls_still_work(self):
        for arguments in ({}, None, {"since": "2026-10-01", "all": True},
                          {"since": None, "until": None, "author": None,
                           "all": None, "root": None},
                          {"since": "", "extra": [1, 2]}):
            with self.subTest(arguments):
                params = {"name": TOOL}
                if arguments is not None:
                    params["arguments"] = arguments
                resp = mcp_mod.handle(tools_call(params), self.root)
                self.assertNotIn("error", resp)
                self.assertFalse(resp["result"].get("isError"))
                self.assertIn("# Standup", resp["result"]["content"][0]["text"])

    def test_bad_values_of_good_types_are_tool_errors_not_protocol_errors(self):
        # a well-typed but impossible date, or a root outside the server's:
        # the request is fine, running it failed (MCP: isError result)
        for arguments in ({"since": "2026-13-45"},
                          {"root": tempfile.gettempdir()}):
            with self.subTest(arguments):
                resp = mcp_mod.handle(tools_call(with_args(arguments)),
                                      self.root)
                self.assertTrue(resp["result"]["isError"])

    def test_notifications_with_bad_params_are_never_answered(self):
        for name, params in BAD_PARAMS.items():
            with self.subTest(name):
                self.assertIsNone(
                    mcp_mod.handle(tools_call(params, rid=...), self.root))
        lines = self.serve_lines(
            *[tools_call(p, rid=...) for p in BAD_PARAMS.values()],
            {"jsonrpc": "2.0", "method": "notifications/cancelled",
             "params": "garbage"},
            {"jsonrpc": "2.0", "method": 5, "params": [1]},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(lines, [{"jsonrpc": "2.0", "id": 1, "result": {}}])

    def test_unparseable_and_absurd_input_does_not_kill_the_server(self):
        depth = 200_000
        bomb = "[" * depth + "]" * depth
        nested_id = "[" * 900 + "1" + "]" * 900
        lines = self.serve_lines(
            bomb,                                    # parse recursion
            '{"jsonrpc": "2.0", "id": ' + nested_id + ', "method": "ping"}',
            {"jsonrpc": "2.0", "id": [1], "method": "ping"},
            {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"},
            {"jsonrpc": "2.0", "id": True, "method": "ping"},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        codes = [r.get("error", {}).get("code") for r in lines[:-1]]
        self.assertEqual(codes[0], mcp_mod.PARSE_ERROR)
        # a 900-deep id parses or not depending on how deep the caller's
        # own stack is; either way it is refused, not echoed
        self.assertIn(codes[1], (mcp_mod.PARSE_ERROR, mcp_mod.INVALID_REQUEST))
        self.assertEqual(codes[2:], [mcp_mod.INVALID_REQUEST] * 3)
        self.assertTrue(all(r["id"] is None for r in lines[:-1]))
        self.assertEqual(lines[-1], {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_ids_that_json_rpc_allows_are_echoed(self):
        for rid in (0, 7, -1, 2.5, "abc", "", None):
            with self.subTest(rid):
                resp = mcp_mod.handle(
                    {"jsonrpc": "2.0", "id": rid, "method": "ping"}, self.root)
                self.assertEqual(resp, {"jsonrpc": "2.0", "id": rid,
                                        "result": {}})

    def test_real_process_survives_a_run_of_bad_requests(self):
        requests = [tools_call(p, rid=i) for i, p in
                    enumerate(list(BAD_PARAMS.values())[:12])]
        requests.append(tools_call(with_args({"since": "2026-10-01"}), rid=99))
        payload = "".join(json.dumps(r) + "\n" for r in requests).encode()
        proc = subprocess.run(
            [sys.executable, "-m", "standupguy", "--root", self.root, "mcp"],
            input=payload, capture_output=True, cwd=REPO, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, b"")
        lines = [json.loads(line) for line in
                 proc.stdout.decode("utf-8").splitlines() if line]
        self.assertEqual(len(lines), 13)
        self.assertEqual({r["error"]["code"] for r in lines[:-1]}, {-32602})
        self.assertIn("add login flow", lines[-1]["result"]["content"][0]["text"])


# ------------------------------------------------------------------ H-5


class VanishedReader(io.StringIO):
    """A stdout whose reader has already left, like a closed pipe:
    `write` (or only `flush`, once the buffer is full) raises EPIPE."""

    def __init__(self, fail_on="write"):
        super().__init__()
        self.fail_on = fail_on

    def write(self, text):
        if self.fail_on == "write":
            raise BrokenPipeError(32, "Broken pipe")
        return super().write(text)

    def flush(self):
        if self.fail_on == "flush":
            raise BrokenPipeError(32, "Broken pipe")


class TestBrokenPipe(unittest.TestCase):
    ARGV = ["--all", "--since", "2026-10-01"]

    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()

    def run_main(self, stdout, *argv):
        err = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(err):
            code = cli_mod.main(["--root", self.repo, *argv])
        return code, err.getvalue()

    def test_a_write_to_a_closed_pipe_exits_quietly(self):
        for argv in (self.ARGV, self.ARGV + ["--format", "json"],
                     self.ARGV + ["--week"], self.ARGV + ["--sprint"]):
            with self.subTest(argv):
                code, err = self.run_main(VanishedReader("write"), *argv)
                self.assertEqual(code, 1)
                self.assertEqual(err, "")

    def test_a_flush_to_a_closed_pipe_exits_quietly(self):
        # small output sits in the buffer; the pipe is only found out to
        # be closed when it is flushed — by Python, at exit, too late
        code, err = self.run_main(VanishedReader("flush"), *self.ARGV)
        self.assertEqual(code, 1)
        self.assertEqual(err, "")

    def test_a_working_pipe_is_untouched(self):
        out = io.StringIO()
        code, err = self.run_main(out, *self.ARGV)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("add login flow", out.getvalue())

    def closed_reader(self, *argv, stdin=None):
        """Run the real program with the read end of its stdout already
        closed -> (returncode, stdout, stderr)."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "standupguy", *argv],
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO)
        proc.stdout.close()                  # `| true`
        if stdin is not None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except OSError:
                pass
        err = proc.stderr.read()
        proc.stderr.close()
        rc = proc.wait(timeout=60)
        return rc, err

    def test_real_process_with_a_closed_pipe_prints_no_traceback(self):
        for argv in (["--root", self.repo, *self.ARGV],
                     ["--root", self.repo, *self.ARGV, "--format", "json"],
                     ["--root", self.repo, *self.ARGV, "--week"]):
            with self.subTest(argv):
                rc, err = self.closed_reader(*argv)
                self.assertEqual(err, b"", err.decode("utf-8", "replace"))
                self.assertEqual(rc, 1)

    def test_help_and_version_into_a_closed_pipe_stay_quiet(self):
        for argv in (["--help"], ["--version"]):
            with self.subTest(argv):
                rc, err = self.closed_reader(*argv)
                self.assertEqual(err, b"", err.decode("utf-8", "replace"))
                self.assertIn(rc, (0, 1))

    def test_real_mcp_process_survives_its_client_vanishing(self):
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        rc, err = self.closed_reader("--root", self.repo, "mcp",
                                     stdin=(request + "\n").encode())
        self.assertEqual(err, b"", err.decode("utf-8", "replace"))
        self.assertEqual(rc, 1)


class TestServeClientDisconnect(unittest.TestCase):
    def test_a_client_hanging_up_mid_response_is_silent(self):
        server = serve_mod.make_server(make_repo(), "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        started, client_gone, finished = (threading.Event() for _ in range(3))

        def big_payload_after_the_client_left(*args, **kwargs):
            started.set()
            client_gone.wait(10)
            return 200, "text/plain", "x" * (16 * 1024 * 1024)

        real_shutdown_request = server.shutdown_request

        def shutdown_request(request):        # the very last thing it does
            real_shutdown_request(request)
            finished.set()

        server.shutdown_request = shutdown_request
        err = io.StringIO()
        with mock.patch.object(serve_mod, "_payload",
                               big_payload_after_the_client_left), \
                contextlib.redirect_stderr(err):
            client = socket.create_connection(("127.0.0.1", port), timeout=10)
            client.sendall(b"GET /digest HTTP/1.0\r\n\r\n")
            self.assertTrue(started.wait(10))
            client.close()                    # gone before the answer
            client_gone.set()
            self.assertTrue(finished.wait(10))
        self.assertEqual(err.getvalue(), "")

    def test_the_server_keeps_serving_after_a_hang_up(self):
        server = serve_mod.make_server(make_repo(), "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for _ in range(3):                    # connect, say nothing, leave
            socket.create_connection(("127.0.0.1", port), timeout=10).close()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=10) as resp:
            self.assertEqual(json.loads(resp.read()), {"ok": True})


# ------------------------------------------------------------------ H-6


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class TestPyprojectMetadata(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
            self.pp = tomllib.load(f)
        self.project = self.pp["project"]

    def test_license_is_an_spdx_string_and_the_file_is_mit(self):
        self.assertEqual(self.project["license"], "MIT")
        self.assertEqual(self.project["license-files"], ["LICENSE"])
        self.assertEqual(self.pp["build-system"]["requires"],
                         ["setuptools>=77"])    # first to read PEP 639
        with open(os.path.join(REPO, "LICENSE"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("MIT License"))
        self.assertIn("Permission is hereby granted, free of charge", text)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", text)

    def test_classifiers(self):
        self.assertEqual(self.project["classifiers"], [
            "Development Status :: 4 - Beta",
            "Environment :: Console",
            "Intended Audience :: Developers",
            "Operating System :: OS Independent",
            "Programming Language :: Python :: 3",
            "Programming Language :: Python :: 3.10",
            "Programming Language :: Python :: 3.11",
            "Programming Language :: Python :: 3.12",
            "Programming Language :: Python :: 3.13",
            "Topic :: Software Development :: Version Control :: Git",
        ])
        self.assertEqual(self.project["requires-python"], ">=3.10")

    def test_keywords_and_urls(self):
        self.assertTrue(len(self.project["keywords"]) >= 5)
        self.assertIn("git", self.project["keywords"])
        self.assertIn("standup", self.project["keywords"])
        self.assertEqual(self.project["urls"], {
            "Homepage": "https://github.com/calasdan-sketch/standupguy",
            "Issues": "https://github.com/calasdan-sketch/standupguy/issues",
        })


if __name__ == "__main__":
    unittest.main()
