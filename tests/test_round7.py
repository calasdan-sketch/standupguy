"""Review round 7 regressions: the LOW items the third panel left open.

S1  A weekday inside a whole date (git's own `Date:` format, RFC 2822,
    `monday 2026-10-12`) is not warned about: git reads the date.
S2  A bare eight-digit number is a YYYYMMDD calendar day, as in the README's
    date table. Git itself reads it as "now".
S3  A git that does not answer is one `error:` line and exit code 2, not a
    traceback.
S4  `standupguy mcp` with standard input closed is one `error:` line and exit
    code 2, not an AttributeError.
S5  The README says the digest reads the checked-out branch.
S6  Python 3.14 reports an HTTPError that was never closed.
S7  README: the closed-pipe exit code, the workflow file, --port.
"""

import contextlib
import datetime
import gc
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from unittest import mock

from test_dates import commit_at, it_is, new_repo
from test_round5 import run_cli
from test_standupguy import git, make_repo  # also isolates git config/HOME

from standupguy import cli as cli_mod
from standupguy import draft as draft_mod
from standupguy import gitlog as gitlog_mod
from standupguy import mcp as mcp_mod
from standupguy import push as push_mod
from standupguy import serve as serve_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def readme_text():
    """The README as one line of single-spaced text (CRLF or LF alike)."""
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
        return " ".join(f.read().split())


# ------------------------------------------------------------------ S6


class TestAnHttpErrorIsClosedItself(unittest.TestCase):
    """Python 3.14 reports an HTTPError that was never closed (a
    ResourceWarning when it is collected), however closed the file it carries
    is. draft and push let go of the error they catch: the error itself."""

    WEBHOOK = "https://hooks.slack.com/services/T0/B0/X"

    def watched(self):
        """An HTTPError that records the calls to its own close()."""
        error = urllib.error.HTTPError("http://a.test/v1", 500, "boom", {}, None)
        calls = []
        real_close = error.close

        def close():
            calls.append(1)
            real_close()

        error.close = close
        return error, calls

    def fail_a_draft(self, error):
        def fail(self, req, *a, **k):
            raise error

        with mock.patch.object(urllib.request.OpenerDirector, "open", fail):
            with self.assertRaises(draft_mod.DraftError):
                draft_mod.draft("digest", base_url="http://127.0.0.1:1/v1",
                                api_key="k", model="m", timeout=5)

    def fail_a_push(self, error):
        def fake_urlopen(req, timeout=30):      # no network in a test
            raise error

        with mock.patch.object(push_mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(push_mod.PushError):
                push_mod.push_slack(self.WEBHOOK, "hi")

    def test_a_failed_draft_closes_the_error(self):
        error, calls = self.watched()
        self.fail_a_draft(error)
        self.assertEqual(calls, [1])

    def test_a_failed_push_closes_the_error(self):
        error, calls = self.watched()
        self.fail_a_push(error)
        self.assertEqual(calls, [1])

    def test_nothing_is_left_for_the_garbage_collector(self):
        def run():
            for fail in (self.fail_a_draft, self.fail_a_push):
                fail(urllib.error.HTTPError("http://a.test/v1", 404, "gone",
                                            {}, None))

        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            run()
            gc.collect()
        leaked = [str(w.message) for w in seen
                  if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaked, [])


# ------------------------------------------------------------------ S1


class TestAWeekdayInsideAWholeDate(unittest.TestCase):
    """`standupguy --since "$(git log -1 --format=%cd)"` pastes git's own date
    format ("Sun Oct 18 09:00:00 2026 -0500"). Git reads that exactly and
    ignores the weekday in it, so the warning for a bare weekday has nothing
    to say there."""

    WHOLE = (
        "Sun Oct 18 09:00:00 2026 -0500",           # git's default date format
        "Sun Oct 18 2026",
        "Sun, 18 Oct 2026 09:00:00 +0000",          # RFC 2822 (--date=rfc)
        "Mon Oct 12 2026",                           # a weekday that is wrong
        "Monday, October 12, 2026",
        "monday Oct 12", "monday 12 october", "MON OCT 12",
        "monday 2026-10-12", "mon 2026-10-12 09:00",
        "monday 10/12/2026", "monday 12.10.2026", "monday 2026/10/12",
    )
    PARTIAL = (
        "monday", "mon", "this monday", "monday 9am", "friday 17:00",
        "monday 2026", "monday october", "monday oct 3pm",
        "last week monday", "monday 2026-10",
    )

    def test_a_whole_date_with_a_weekday_has_nothing_to_warn_about(self):
        for value in self.WHOLE:
            with self.subTest(value):
                self.assertIsNone(gitlog_mod.check_date(value))

    def test_a_weekday_that_is_all_there_is_to_the_day_still_warns(self):
        for value in self.PARTIAL:
            with self.subTest(value):
                warning = gitlog_mod.check_date(value)
                self.assertTrue(warning, f"no warning for {value!r}")
                self.assertIn("means today", warning)

    def test_an_absurdly_long_number_is_not_a_day_and_not_a_crash(self):
        # int() refuses a string of thousands of digits
        warning = gitlog_mod.check_date("monday jan " + "9" * 5000)
        self.assertIn("means today", warning)

    def test_the_command_line_is_quiet_for_gits_own_date_format(self):
        repo = make_repo()
        stamp = gitlog_mod._git(repo, "log", "-1", "--format=%cd",
                                "--date=default").strip()
        self.assertRegex(stamp, r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d+ ")
        code, out, err = run_cli("--root", repo, "--all", "--since", stamp)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("update readme", out)         # the commit that date is

    def test_the_warning_for_a_bare_weekday_is_still_given(self):
        code, out, err = run_cli("--root", make_repo(), "--all", "--since",
                                 "monday")
        self.assertEqual(code, 0, err)
        self.assertTrue(err.startswith("warning: --since 'monday': "), err)

    def test_the_readme_says_so(self):
        flat = readme_text()
        for phrase in ("There is no warning when the value also gives its "
                       "date", "Sun Oct 18 09:00:00 2026 -0500"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")


class TestWhatGitDoesWithAWeekdayInAWholeDate(unittest.TestCase):
    """The fact S1 rests on, asked of the git that is installed: with the date
    spelled out the weekday does not matter, even a wrong one."""

    @classmethod
    def setUpClass(cls):
        cls.repo = new_repo()
        for day in range(12, 18):               # Monday the 12th ... Saturday
            commit_at(cls.repo, f"day {day}", f"2026-10-{day}T12:00:00-05:00")
        with it_is("2026-10-17T15:00:00-05:00"):
            said = gitlog_mod._git(cls.repo, "rev-parse",
                                   "--since=today 00:00").strip()
        midnight = datetime.datetime.fromisoformat("2026-10-17T00:00:00-05:00")
        cls.staged = said == f"--max-age={int(midnight.timestamp())}"

    def setUp(self):
        if not self.staged:
            self.skipTest("this git does not take TZ and GIT_TEST_DATE_NOW")

    def subjects(self, since):
        return sorted(c.subject for c in
                      gitlog_mod.commits_since(self.repo, since))

    def test_the_date_is_read_and_the_weekday_is_not(self):
        with it_is("2026-10-17T15:00:00-05:00"):
            for value in ("Fri Oct 16 2026", "Mon Oct 16 2026",
                          "Tue, 16 Oct 2026", "sunday 2026-10-16",
                          "Friday, October 16, 2026"):
                with self.subTest(value):
                    self.assertEqual(self.subjects(value), ["day 16", "day 17"])


# ------------------------------------------------------------------ S2


class TestACompactDate(unittest.TestCase):
    """`--since 20261005`: git reads a bare eight-digit number as "now" (it is
    only a date when a time of day follows), so the digest was empty, with no
    word of warning. It is the day it says, the way 2026-10-05 is."""

    def arg(self, value, end=False):
        return gitlog_mod._since_to_arg(value, end=end)

    def test_it_is_a_calendar_day_like_the_iso_form(self):
        for value, day in (("20261005", "2026-10-05"),
                           ("20260101", "2026-01-01"),
                           ("20241231", "2024-12-31"),
                           ("20240229", "2024-02-29"),
                           (" 20261005 ", "2026-10-05")):
            with self.subTest(value):
                self.assertEqual(self.arg(value), f"{day} 00:00")
                self.assertEqual(self.arg(value, end=True), f"{day} 23:59:59")

    def test_nine_digits_and_more_are_still_a_unix_timestamp(self):
        for value in ("100000000", "123456789", "1792128212", "@1792128212",
                      "99999999999"):
            with self.subTest(value):
                self.assertEqual(self.arg(value), value)
                self.assertEqual(self.arg(value, end=True), value)

    def test_a_time_of_day_after_it_is_still_taken_as_given(self):
        self.assertEqual(self.arg("20261005 10:30"), "20261005 10:30")

    def test_a_number_that_is_not_a_day_is_left_alone_for_check_date(self):
        for value in ("20261345", "20260230", "00000000", "12345678",
                      "99999999"):
            with self.subTest(value):
                self.assertEqual(self.arg(value), f"{value} 00:00")

    def test_check_date_accepts_a_real_day_without_a_word(self):
        for value in ("20261005", "20240229", " 20261231 "):
            with self.subTest(value):
                self.assertIsNone(gitlog_mod.check_date(value))

    def test_check_date_refuses_a_number_that_is_no_day(self):
        for value in ("20261345", "20260230", "20250229", "00000000",
                      "12345678", "99999999", "20261301", "20260001"):
            with self.subTest(value):
                with self.assertRaises(ValueError) as cm:
                    gitlog_mod.check_date(value)
                self.assertIn("not a valid date", str(cm.exception))
                self.assertIn(value, str(cm.exception))

    def test_the_command_line_reads_it_as_that_day(self):
        repo = make_repo()      # 10-10 and three commits on 10-14
        for flags in (("--since", "20261014"),
                      ("--since", "2026-10-14"),
                      ("--since", "20261014", "--until", "20261014"),
                      ("--since", "2026-10-14", "--until", "20261014"),
                      ("--since", "20261001", "--until", "20261014")):
            with self.subTest(flags):
                code, out, err = run_cli("--root", repo, "--all", *flags,
                                         "--format", "json")
                self.assertEqual((code, err), (0, ""))
                found = [c["subject"] for c in json.loads(out)["commits"]]
                same = run_cli("--root", repo, "--all", "--format", "json",
                               *[f.replace("20261014", "2026-10-14")
                                  .replace("20261001", "2026-10-01")
                                 for f in flags])
                self.assertEqual(
                    found, [c["subject"] for c in json.loads(same[1])["commits"]])
                self.assertTrue(found)
        # the three commits of the day, not the empty digest "now" gives
        code, out, err = run_cli("--root", repo, "--all", "--since",
                                 "20261014", "--format", "json")
        self.assertEqual(len(json.loads(out)["commits"]), 3)

    def test_the_command_line_refuses_a_number_that_is_no_day(self):
        repo = make_repo()
        for flag in ("--since", "--until"):
            with self.subTest(flag):
                code, out, err = run_cli("--root", repo, "--all", flag,
                                         "20261345")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                # Python's own words for the month (3.14 adds ", not 13")
                self.assertTrue(
                    err.startswith(f"error: {flag} '20261345' is not a valid "
                                   "date (month must be in 1..12")
                    and err.endswith(")\n") and err.count("\n") == 1, err)

    def test_serve_and_mcp_refuse_it_too(self):
        repo = make_repo()
        with self.assertRaises(ValueError):
            serve_mod._payload([repo], {"since": ["20261345"]}, False)
        result = mcp_mod._call_tool({"since": "20261345"}, [repo])
        self.assertTrue(result["isError"])
        self.assertIn("not a valid date", result["content"][0]["text"])
        # ...and read a real one as that day
        status, _, body = serve_mod._payload(
            [repo], {"since": ["20261014"], "all": ["1"]}, False)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["commits"]), 3)

    def test_the_readme_says_so(self):
        flat = readme_text()
        for phrase in ("`20261014`", "git itself takes it for *now*",
                       "nine or more digits"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")


# ------------------------------------------------------------------ S3

PING = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'


def times_out_on(*subcommands):
    """A stand-in for subprocess.run: the git commands named here run into
    the timeout, every other command runs for real."""
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[0] == "git" and cmd[3] in subcommands:   # git -C <root> <command>
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)

    return mock.patch.object(gitlog_mod.subprocess, "run", run)


class TestAGitThatDoesNotAnswer(unittest.TestCase):
    """git slower than its timeout ended the command line in a traceback
    (`subprocess.TimeoutExpired`); `mcp` and `serve` coped. It is one `error:`
    line and exit code 2 now, the code of a git that is missing."""

    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()

    def assertOneErrorLine(self, code, out, err, command):
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertTrue(err.startswith(f"error: git {command} did not finish "
                                       "within 60 seconds"), err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn(self.repo, err)          # no command line, no path

    def test_the_default_is_a_minute_and_it_is_what_subprocess_gets(self):
        self.assertEqual(gitlog_mod.GIT_TIMEOUT, 60)
        seen = []
        real_run = subprocess.run

        def run(cmd, *args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(gitlog_mod.subprocess, "run", run), \
                mock.patch.object(gitlog_mod, "GIT_TIMEOUT", 7):
            gitlog_mod._git(self.repo, "rev-parse", "--git-dir")
        self.assertEqual(seen, [7])

    def test_git_says_what_ran_out_and_for_how_long(self):
        with times_out_on("log"):
            with self.assertRaises(gitlog_mod.GitTimeout) as cm:
                gitlog_mod._git(self.repo, "log", "-z", "--since=x")
        self.assertEqual(
            str(cm.exception),
            "git log did not finish within 60 seconds - is the repository on "
            "a slow or disconnected drive?")
        with times_out_on("log"), mock.patch.object(gitlog_mod,
                                                    "GIT_TIMEOUT", 1):
            with self.assertRaises(gitlog_mod.GitTimeout) as cm:
                gitlog_mod._git(self.repo, "log")
        self.assertIn("within 1 second -", str(cm.exception))

    def test_the_command_line_is_one_line_and_exit_code_2(self):
        for command in ("log", "status", "rev-parse"):
            for extra in ((), ("--format", "json"), ("--week",), ("--sprint",)):
                with self.subTest(command, extra=extra):
                    with times_out_on(command):
                        code, out, err = run_cli(
                            "--root", self.repo, "--all", "--since",
                            "2026-10-01", *extra)
                    self.assertOneErrorLine(code, out, err, command)

    def test_no_other_helper_swallows_it(self):
        with times_out_on("config"):
            with self.assertRaises(gitlog_mod.GitTimeout):
                gitlog_mod.default_author(self.repo)
            code, out, err = run_cli("--root", self.repo, "--since",
                                     "2026-10-01")
        self.assertOneErrorLine(code, out, err, "config")
        with times_out_on("rev-parse"):
            with self.assertRaises(gitlog_mod.GitTimeout):
                gitlog_mod.repo_name(self.repo)
        # the second rev-parse in repo_name (a folder without a work tree)
        real_run = subprocess.run

        def second_one_hangs(cmd, *args, **kwargs):
            if cmd[0] == "git" and cmd[3:5] == ["rev-parse", "--git-dir"]:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return real_run(cmd, *args, **kwargs)

        no_tree = os.path.join(self.repo, ".git")
        with mock.patch.object(gitlog_mod.subprocess, "run", second_one_hangs):
            with self.assertRaises(gitlog_mod.GitTimeout):
                gitlog_mod.repo_name(no_tree)

    def test_mcp_and_serve_still_cope_and_tell_no_paths(self):
        with times_out_on("log"):
            result = mcp_mod._call_tool({"since": "2026-10-01", "all": True},
                                        [self.repo])
        self.assertTrue(result["isError"])
        said = json.loads(result["content"][0]["text"])["error"]
        self.assertTrue(said.startswith("git log did not finish within 60 "
                                        "seconds"), said)
        self.assertNotIn(self.repo, said)
        server = serve_mod.make_server(self.repo, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        err = io.StringIO()
        with times_out_on("log"), contextlib.redirect_stderr(err):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/digest?since=2026-10-01&all=1",
                    timeout=10)
        with cm.exception:
            body = cm.exception.read().decode("utf-8")
        self.assertEqual(cm.exception.code, 500)
        self.assertIn("internal error", json.loads(body)["error"])
        self.assertNotIn("git", body)
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertIn("GitTimeout: git log did not finish within 60 seconds",
                      lines[0])

    @unittest.skipIf(os.name == "nt", "the stand-in git is a shell script; the "
                                      "tests above cover the same code")
    def test_a_real_git_that_sleeps(self):
        # not a mock: a `git` first on PATH that hangs on `log`, and a short
        # timeout, so the real subprocess.run gives up on a real child
        real_git = shutil.which("git")
        self.assertTrue(real_git)
        fakebin = tempfile.mkdtemp()
        script = os.path.join(fakebin, "git")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\nfor a in "$@"; do\n  if [ "$a" = log ]; then '
                    'exec sleep 60; fi\ndone\nexec "$REAL_GIT" "$@"\n')
        os.chmod(script, 0o755)
        env = {"PATH": fakebin + os.pathsep + os.environ["PATH"],
               "REAL_GIT": real_git}
        started = time.monotonic()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(gitlog_mod, "GIT_TIMEOUT", 1):
            code, out, err = run_cli("--root", self.repo, "--since",
                                     "2000-01-01")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(
            err, "error: git log did not finish within 1 second - is the "
                 "repository on a slow or disconnected drive?\n")
        self.assertLess(time.monotonic() - started, 30)

    def test_the_readme_says_so(self):
        flat = readme_text()
        self.assertTrue("did not finish within 60 seconds" in flat or
                        "git that does not answer within 60 seconds" in flat,
                        "the README does not say what a slow git does")


# ------------------------------------------------------------------ S4


class TestMcpWithStandardInputClosed(unittest.TestCase):
    """`standupguy mcp <&-` ended in an AttributeError traceback ('NoneType'
    object has no attribute 'readline', exit code 1): a process started with
    standard input shut has None for sys.stdin. There is no client to talk to:
    one `error:` line and exit code 2, before any request is read."""

    LINE = "error: standard input is closed, so there are no requests to read\n"

    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()

    def run_without_stdin(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_mod.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_command_line_says_so_and_exits_2(self):
        code, out, err = self.run_without_stdin("--root", self.repo, "mcp")
        self.assertEqual((code, out, err), (2, "", self.LINE))

    def test_the_library_call_says_so_and_returns_2(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", None), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mcp_mod.serve([self.repo]), 2)
        self.assertEqual(err.getvalue(), self.LINE)

    def test_a_stream_the_caller_passes_is_used_whatever_sys_stdin_is(self):
        out = io.BytesIO()
        with mock.patch.object(sys, "stdin", None):
            code = mcp_mod.serve([self.repo], stdin=io.BytesIO(PING),
                                 stdout=out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()),
                         {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_standard_output_closed_keeps_its_own_line_and_code(self):
        # documented before: exit code 1, said before a request is read
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", None), \
                mock.patch.object(sys, "stdout", None), \
                contextlib.redirect_stderr(err):
            code = cli_mod.main(["--root", self.repo, "mcp"])
        self.assertEqual(code, 1)
        self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())
        self.assertIn("standard output is closed", err.getvalue())

    @unittest.skipIf(os.name == "nt", "there is no <&- on Windows; the tests "
                                      "above stand in for it")
    def test_in_a_real_process(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
        env["PYTHONPATH"] = REPO
        proc = subprocess.run(
            ["sh", "-c", 'exec "$@" <&-', "sh", sys.executable, "-m",
             "standupguy", "--root", self.repo, "mcp"],
            capture_output=True, env=env, cwd=REPO, timeout=60)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr.decode("utf-8", "replace"), self.LINE)
        self.assertEqual(proc.returncode, 2)

    def test_the_readme_says_so(self):
        flat = readme_text()
        for phrase in ("standupguy mcp <&-", "standard input is closed"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")


# ------------------------------------------------------------------ S5


class TestTheDigestReadsTheCheckedOutBranch(unittest.TestCase):
    """The digest is `git log HEAD`. A commit made yesterday on another branch
    is not in it, and `--all` means every author, not every branch. Said in
    the README and in `--all`'s help; this pins the behaviour they describe."""

    @classmethod
    def setUpClass(cls):
        cls.repo = new_repo()
        commit_at(cls.repo, "chore: on main early", "2026-10-15T10:00:00")
        git(cls.repo, "checkout", "-qb", "feature/login")
        commit_at(cls.repo, "feat: on the feature branch", "2026-10-16T11:00:00")
        git(cls.repo, "checkout", "-q", "main")
        commit_at(cls.repo, "docs: on main later", "2026-10-17T09:00:00")

    def subjects(self, *flags):
        code, out, err = run_cli("--root", self.repo, "--all", "--since",
                                 "2026-10-15", "--format", "json", *flags)
        self.assertEqual((code, err), (0, ""))
        return [c["subject"] for c in json.loads(out)["commits"]]

    def test_a_commit_on_another_branch_is_not_in_it(self):
        git(self.repo, "checkout", "-q", "main")
        self.assertEqual(self.subjects(),
                         ["docs: on main later", "chore: on main early"])

    def test_checking_the_branch_out_shows_its_work(self):
        git(self.repo, "checkout", "-q", "feature/login")
        self.addCleanup(git, self.repo, "checkout", "-q", "main")
        self.assertEqual(self.subjects(),
                         ["feat: on the feature branch", "chore: on main early"])

    def test_all_is_about_authors_not_branches(self):
        git(self.repo, "checkout", "-q", "main")
        self.assertNotIn("feat: on the feature branch", self.subjects("--all"))

    def test_the_readme_and_the_help_say_so(self):
        flat = readme_text()
        for phrase in ("the history of the branch that is checked out",
                       "`--all` means every author, not every branch"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")
        helptext = " ".join(cli_mod.build_parser().format_help().split())
        self.assertTrue("not every branch" in helptext,
                        "--help does not say it")


# ------------------------------------------------------------------ S7a


def child_env():
    """The environment of a real child process. This sandbox exports
    PYTHONUNBUFFERED=1, which makes a child write every print at once and
    hides what a buffered stdout does at a closed pipe."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env["PYTHONPATH"] = REPO
    return env


class TestWhatAClosedPipeDoes(unittest.TestCase):
    """The README said `standupguy --week | head -1` ends with exit code 1.
    It does when the reader is gone before the output is written; when the
    output goes into the pipe first, as it does in that very command line,
    the exit code is 0 (and a shell reports head's anyway). Both are checked
    in a real process whose environment does not set PYTHONUNBUFFERED."""

    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()

    def argv(self, *extra):
        return [sys.executable, "-m", "standupguy", "--root", self.repo,
                "--all", "--since", "2026-10-01", *extra]

    def test_a_reader_that_is_gone_before_the_write_is_exit_code_1(self):
        for extra in ([], ["--format", "json"], ["--week"]):
            with self.subTest(extra):
                read_end, write_end = os.pipe()
                os.close(read_end)           # nobody will ever read this pipe
                try:
                    proc = subprocess.Popen(
                        self.argv(*extra), stdout=write_end,
                        stderr=subprocess.PIPE, cwd=REPO, env=child_env())
                finally:
                    os.close(write_end)
                try:
                    _, err = proc.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    raise
                self.assertEqual(err, b"", err.decode("utf-8", "replace"))
                self.assertEqual(proc.returncode, 1)

    def test_a_digest_that_fits_the_pipe_is_written_whole_with_exit_code_0(self):
        proc = subprocess.Popen(self.argv(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=REPO,
                                env=child_env())
        try:
            # Nobody has read anything yet. A short digest fits in the pipe,
            # so the child neither blocks nor fails: it is done before the
            # reader (`head -1`) has looked.
            code = proc.wait(timeout=60)
            out, err = proc.stdout.read(), proc.stderr.read()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        self.assertEqual(err, b"", err.decode("utf-8", "replace"))
        self.assertEqual(code, 0)
        self.assertIn(b"add login flow", out)

    def test_the_readme_says_what_is_true(self):
        flat = readme_text()
        self.assertFalse("ends quietly: exit code 1, no traceback" in flat,
                         "the README still says every closed pipe is exit "
                         "code 1")
        for phrase in ("never ends in a traceback",
                       "gone when the output is written",
                       "A digest that fits in the pipe is written whole and "
                       "the exit code is 0"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")


# ------------------------------------------------------------------ S7b


class TestTheWorkflowFileIsReadable(unittest.TestCase):
    """The README calls workflows/activepieces-standup-digest.json a
    human-readable spec. It was one line of 3.5 kB. It is written the way
    json.dumps(indent=2) writes it, so a person can read it and a diff shows
    what changed."""

    PATH = os.path.join(REPO, "workflows", "activepieces-standup-digest.json")

    def text(self):
        with open(self.PATH, encoding="utf-8", newline="") as f:
            return f.read()

    def test_it_is_indented_one_item_to_a_line(self):
        text = self.text()
        data = json.loads(text)
        self.assertEqual(
            text.replace("\r\n", "\n"),
            json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def test_it_is_still_the_flow_the_readme_describes(self):
        data = json.loads(self.text())
        self.assertEqual(
            [step["piece"] for step in data["steps"]],
            ["Schedule trigger", "HTTP Request", "Branch (condition)",
             "Slack"])
        self.assertEqual(data["platform"], "activepieces")

    def test_the_readme_still_calls_it_human_readable(self):
        self.assertTrue("human-readable spec" in readme_text())


# ------------------------------------------------------------------ S7c


class TestEveryOptionIsInTheReadme(unittest.TestCase):
    """`--port` was in `--help` only. Every long option of the command line is
    named in the README now, and a new one cannot be forgotten."""

    def options(self):
        parser = cli_mod.build_parser()
        found = {s for action in parser._actions
                 for s in action.option_strings if s.startswith("--")}
        return sorted(found - {"--help"})   # argparse's own, in every --help

    def test_every_option_is_named(self):
        options = self.options()
        self.assertIn("--port", options)      # the extraction sees options
        self.assertIn("--insecure-allow-lan", options)
        flat = readme_text()
        missing = [o for o in options if o not in flat]
        self.assertEqual(missing, [],
                         "options the README never names: " + " ".join(missing))

    def test_the_default_port_is_the_one_the_readme_gives(self):
        self.assertEqual(cli_mod.build_parser().parse_args(["serve"]).port,
                         8765)
        self.assertTrue("default 8765" in readme_text())

    def test_port_0_is_a_free_port_and_the_start_line_says_which(self):
        with mock.patch.object(serve_mod.ThreadedBridgeServer,
                               "serve_forever", return_value=None):
            code, out, err = run_cli("--root", make_repo(), "serve",
                                     "--port", "0")
        self.assertEqual((code, err), (0, ""))
        m = re.search(r"on http://127\.0\.0\.1:(\d+) ", out)
        self.assertTrue(m is not None and int(m.group(1)) > 0, out)
        for phrase in ("`--port 0` lets the system choose a free one",
                       "the line `serve` prints when it starts says which"):
            self.assertTrue(phrase in readme_text(),
                            f"the README does not say: {phrase!r}")


if __name__ == "__main__":
    unittest.main()
