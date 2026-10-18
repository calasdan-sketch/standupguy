"""Repositories git will not read to the end, and a closed standard output.

Panel finding SG-6:
- a repo owned by another user was reported as "not a git repo", which hides
  git's own hint (the `safe.directory` command that fixes it);
- a bare repository ended in a traceback (`git status` needs a work tree), and
  `serve` answered 500 to every request for it;
- `standupguy >&-` (standard output closed) ended in an AttributeError.
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
import urllib.request
from unittest import mock

from test_round5 import run_cli
from test_standupguy import make_repo  # also isolates git config/HOME

from standupguy import cli as cli_mod
from standupguy import gitlog as gitlog_mod
from standupguy import mcp as mcp_mod
from standupguy import push as push_mod
from standupguy import serve as serve_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PING = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n").encode()


def rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def git_says(stderr, code=128):
    """Every git command fails with this message."""
    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, code, stdout=b"",
                                           stderr=stderr.encode("utf-8"))
    return mock.patch.object(gitlog_mod.subprocess, "run", fake_run)


def bare_clone(repo, name="project.git"):
    bare = os.path.join(tempfile.mkdtemp(), name)
    subprocess.run(["git", "clone", "-q", "--bare", repo, bare], check=True,
                   capture_output=True)
    return bare


class TestWhyGitRefusedARepo(unittest.TestCase):
    PATH = os.path.join(os.sep, "srv", "work", "api")
    DUBIOUS = ("fatal: detected dubious ownership in repository at '{p}'\n"
               "To add an exception for this directory, call:\n\n"
               "\tgit config --global --add safe.directory {p}\n")

    def test_gits_own_hint_is_shown_not_not_a_git_repo(self):
        said = self.DUBIOUS.format(p=self.PATH)
        for argv in ((), ("--format", "json"), ("mcp",), ("serve", "--port", "0")):
            with self.subTest(argv), git_says(said), mock.patch.object(
                    serve_mod.ThreadedBridgeServer, "serve_forever",
                    side_effect=AssertionError("started")):
                code, out, err = run_cli("--root", self.PATH, *argv)
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("error: "), err)
            self.assertIn("dubious ownership", err)
            self.assertIn(
                f"git config --global --add safe.directory {self.PATH}", err)
            self.assertNotIn("not a git repo", err)

    def test_a_folder_that_is_just_not_a_repository_keeps_its_short_message(self):
        with git_says("fatal: not a git repository (or any of the parent "
                      "directories): .git\n"):
            code, out, err = run_cli("--root", self.PATH)
        self.assertEqual((code, out, err),
                         (2, "", f"error: not a git repo: {self.PATH}\n"))

    def test_git_failing_without_a_word_is_not_a_repo_either(self):
        with git_says(""):
            code, out, err = run_cli("--root", self.PATH)
        self.assertEqual((code, out, err),
                         (2, "", f"error: not a git repo: {self.PATH}\n"))

    def test_what_git_said_is_shown_as_one_printable_line(self):
        said = "fatal: odd\n\tthing\x1b[2J   " + "x" * 3000 + "\n"
        with git_says(said):
            code, out, err = run_cli("--root", self.PATH)
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertNotIn("\x1b", err)
        self.assertIn("fatal: odd thing", err)
        self.assertLess(len(err), 900)

    def test_a_folder_that_does_not_exist_is_told_as_git_tells_it(self):
        gone = os.path.join(tempfile.mkdtemp(), "missing")
        code, out, err = run_cli("--root", gone)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith(f"error: git cannot use {gone}: "), err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid")
                         and os.geteuid() == 0,
                         "needs root, to give a folder to another user")
    def test_a_repo_owned_by_another_user_for_real(self):
        repo = make_repo()
        try:
            for here, dirs, files in os.walk(repo):
                for name in (here, *(os.path.join(here, f) for f in files)):
                    os.chown(name, 4242, 4242)
        except OSError as e:
            self.skipTest(f"cannot give files away here: {e}")
        with mock.patch.dict(os.environ):
            os.environ.pop("SUDO_UID", None)
            code, out, err = run_cli("--root", repo)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertIn("dubious ownership", err)
        self.assertIn("safe.directory", err)
        self.assertNotIn("not a git repo", err)


class TestABareRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = make_repo()
        cls.bare = bare_clone(cls.src)

    def test_nothing_is_uncommitted_where_there_is_no_work_tree(self):
        self.assertEqual(gitlog_mod.uncommitted(self.bare), [])
        # the folder it was cloned from still has its untracked file
        self.assertIn("wip.py", gitlog_mod.uncommitted(self.src))

    def test_it_is_named_after_its_folder(self):
        self.assertEqual(gitlog_mod.repo_name(self.bare), "project")
        self.assertEqual(gitlog_mod.repo_name(self.src),
                         os.path.basename(self.src))
        # standing inside a repo's own .git folder
        self.assertEqual(gitlog_mod.repo_name(os.path.join(self.src, ".git")),
                         os.path.basename(self.src))
        # a bare repo that is not called *.git keeps its name as it is
        plain = bare_clone(self.src, "mirror")
        self.assertEqual(gitlog_mod.repo_name(plain), "mirror")

    def test_the_digest_works_in_every_mode(self):
        for extra in ((), ("--week",), ("--sprint",)):
            with self.subTest(extra):
                code, out, err = run_cli("--root", self.bare, "--all",
                                         "--since", "2026-10-01", *extra)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("project", out)
                self.assertIn("add login flow", out)

    def test_the_json(self):
        code, out, err = run_cli("--root", self.bare, "--all", "--since",
                                 "2026-10-01", "--format", "json")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual((data["repo"], data["in_progress"]), ("project", []))
        self.assertEqual(len(data["commits"]), 4)

    def test_serve_answers_instead_of_failing_every_request(self):
        server = serve_mod.make_server(self.bare, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = (f"http://127.0.0.1:{server.server_address[1]}"
               "/digest?all=1&since=2026-10-01&format=json")
        with urllib.request.urlopen(url, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["in_progress"], [])
        self.assertEqual(len(data["commits"]), 4)

    def test_mcp_answers(self):
        resp = mcp_mod.handle(
            rpc("tools/call", {"name": "what_did_i_ship",
                               "arguments": {"since": "2026-10-01",
                                             "all": True}}), [self.bare])
        result = resp["result"]
        self.assertFalse(result.get("isError"), result)
        self.assertIn("add login flow", result["content"][0]["text"])


def run_closed(*argv):
    """main() with sys.stdout None, which is what Python has when the process
    was started with standard output closed (`command >&-`; pythonw on Windows)."""
    err = io.StringIO()
    with mock.patch.object(sys, "stdout", None), contextlib.redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, err.getvalue()


class TestStandardOutputClosed(unittest.TestCase):
    def assertSaidSoOnce(self, code, err):
        self.assertEqual(code, 1, err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn("standard output is closed", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("AttributeError", err)

    def test_the_digest_and_the_json_have_nowhere_to_go(self):
        repo = make_repo()
        for extra in ((), ("--format", "json"), ("--week",), ("--sprint",)):
            with self.subTest(extra):
                code, err = run_closed("--root", repo, "--since", "2026-10-14",
                                       *extra)
                self.assertSaidSoOnce(code, err)

    def test_mcp_cannot_answer(self):
        stdin = io.TextIOWrapper(io.BytesIO(PING), encoding="utf-8")
        with mock.patch.object(sys, "stdin", stdin):
            code, err = run_closed("--root", make_repo(), "mcp")
        self.assertSaidSoOnce(code, err)
        self.assertEqual(stdin.buffer.tell(), 0, "it read a request first")

    def test_the_slack_push_does_not_need_it(self):
        posted = []

        class Answer:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=30):      # no network in a test
            posted.append(req.full_url)
            return Answer()

        hook = "https://hooks.slack.com/services/T0000/B0000/X"
        with mock.patch.object(push_mod.urllib.request, "urlopen", fake_urlopen):
            code, err = run_closed("--root", make_repo(), "--since",
                                   "2026-10-14", "--push-slack", hook)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(posted, [hook])

    def test_serve_does_not_need_it_for_its_banner(self):
        with mock.patch.object(serve_mod.ThreadedBridgeServer, "serve_forever",
                               return_value=None):
            code, err = run_closed("--root", make_repo(), "serve", "--port", "0")
        self.assertEqual((code, err), (0, ""))

    @unittest.skipIf(os.name == "nt", "there is no >&- on Windows; the "
                                      "tests above stand in for it")
    def test_in_a_real_process(self):
        repo = make_repo()
        env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
        env["PYTHONPATH"] = REPO
        for extra in ((), ("--format", "json")):
            with self.subTest(extra):
                proc = subprocess.run(
                    ["sh", "-c", 'exec "$@" >&-', "sh", sys.executable, "-m",
                     "standupguy", "--root", repo, "--since", "2026-10-14",
                     *extra], capture_output=True, env=env, cwd=REPO,
                    timeout=60)
                err = proc.stderr.decode("utf-8", "replace")
                self.assertEqual(proc.stdout, b"")
                self.assertSaidSoOnce(proc.returncode, err)


class TestReadmeSaysSo(unittest.TestCase):
    def test_the_paragraphs(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            flat = " ".join(f.read().split())
        for phrase in ("safe.directory", "bare repository",
                       "standard output is closed"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")


if __name__ == "__main__":
    unittest.main()
