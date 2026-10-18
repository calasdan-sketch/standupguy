"""Panel round 4 regressions.

G-1  MCP stdin/stdout are UTF-8 bytes, not the console code page.
G-2  Slack push escapes & < > so no <@user>/<!subteam>/<url|text> survives.
G-3  MCP `root` argument cannot point the server at any repo on disk.
G-4  `|` in an author or subject, and paths starting "COMMIT:", parse.
G-5  `feat!:` / `feat(api)!:` are Features, not "- : ...".
G-6  Impossible ISO dates are refused; unknown date words get a warning.
G-7  A bare --until date is inclusive.
G-8  Non-ASCII paths in `git status` are not octal-escaped.
G-9  Missing git -> clean "git not found" exit 2.
G-10 "Draft the release notes" is not WIP; tags still are.
G-11 --week honours --until.
G-12 serve defaults to your git user, JSON has email/wip, shape shared.
G-13 Notifications are never answered; non-objects get -32600.
G-14 No user.email -> one-line notice.
Product: --root is repeatable (one section per repo).
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from test_standupguy import commit, git, make_repo  # isolates git/HOME

from standupguy import cli as cli_mod
from standupguy import digest as digest_mod
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


def rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method,
            "params": params or {}}


def serve_lines(root, *lines):
    stdin = io.BytesIO("".join(line + "\n" for line in lines).encode("utf-8"))
    stdout = io.BytesIO()
    mcp_mod.serve(root, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in
            stdout.getvalue().decode("utf-8").splitlines() if line]


# ------------------------------------------------------------------ G-1


class TestMcpWireEncoding(unittest.TestCase):
    def test_non_cp1252_author_survives_a_cp1252_console(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        commit(tmp, "Ξένος", "x@example.com", "2026-10-14T10:00:00",
               "feat: ελληνικά", {"a.py": "1"})
        commit(tmp, "Łukasz", "l@example.com", "2026-10-14T11:00:00",
               "fix: polski", {"b.py": "2"})
        requests = [
            rpc("initialize"),
            rpc("tools/call", {"name": "what_did_i_ship",
                               "arguments": {"since": "2026-10-01",
                                             "author": "Łukasz"}}, 2),
            rpc("tools/call", {"name": "what_did_i_ship",
                               "arguments": {"since": "2026-10-01",
                                             "author": "Ξένος"}}, 3),
        ]
        payload = "".join(json.dumps(r, ensure_ascii=False) + "\n"
                          for r in requests).encode("utf-8")
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        proc = subprocess.run(
            [sys.executable, "-m", "standupguy", "--root", tmp, "mcp"],
            input=payload, capture_output=True, cwd=REPO, env=env,
            timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"UnicodeDecodeError", proc.stderr)
        lines = [json.loads(line) for line in
                 proc.stdout.decode("utf-8").splitlines() if line]
        self.assertEqual([r["id"] for r in lines], [1, 2, 3])
        polish = lines[1]["result"]["content"][0]["text"]
        greek = lines[2]["result"]["content"][0]["text"]
        self.assertIn("polski", polish)
        self.assertNotIn("ελληνικά", polish)    # the filter decoded right
        self.assertIn("ελληνικά", greek)
        self.assertNotIn("polski", greek)
        self.assertTrue(proc.stdout.isascii())  # JSON-escaped: safe anywhere


# ------------------------------------------------------------------ G-2


class TestSlackMarkupEscaped(unittest.TestCase):
    PAYLOADS = ("<@U024BE7LH>", "<!subteam^S0123|@devs>",
                "<http://evil.example|the standup>")

    def test_user_group_and_link_markup_cannot_survive(self):
        for raw in self.PAYLOADS:
            out = push_mod.sanitize_slack_text(f"feat: {raw} done")
            self.assertNotIn("<", out, raw)
            self.assertNotIn(">", out, raw)
            self.assertIn("&lt;", out)
            self.assertIn("&gt;", out)
        self.assertEqual(push_mod.sanitize_slack_text("<@U024BE7LH>"),
                         "&lt;@U024BE7LH&gt;")

    def test_ampersand_escaped_first_no_double_escaping(self):
        self.assertEqual(push_mod.sanitize_slack_text("a & b"), "a &amp; b")
        self.assertEqual(push_mod.sanitize_slack_text("&lt;"), "&amp;lt;")
        self.assertEqual(push_mod.escape_slack_markup("<a&b>"),
                         "&lt;a&amp;b&gt;")

    def test_mass_mentions_still_readable_and_dead(self):
        out = push_mod.sanitize_slack_text("<!channel> and @here")
        self.assertEqual(out, "[at]channel and [at]here")

    def test_push_body_is_escaped(self):
        captured = {}

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=30):
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()

        with mock.patch.object(push_mod.urllib.request, "urlopen", fake_urlopen):
            push_mod.push_slack("https://hooks.slack.com/services/T/x",
                                "- ping <@U024BE7LH> & <!subteam^S1|@devs>")
        text = captured["body"]["text"]
        self.assertNotIn("<", text)
        self.assertIn("&lt;@U024BE7LH&gt;", text)
        self.assertIn("&amp;", text)


# ------------------------------------------------------------------ G-3


class TestMcpRootConfinement(unittest.TestCase):
    def call(self, server_root, args):
        return mcp_mod.handle(
            rpc("tools/call", {"name": "what_did_i_ship",
                               "arguments": args}), server_root)

    def test_root_outside_the_configured_root_is_refused(self):
        inside, outside = make_repo(), make_repo()
        resp = self.call(inside, {"since": "2026-10-01", "root": outside})
        self.assertTrue(resp["result"].get("isError"))
        text = resp["result"]["content"][0]["text"]
        self.assertIn("outside the configured root", text)
        self.assertNotIn("add login flow", text)
        # `..` cannot climb out either
        sneaky = os.path.join(inside, "..", os.path.basename(outside))
        resp = self.call(inside, {"since": "2026-10-01", "root": sneaky})
        self.assertTrue(resp["result"].get("isError"))

    def test_root_inside_is_fine(self):
        repo = make_repo()
        sub = os.path.join(repo, "app")
        for r in (repo, sub):
            resp = self.call(repo, {"since": "2026-10-01", "root": r,
                                    "all": True})
            self.assertFalse(resp["result"].get("isError"), r)
            self.assertIn("add login flow", resp["result"]["content"][0]["text"])

    def test_resolve_root_unit(self):
        a, b = make_repo(), make_repo()
        self.assertEqual(mcp_mod.resolve_root(None, [a, b]), [a, b])
        self.assertEqual(mcp_mod.resolve_root(b, [a, b]), [b])
        with self.assertRaises(ValueError):
            mcp_mod.resolve_root(tempfile.gettempdir(), [a])

    def test_mcp_default_author_matches_cli(self):
        repo = make_repo()  # user.email dan@example.com; Teammate also commits
        text = self.call(repo, {"since": "2026-10-01"})["result"]["content"][0]["text"]
        self.assertIn("add login flow", text)
        self.assertNotIn("update readme", text)       # Teammate filtered out
        text = self.call(repo, {"since": "2026-10-01", "all": True})["result"]["content"][0]["text"]
        self.assertIn("update readme", text)


# ------------------------------------------------------------------ G-4


class TestLogParsing(unittest.TestCase):
    def test_pipe_in_author_and_subject(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        commit(tmp, "Pipe|Name", "pipe|@example.com", "2026-10-14T10:00:00",
               "feat: a|b (c|d)", {"x.py": "1", "y.py": "2"})
        cs = gitlog_mod.commits_since(tmp, "2026-10-01")
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual((c.author, c.email, c.subject),
                         ("Pipe|Name", "pipe|@example.com", "feat: a|b (c|d)"))
        self.assertEqual(sorted(c.files), ["x.py", "y.py"])
        self.assertEqual(c.date, "2026-10-14")
        self.assertEqual(digest_mod.classify(c.subject), ("feat", "a|b (c|d)"))

    def test_parse_log_path_starting_with_commit_prefix(self):
        # crafted `git log -z` output: a path named "COMMIT:notes.txt" used
        # to be mistaken for a commit header and blew up with ValueError
        out = ("\x1eabc123\x1fDan\x1fd@e.c\x1f2026-10-14\x1ffeat: x\n"
               "COMMIT:notes.txt\0src/COMMIT:a|b.py\0\0"
               "\x1edef456\x1fPipe|Name\x1fp@e.c\x1f2026-10-13\x1fchore: y|z\0"
               "\x1e0123456\x1fA\x1fa@e.c\x1f2026-10-12\x1f"
               "Merge pull request #7 from x/y\nREADME.md\0")
        cs = gitlog_mod.parse_log(out)
        self.assertEqual([c.sha for c in cs], ["abc123", "def456", "0123456"])
        self.assertEqual(cs[0].files, ["COMMIT:notes.txt", "src/COMMIT:a|b.py"])
        self.assertEqual(cs[1].files, [])
        self.assertEqual(cs[1].author, "Pipe|Name")
        self.assertEqual(cs[1].subject, "chore: y|z")
        self.assertEqual(cs[2].pr_number, 7)
        self.assertEqual(cs[2].files, ["README.md"])
        self.assertEqual(gitlog_mod.parse_log(""), [])

    @unittest.skipIf(os.name == "nt", "':' is not allowed in Windows paths")
    def test_real_path_starting_with_commit_prefix(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        commit(tmp, "D", "d@e.c", "2026-10-14T10:00:00", "feat: x",
               {"COMMIT:notes.txt": "1"})
        cs = gitlog_mod.commits_since(tmp, "2026-10-01")
        self.assertEqual([c.files for c in cs], [["COMMIT:notes.txt"]])

    def test_non_ascii_file_names_are_raw(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        commit(tmp, "D", "d@e.c", "2026-10-14T10:00:00", "feat: x",
               {"café.py": "1", "has space.py": "2"})
        cs = gitlog_mod.commits_since(tmp, "2026-10-01")
        self.assertEqual(sorted(cs[0].files), ["café.py", "has space.py"])


# ------------------------------------------------------------------ G-5


class TestBreakingChangePrefix(unittest.TestCase):
    def test_bang_is_part_of_the_prefix(self):
        self.assertEqual(digest_mod.classify("feat!: drop v1"),
                         ("feat", "drop v1"))
        self.assertEqual(digest_mod.classify("feat(api)!: drop v1"),
                         ("feat", "drop v1"))
        self.assertEqual(digest_mod.classify("fix(core): keep"),
                         ("fix", "keep"))
        self.assertEqual(digest_mod.classify("Fix: spaced  out"),
                         ("fix", "spaced  out"))
        self.assertEqual(digest_mod.classify("random words"),
                         ("other", "random words"))
        self.assertEqual(digest_mod.classify("feat! not a prefix"),
                         ("other", "feat! not a prefix"))

    def test_digest_line_has_no_leading_colon(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        commit(tmp, "D", "d@e.c", "2026-10-14T10:00:00",
               "feat(api)!: drop v1 endpoints", {"a": "1"})
        md = digest_mod.digest_markdown(tmp, "2026-10-01", "x")
        self.assertIn("### Features", md)
        self.assertIn("- drop v1 endpoints", md)
        self.assertNotIn("- : ", md)


# ------------------------------------------------------------------ G-6


class TestDateValidation(unittest.TestCase):
    def test_impossible_iso_dates_raise(self):
        for bad in ("2026-13-45", "2026-02-30", "2026-00-01", "2026-13-45 10:00"):
            with self.assertRaises(ValueError, msg=bad):
                gitlog_mod.check_date(bad)

    def test_good_values_pass_silently(self):
        for good in ("2026-10-14", "2026-1-5", "2026-10-14T10:00",
                     "2026-10-14 10:00", "yesterday", "today", "3 days ago",
                     "last monday", "2 weeks ago", "noon", "5pm",
                     "@1792128212", "Oct 14", "last fri", "", None):
            self.assertIsNone(gitlog_mod.check_date(good), good)

    def test_unknown_words_warn(self):
        for odd in ("notadate", "foo bar", "yesteryear"):
            warning = gitlog_mod.check_date(odd)
            self.assertIsNotNone(warning, odd)
            self.assertIn("git will guess", warning)

    def test_cli_refuses_bad_date_and_warns_on_nonsense(self):
        tmp = make_repo()
        code, out, err = run_cli("--root", tmp, "--since", "2026-13-45")
        self.assertEqual(code, 2)
        self.assertIn("--since", err)
        self.assertIn("not a valid date", err)
        self.assertEqual(out, "")
        code, out, err = run_cli("--root", tmp, "--since", "2026-10-01",
                                 "--until", "2026-02-30")
        self.assertEqual(code, 2)
        self.assertIn("--until", err)
        code, out, err = run_cli("--root", tmp, "--since", "notadate")
        self.assertEqual(code, 0)
        self.assertIn("warning: --since", err)
        self.assertIn("notadate", err)


# ------------------------------------------------------------------ G-7


class TestInclusiveUntil(unittest.TestCase):
    def test_end_of_day_appended_for_bare_until(self):
        f = gitlog_mod._since_to_arg
        self.assertEqual(f("2026-10-15", end=True), "2026-10-15 23:59:59")
        self.assertEqual(f("last monday", end=True), "last monday 23:59:59")
        self.assertEqual(f("2026-10-15 12:00", end=True), "2026-10-15 12:00")
        self.assertEqual(f("now", end=True), "now")
        self.assertEqual(f("2026-10-15"), "2026-10-15 00:00")

    def test_cli_until_includes_the_whole_day(self):
        tmp = make_repo()  # commits on 10-10 and 10-14 (10:00, 15:00, 16:00)
        code, out, _ = run_cli("--root", tmp, "--all", "--since", "2026-10-01",
                               "--until", "2026-10-14", "--format", "json")
        self.assertEqual(code, 0)
        subjects = {c["subject"] for c in json.loads(out)["commits"]}
        self.assertIn("fix: null pointer on logout", subjects)  # 15:00
        self.assertIn("docs: update readme", subjects)          # 16:00
        self.assertEqual(len(subjects), 4)


# ------------------------------------------------------------------ G-8


class TestUncommittedPaths(unittest.TestCase):
    def test_non_ascii_and_spaces_and_renames(self):
        tmp = make_repo()
        with open(os.path.join(tmp, "café.py"), "w", encoding="utf-8") as f:
            f.write("x")
        with open(os.path.join(tmp, "has space.py"), "w", encoding="utf-8") as f:
            f.write("x")
        git(tmp, "mv", "req.txt", "moved req.txt")
        paths = gitlog_mod.uncommitted(tmp)
        self.assertIn("café.py", paths)
        self.assertIn("has space.py", paths)
        self.assertIn("moved req.txt", paths)
        self.assertNotIn("req.txt", paths)          # the rename's old name
        self.assertFalse(any("\\303" in p or '"' in p for p in paths), paths)
        md = digest_mod.digest_markdown(tmp, "2026-10-14", "x")
        self.assertIn("- `café.py`", md)


# ------------------------------------------------------------------ G-9


class TestGitMissing(unittest.TestCase):
    def test_clean_exit_when_git_is_not_on_path(self):
        tmp = make_repo()
        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"PATH": empty}):
            code, out, err = run_cli("--root", tmp, "--since", "2026-10-01")
        self.assertEqual(code, 2)
        self.assertIn("git not found", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")

    def test_gitlog_raises_a_named_error(self):
        with mock.patch.object(gitlog_mod.subprocess, "run",
                               side_effect=FileNotFoundError(2, "no", "git")):
            with self.assertRaises(gitlog_mod.GitNotFound):
                gitlog_mod.commits_since(".", "yesterday")
            with self.assertRaises(gitlog_mod.GitNotFound):
                gitlog_mod.default_author(".")


# ----------------------------------------------------------------- G-10


class TestWipDetection(unittest.TestCase):
    def test_only_tags_count(self):
        for s in ("wip: x", "WIP x", "WIP", "[WIP] x", "feat: [wip] y",
                  "Draft: z", "draft : z", "[draft] z", "(draft) z",
                  "feat: wip: half"):
            self.assertTrue(digest_mod.is_wip(s), s)
        for s in ("Draft the release notes for v2", "docs: draft guidelines",
                  "feat: drafts module", "fix: wipe cache",
                  "feat: swipe left", "docs: delta", "Drafted the plan"):
            self.assertFalse(digest_mod.is_wip(s), s)


# ----------------------------------------------------------------- G-11


class TestWeekUntil(unittest.TestCase):
    def test_week_honours_until(self):
        tmp = make_repo()  # 10-10 and 10-14
        code, out, _ = run_cli("--root", tmp, "--all", "--week",
                               "--since", "2026-10-01", "--until", "2026-10-12")
        self.assertEqual(code, 0)
        self.assertIn("## 2026-10-10", out)
        self.assertNotIn("## 2026-10-14", out)
        self.assertIn("2026-10-01 → 2026-10-12", out)
        md = digest_mod.weekly_markdown(tmp, None, "2026-10-01", "2026-10-12")
        self.assertNotIn("2026-10-14", md)


# ----------------------------------------------------------------- G-12


class TestServeShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = make_repo()
        cls.server = serve_mod.make_server(cls.root, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_default_author_is_the_repo_user_like_the_cli(self):
        _, body = self.get("/digest?since=2026-10-01")
        authors = {c["author"] for c in json.loads(body)["commits"]}
        self.assertEqual(authors, {"Dan"})
        _, body = self.get("/digest?since=2026-10-01&all=1")
        authors = {c["author"] for c in json.loads(body)["commits"]}
        self.assertEqual(authors, {"Dan", "Teammate"})
        _, body = self.get("/week?since=2026-10-01&all=true")
        self.assertIn("Teammate", {c["author"] for c in json.loads(body)["commits"]})

    def test_json_has_email_and_wip_and_matches_cli(self):
        _, body = self.get("/digest?since=2026-10-01&all=1")
        data = json.loads(body)
        for c in data["commits"]:
            self.assertEqual(set(c), {"sha", "author", "email", "date",
                                      "subject", "files", "pr", "wip"})
        self.assertIn("markdown", data)
        code, out, _ = run_cli("--root", self.root, "--all", "--since",
                               "2026-10-01", "--format", "json")
        cli = json.loads(out)
        self.assertEqual(cli["commits"], data["commits"])
        self.assertEqual(cli["in_progress"], data["in_progress"])
        self.assertEqual(cli["since"], data["since"])

    def test_slack_text_is_sanitized_for_workflows(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        commit(tmp, "D", "d@e.c", "2026-10-14T10:00:00",
               "feat: ping <@U1> & <!subteam^S1|@devs>", {"a": "1"})
        server = serve_mod.make_server(tmp, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}"
                    "/digest?since=2026-10-01&all=1", timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            serve_mod.Handler.root = self.root
        self.assertIn("<@U1>", data["markdown"])          # raw for others
        self.assertNotIn("<", data["slack_text"])         # dead for Slack
        self.assertIn("&lt;@U1&gt; &amp; &lt;!subteam^S1|@devs&gt;",
                      data["slack_text"])

    def test_bad_date_is_a_400_not_a_500(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/digest?since=2026-13-45")
        self.assertEqual(cm.exception.code, 400)
        self.assertIn("not a valid date", cm.exception.read().decode())

    def test_week_until(self):
        _, body = self.get("/week?since=2026-10-01&until=2026-10-12&all=1")
        self.assertEqual([c["subject"] for c in json.loads(body)["commits"]],
                         ["chore: bump deps"])


# ----------------------------------------------------------------- G-13


class TestJsonRpcHygiene(unittest.TestCase):
    def test_notifications_are_never_answered(self):
        root = make_repo()
        for req in ({"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": 1}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"},
                    {"jsonrpc": "2.0", "method": "tools/list"},   # no id
                    {"jsonrpc": "2.0", "id": 9,
                     "method": "notifications/cancelled"}):
            self.assertIsNone(mcp_mod.handle(req, root), req)
        lines = serve_lines(root,
                            json.dumps({"jsonrpc": "2.0",
                                        "method": "notifications/cancelled"}),
                            json.dumps(rpc("ping", rid=5)))
        self.assertEqual([r["id"] for r in lines], [5])

    def test_non_object_requests_get_invalid_request(self):
        root = make_repo()
        lines = serve_lines(root, "[1, 2]", '"str"', "42", "not json at all",
                            json.dumps(rpc("ping", rid=7)))
        codes = [r.get("error", {}).get("code") for r in lines[:-1]]
        self.assertEqual(codes, [-32600, -32600, -32600, -32700])
        for r in lines[:-1]:
            self.assertIsNone(r["id"])
            self.assertNotIn("object has no attribute", r["error"]["message"])
        self.assertEqual(lines[-1], {"jsonrpc": "2.0", "id": 7, "result": {}})
        self.assertEqual(mcp_mod.handle({"jsonrpc": "2.0", "id": 1}, root)
                         ["error"]["code"], -32600)
        self.assertEqual(mcp_mod.handle(rpc("nope"), root)["error"]["code"],
                         -32601)
        self.assertEqual(mcp_mod.handle(
            rpc("tools/call", {"name": "other"}), root)["error"]["code"], -32602)


# ----------------------------------------------------------------- G-14


class TestNoUserEmailNotice(unittest.TestCase):
    def test_notice_once_and_everyone_shown(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        commit(tmp, "A", "a@e.c", "2026-10-14T10:00:00", "feat: a", {"a": "1"})
        commit(tmp, "B", "b@e.c", "2026-10-14T11:00:00", "fix: b", {"b": "1"})
        code, out, err = run_cli("--root", tmp, "--since", "2026-10-01")
        self.assertEqual(code, 0)
        self.assertIn("no git user.email", err)
        self.assertEqual(err.count("no git user.email"), 1)
        self.assertIn("- a", out)
        self.assertIn("- b", out)
        # explicit --all / --author: no notice
        _, _, err = run_cli("--root", tmp, "--since", "2026-10-01", "--all")
        self.assertEqual(err, "")
        _, _, err = run_cli("--root", tmp, "--since", "2026-10-01",
                            "--author", "a@e.c")
        self.assertEqual(err, "")


# ------------------------------------------------------------ multi-root


class TestMultiRoot(unittest.TestCase):
    def make_second(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        git(tmp, "config", "user.email", "dan@example.com")
        commit(tmp, "Dan", "dan@example.com", "2026-10-14T12:00:00",
               "feat: second repo thing", {"z.py": "1"})
        return tmp

    def test_one_section_per_repo_in_order(self):
        a, b = make_repo(), self.make_second()
        code, out, err = run_cli("--root", a, "--root", b, "--since", "2026-10-01")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("# Standup — "), 2)
        self.assertLess(out.index(f"# Standup — {os.path.basename(a)}"),
                        out.index(f"# Standup — {os.path.basename(b)}"))
        self.assertIn("add login flow", out)
        self.assertIn("second repo thing", out)
        # sprint and week modes too
        code, out, _ = run_cli("--root", a, "--root", b, "--sprint",
                               "--since", "2026-10-01")
        self.assertEqual(out.count("# Sprint review — "), 2)
        code, out, _ = run_cli("--root", a, "--root", b, "--week",
                               "--since", "2026-10-01")
        self.assertEqual(out.count("# Weekly summary — "), 2)

    def test_json_lists_repos_only_when_several(self):
        a, b = make_repo(), self.make_second()
        _, out, _ = run_cli("--root", a, "--root", b, "--since", "2026-10-01",
                            "--format", "json")
        data = json.loads(out)
        self.assertEqual([r["repo"] for r in data["repos"]],
                         [os.path.basename(a), os.path.basename(b)])
        _, out, _ = run_cli("--root", a, "--since", "2026-10-01",
                            "--format", "json")
        self.assertNotIn("repos", json.loads(out))   # single root: unchanged

    def test_not_a_repo_among_roots_is_reported(self):
        a = make_repo()
        code, _, err = run_cli("--root", a, "--root", tempfile.mkdtemp())
        self.assertEqual(code, 2)
        self.assertIn("not a git repo", err)

    def test_mcp_serves_every_root_by_default(self):
        a, b = make_repo(), self.make_second()
        resp = mcp_mod.handle(
            rpc("tools/call", {"name": "what_did_i_ship",
                               "arguments": {"since": "2026-10-01"}}), [a, b])
        text = resp["result"]["content"][0]["text"]
        self.assertIn("add login flow", text)
        self.assertIn("second repo thing", text)
        self.assertEqual(text.count("# Standup — "), 2)


# ------------------------------------------------------------------ docs


class TestReadme(unittest.TestCase):
    def test_install_and_platform_wording(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("alias standupguy", text)
        self.assertNotIn("git clone <repo>", text)
        self.assertIn("pip install git+https://github.com/calasdan-sketch/standupguy",
                      text)
        self.assertNotIn("python3", text)          # Windows: python / py
        self.assertNotIn("can never ping the channel or inject payloads", text)
        self.assertIn("&lt;", text)                 # the escaping is documented
        self.assertIn("inclusive", text)            # --until semantics
        self.assertIn("--root", text)


if __name__ == "__main__":
    unittest.main()
