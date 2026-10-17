"""Review round 6 regressions: what Windows testing left open.

I-1  A reader that has gone away is not always a BrokenPipeError. On
     Windows the flush of a closed stdout pipe raises a plain
     OSError(EINVAL). Every printing path (the CLI, the MCP loop, the HTTP
     bridge) treats it like the POSIX error and leaves quietly; anywhere
     else EINVAL is a real error and is not swallowed.
I-2  Control characters in commit text (subject, author name, file name) are
     shown as visible escapes on a terminal, so a commit cannot clear the
     screen or retitle the window. JSON, `serve` and MCP output stay exact.
I-3  `mcp` refuses to start on a path that is not a git repository, with the
     same one-line error and exit code 2 as `serve`.
I-4  `serve`: a request that fails inside the bridge gets a generic 500 (no
     git command line, no paths); the detail goes to the server's stderr.
I-5  No stray invisible characters in the source or the docs.

Linux cannot produce that Windows error, so the tests below stand in for
it: a stdout whose write() works and whose flush() raises
OSError(EINVAL, "Invalid argument"), and a module-level flag that makes
the code believe it is on Windows. The real closed-pipe processes are in
test_round5.TestBrokenPipe.
"""

import contextlib
import errno
import io
import json
import os
import re
import subprocess
import sys
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from test_standupguy import commit, git, make_repo  # isolates git/HOME
from test_round5 import RawModel, completion, run_cli

from standupguy import cli as cli_mod
from standupguy import mcp as mcp_mod
from standupguy import push as push_mod
from standupguy import serve as serve_mod
from standupguy import stdio as stdio_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A whole process that sees Windows' closed pipe: print() works, the flush
# fails with OSError(EINVAL) until fd 1 has been pointed at the null device
# (then the bytes have somewhere to go, as on a real Windows console).
WINDOWS_CHILD = """
import errno, io, os, stat, sys
from standupguy import cli, stdio

stdio.WINDOWS = True      # Linux cannot raise the Windows error: pretend

class ReaderGone(io.RawIOBase):
    def writable(self):
        return True
    def fileno(self):
        return 1
    def write(self, data):
        if stat.S_ISCHR(os.fstat(1).st_mode):    # the null device by now
            return len(data)
        raise OSError(errno.EINVAL, 'Invalid argument')

sys.stdout = io.TextIOWrapper(io.BufferedWriter(ReaderGone()),
                              encoding='utf-8')
sys.exit(cli.main(sys.argv[1:]))
"""

# ------------------------------------------------------------------ I-1


def einval():
    """The error Python on Windows raises for a stdout pipe nobody reads."""
    return OSError(errno.EINVAL, "Invalid argument")


class ScratchFd:
    """Stands in for file descriptor 1. When the reader has gone the code
    under test points its stdout descriptor at the null device; doing that
    to the test run's REAL fd 1 would swallow the test report, so the fake
    stdouts below hand out this one instead. `redirected()` then tells
    whether a write to it still lands in the file (it was left alone) or
    vanishes (it now goes to the null device)."""

    def __init__(self, test):
        self.file = tempfile.TemporaryFile()
        test.addCleanup(self.file.close)
        self.fd = os.dup(self.file.fileno())
        test.addCleanup(self.close)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def redirected(self) -> bool:
        os.write(self.fd, b"probe")
        self.file.seek(0)
        return self.file.read() == b""


class ClosedPipe(io.StringIO):
    """A stdout whose reader has gone, the way Python on Windows reports it:
    write() only fills the buffer and flush() raises a plain OSError — no
    BrokenPipeError anywhere. fail_on="write" is the unbuffered variant
    (`python -u`), where print() itself raises."""

    def __init__(self, fd, fail_on="flush", error=einval):
        super().__init__()
        self.fd, self.fail_on, self.error = fd, fail_on, error

    def fileno(self):
        return self.fd

    def write(self, text):
        if self.fail_on == "write":
            raise self.error()
        return super().write(text)

    def flush(self):
        if self.fail_on == "flush":
            raise self.error()


class TestPipeClosedHelper(unittest.TestCase):
    def check(self, exc, windows):
        with mock.patch.object(stdio_mod, "WINDOWS", windows):
            return stdio_mod.pipe_closed(exc)

    def test_the_posix_errors_count_everywhere(self):
        for windows in (False, True):
            for exc in (BrokenPipeError(errno.EPIPE, "Broken pipe"),
                        OSError(errno.EPIPE, "Broken pipe")):
                with self.subTest(windows=windows, exc=repr(exc)):
                    self.assertTrue(self.check(exc, windows))

    def test_einval_counts_on_windows_only(self):
        self.assertTrue(self.check(einval(), windows=True))
        self.assertFalse(self.check(einval(), windows=False))

    def test_nothing_else_counts(self):
        for windows in (False, True):
            for exc in (OSError(errno.ENOSPC, "No space left on device"),
                        OSError(errno.EBADF, "Bad file descriptor"),
                        OSError("no errno at all"),
                        ConnectionResetError(errno.ECONNRESET, "reset"),
                        ValueError("I/O operation on closed file"),
                        RuntimeError("boom"), None):
                with self.subTest(windows=windows, exc=repr(exc)):
                    self.assertFalse(self.check(exc, windows))

    def test_the_flag_follows_the_platform(self):
        self.assertEqual(stdio_mod.WINDOWS, os.name == "nt")


class TestHushStdout(unittest.TestCase):
    def test_points_the_descriptor_at_the_null_device(self):
        scratch = ScratchFd(self)
        with contextlib.redirect_stdout(ClosedPipe(scratch.fd)):
            stdio_mod.hush_stdout()
        self.assertTrue(scratch.redirected())

    def test_a_stdout_without_a_descriptor_is_left_alone(self):
        with contextlib.redirect_stdout(io.StringIO()):
            stdio_mod.hush_stdout()               # nothing to point: no error
        with mock.patch.object(sys, "stdout", None):
            stdio_mod.hush_stdout()               # pythonw: no stdout at all


class TestWindowsClosedPipe(unittest.TestCase):
    ARGV = ["--all", "--since", "2026-10-01"]

    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()

    def run_main(self, out, *argv, windows=True, stdin=None):
        """cli.main(argv) with `out` as stdout -> (exit code, stderr)."""
        err = io.StringIO()
        with mock.patch.object(stdio_mod, "WINDOWS", windows), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", stdin or sys.stdin):
            code = cli_mod.main(list(argv))
        return code, err.getvalue()

    def closed_pipe(self, **kwargs):
        scratch = ScratchFd(self)
        return scratch, ClosedPipe(scratch.fd, **kwargs)

    def test_small_output_found_out_at_the_flush_exits_quietly(self):
        for argv in (self.ARGV, self.ARGV + ["--format", "json"],
                     self.ARGV + ["--week"], self.ARGV + ["--sprint"]):
            with self.subTest(argv):
                scratch, out = self.closed_pipe()
                code, err = self.run_main(out, "--root", self.repo, *argv)
                self.assertEqual(code, 1)
                self.assertEqual(err, "")
                self.assertTrue(scratch.redirected())

    def test_unbuffered_output_found_out_at_the_write_exits_quietly(self):
        # `python -u`: the OSError comes straight out of print()
        for argv in (self.ARGV, self.ARGV + ["--format", "json"]):
            with self.subTest(argv):
                scratch, out = self.closed_pipe(fail_on="write")
                code, err = self.run_main(out, "--root", self.repo, *argv)
                self.assertEqual(code, 1)
                self.assertEqual(err, "")
                self.assertTrue(scratch.redirected())

    def test_help_and_version_exit_quietly_too(self):
        # argparse prints, then exits through SystemExit: the output is still
        # in the buffer when that happens
        for argv in (["--help"], ["--version"]):
            with self.subTest(argv):
                scratch, out = self.closed_pipe()
                code, err = self.run_main(out, *argv)
                self.assertEqual(code, 1)
                self.assertEqual(err, "")
                self.assertTrue(scratch.redirected())

    def test_a_vanished_mcp_client_ends_the_server_quietly(self):
        scratch, out = self.closed_pipe()
        request = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'
        stdin = io.TextIOWrapper(io.BytesIO(request), encoding="utf-8")
        code, err = self.run_main(out, "--root", self.repo, "mcp",
                                  stdin=stdin)
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertTrue(scratch.redirected())

    def test_serve_does_not_start_serving_into_a_dead_stdout(self):
        made = []
        real_make_server = serve_mod.make_server

        def spy(*args, **kwargs):
            made.append(real_make_server(*args, **kwargs))
            return made[-1]

        scratch, out = self.closed_pipe()      # the banner's flush fails
        with mock.patch.object(serve_mod, "make_server", spy), \
                mock.patch.object(
                    serve_mod.ThreadedBridgeServer, "serve_forever",
                    side_effect=AssertionError("served with nobody to read "
                                               "the banner")):
            code, err = self.run_main(out, "--root", self.repo, "serve",
                                      "--port", "0")
        self.assertEqual((code, err), (1, ""))
        self.assertTrue(scratch.redirected())
        self.assertEqual(made[0].socket.fileno(), -1)    # port given back

    def test_a_working_pipe_is_untouched(self):
        out = io.StringIO()
        code, err = self.run_main(out, "--root", self.repo, *self.ARGV)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("add login flow", out.getvalue())

    def test_in_a_real_process_the_exit_is_clean_too(self):
        # the fakes above prove the exit code; only a whole process shows
        # what Python does with stdout while it exits: status 120 and
        # "Exception ignored ..." unless fd 1 was pointed at the null device
        ping = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'
        runs = [(["--root", self.repo, *self.ARGV], b""),
                (["--root", self.repo, *self.ARGV, "--format", "json"], b""),
                (["--version"], b""),
                (["--root", self.repo, "mcp"], ping)]
        for argv, stdin in runs:
            with self.subTest(argv):
                proc = subprocess.run(
                    [sys.executable, "-c", WINDOWS_CHILD, *argv], input=stdin,
                    capture_output=True, cwd=REPO, timeout=60)
                self.assertEqual(proc.stderr, b"",
                                 proc.stderr.decode("utf-8", "replace"))
                self.assertEqual(proc.returncode, 1)

    def test_einval_is_not_swallowed_off_windows(self):
        # elsewhere EINVAL is "invalid argument": a bug, not a hung-up reader
        for fail_on in ("flush", "write"):
            with self.subTest(fail_on):
                scratch, out = self.closed_pipe(fail_on=fail_on)
                with self.assertRaises(OSError) as cm:
                    self.run_main(out, "--root", self.repo, *self.ARGV,
                                  windows=False)
                self.assertEqual(cm.exception.errno, errno.EINVAL)
                self.assertFalse(scratch.redirected())    # fd 1 left alone

    def test_other_errors_are_not_swallowed_on_windows_either(self):
        # a full disk (`standupguy > digest.md`) must still be heard
        def disk_full():
            return OSError(errno.ENOSPC, "No space left on device")

        scratch, out = self.closed_pipe(error=disk_full)
        with self.assertRaises(OSError) as cm:
            self.run_main(out, "--root", self.repo, *self.ARGV)
        self.assertEqual(cm.exception.errno, errno.ENOSPC)
        self.assertFalse(scratch.redirected())


# A client that hung up, as the bridge can see it. On Windows the socket
# errors are WinError 10053 (aborted) and 10054 (reset).
def winsock(cls, code, winerror):
    exc = cls(code, "winsock")
    exc.winerror = winerror        # only Windows sets this by itself
    return exc


GONE = {
    "broken pipe": BrokenPipeError(errno.EPIPE, "Broken pipe"),
    "reset": winsock(ConnectionResetError, errno.ECONNRESET, 10054),
    "aborted": winsock(ConnectionAbortedError, errno.ECONNABORTED, 10053),
    "winerror alone": winsock(OSError, errno.EIO, 10054),
}


def bare_handler(write_error):
    """A bridge Handler with no socket, whose body write fails."""
    handler = serve_mod.Handler.__new__(serve_mod.Handler)
    handler.send_response = handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda *a, **k: None
    handler.wfile = mock.Mock()
    handler.wfile.write.side_effect = write_error
    handler.close_connection = False
    return handler


class TestClientGone(unittest.TestCase):
    def test_what_counts_as_a_client_that_hung_up(self):
        for name, exc in GONE.items():
            with self.subTest(name):
                self.assertTrue(stdio_mod.client_gone(exc))
        for exc in (OSError(errno.EIO, "I/O error"), ValueError("x"),
                    TimeoutError("timed out"), None):
            with self.subTest(repr(exc)):
                self.assertFalse(stdio_mod.client_gone(exc))
        with mock.patch.object(stdio_mod, "WINDOWS", True):
            self.assertTrue(stdio_mod.client_gone(einval()))

    def test_sending_to_a_client_that_left_is_not_an_error(self):
        for name, exc in GONE.items():
            with self.subTest(name):
                handler = bare_handler(exc)
                handler._send(200, "text/plain", "x")
                self.assertTrue(handler.close_connection)

    def test_other_write_errors_still_surface(self):
        handler = bare_handler(OSError(errno.EIO, "I/O error"))
        with self.assertRaises(OSError):
            handler._send(200, "text/plain", "x")

    def test_the_server_stays_quiet_about_a_client_that_left(self):
        server = serve_mod.make_server(make_repo(), "127.0.0.1", 0)
        self.addCleanup(server.server_close)

        def complaint(exc):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                try:
                    raise exc
                except Exception:
                    server.handle_error(None, ("127.0.0.1", 1))
            return err.getvalue()

        for name, exc in GONE.items():
            with self.subTest(name):
                self.assertEqual(complaint(exc), "")
        # a real bug still gets its traceback
        self.assertIn("ValueError: boom", complaint(ValueError("boom")))


# ------------------------------------------------------------------ I-2

# every C0 and C1 control character, DEL included, except tab and newline
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class TestPrintable(unittest.TestCase):
    def test_every_control_becomes_a_visible_escape(self):
        for cp in (*range(0x20), *range(0x7F, 0xA0)):
            if cp in (0x09, 0x0A):
                continue
            with self.subTest(hex(cp)):
                self.assertEqual(stdio_mod.printable(f"<{chr(cp)}>"),
                                 f"<\\x{cp:02x}>")

    def test_the_usual_attack_is_spelled_out(self):
        self.assertEqual(stdio_mod.printable("a\x1b[2J\x1b]0;pwned\x07b"),
                         "a\\x1b[2J\\x1b]0;pwned\\x07b")

    def test_tab_newline_and_ordinary_text_pass_through(self):
        text = ("col\tcol\nline two \u2014 \u0141ukasz "
                "\u03be\u03ad\u03bd\u03bf\u03c2 \U0001f331 \u00e9")
        self.assertEqual(stdio_mod.printable(text), text)

    def test_nothing_is_left_to_escape_twice(self):
        once = stdio_mod.printable("x\x1b[2Jy\x9b")
        self.assertEqual(stdio_mod.printable(once), once)
        self.assertIsNone(CONTROLS.search(once))


class TestTerminalEscapes(unittest.TestCase):
    BODY = "\x1b[2J\x1b]0;pwned\x07 clear\x9b31m red\rwiped"
    SUBJECT = "feat: " + BODY
    AUTHOR = "Mal\x1b[31mlory"
    UNCOMMITTED = "wip\x1b[2J.py"
    SHOWN = "\\x1b[2J\\x1b]0;pwned\\x07 clear\\x9b31m red\\x0dwiped"
    ARGV = ["--all", "--since", "2026-10-01"]

    @classmethod
    def setUpClass(cls):
        # Windows file names cannot hold control characters; the commit
        # text can, everywhere
        cls.posix = os.name != "nt"
        cls.repo = tempfile.mkdtemp(
            suffix="-\x1b[2Jrepo" if cls.posix else "-repo")
        git(cls.repo, "init", "-qb", "main")
        git(cls.repo, "config", "user.email", "dan@example.com")
        commit(cls.repo, cls.AUTHOR, "m@example.com", "2026-10-14T10:00:00",
               cls.SUBJECT, {"a.py": "1"})
        if cls.posix:
            with open(os.path.join(cls.repo, cls.UNCOMMITTED), "w") as f:
                f.write("x")

    def assertNoControls(self, text):
        found = CONTROLS.search(text)
        self.assertIsNone(found, f"raw control character "
                                 f"{found and found.group()!r} in {text!r}")

    def test_every_text_mode_shows_the_controls_as_escapes(self):
        for extra in ([], ["--week"], ["--sprint"]):
            with self.subTest(extra):
                code, out, err = run_cli("--root", self.repo, *self.ARGV,
                                         *extra)
                self.assertEqual(code, 0, err)
                self.assertNoControls(out)
                self.assertIn(self.SHOWN, out)

    @unittest.skipUnless(os.name != "nt",
                         "no control characters in file names")
    def test_file_names_and_the_repo_name_too(self):
        code, out, err = run_cli("--root", self.repo, *self.ARGV)
        self.assertNoControls(out)
        self.assertIn("`wip\\x1b[2J.py`", out)           # In progress
        self.assertIn("\\x1b[2Jrepo", out.splitlines()[0])   # the title
        code, out, err = run_cli("--root", self.repo, *self.ARGV, "--sprint")
        self.assertNoControls(out)
        self.assertIn("`wip\\x1b[2J.py` _(uncommitted)_", out)

    def test_ordinary_commits_print_exactly_as_before(self):
        code, out, err = run_cli("--root", make_repo(), "--all", "--since",
                                 "2026-10-14")
        self.assertIn("- add login flow (1 files) `", out)
        self.assertNotIn("\\x", out)

    def test_json_stays_exact(self):
        code, out, err = run_cli("--root", self.repo, *self.ARGV,
                                 "--format", "json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        c = data["commits"][0]
        self.assertEqual((c["subject"], c["author"]),
                         (self.SUBJECT, self.AUTHOR))
        if self.posix:
            self.assertIn(self.UNCOMMITTED, data["in_progress"])
        self.assertNotIn("\\x1b", out)    # JSON spells it \u001b, losslessly
        self.assertNoControls(out)         # ...so it is safe to print as is

    def test_mcp_text_stays_exact(self):
        resp = mcp_mod.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "what_did_i_ship",
                        "arguments": {"since": "2026-10-01", "all": True}}},
            [self.repo])
        text = resp["result"]["content"][0]["text"]
        self.assertIn(self.BODY, text)

    def test_serve_stays_exact(self):
        server = serve_mod.make_server(self.repo, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = (f"http://127.0.0.1:{server.server_address[1]}"
               "/digest?since=2026-10-01&all=1")
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        self.assertEqual(data["commits"][0]["subject"], self.SUBJECT)
        self.assertIn(self.BODY, data["markdown"])

    def test_a_model_reply_is_shown_the_same_way(self):
        model = RawModel(completion("Yesterday:\n- \x1b[2J\x9b31m pwned"))
        try:
            code, out, err = run_cli("--root", self.repo, *self.ARGV,
                                     "--draft", "--base-url", model.url)
        finally:
            model.stop()
        self.assertEqual(code, 0, err)
        self.assertNoControls(out)
        self.assertIn("Yesterday:\n- \\x1b[2J\\x9b31m pwned", out)

    def test_the_digest_after_a_failed_draft_too(self):
        model = RawModel(b"<html>nope</html>")
        try:
            code, out, err = run_cli("--root", self.repo, *self.ARGV,
                                     "--draft", "--base-url", model.url)
        finally:
            model.stop()
        self.assertEqual(code, 2)
        self.assertNoControls(out)
        self.assertIn(self.SHOWN, out)

    def test_slack_gets_the_exact_text_and_cleans_it_itself(self):
        with mock.patch.object(push_mod, "push_slack") as push:
            code, out, err = run_cli(
                "--root", self.repo, *self.ARGV, "--push-slack",
                "https://hooks.slack.com/services/T/x")
        self.assertEqual(code, 0, err)
        sent = push.call_args.args[1]
        self.assertIn(self.BODY, sent)
        self.assertNotIn("\\x1b", sent)
        self.assertEqual(out, "pushed to Slack \u2713\n")

    def test_in_a_real_process_the_bytes_on_stdout_are_clean(self):
        proc = subprocess.run(
            [sys.executable, "-m", "standupguy", "--root", self.repo,
             *self.ARGV], capture_output=True, cwd=REPO, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for raw in (b"\x1b", b"\x07", b"\r", b"\xc2\x9b"):  # the last: U+009B
            self.assertNotIn(raw, proc.stdout.replace(b"\r\n", b"\n"), raw)
        self.assertIn(b"\\x1b[2J", proc.stdout)


# ------------------------------------------------------------------ I-3


PING = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'


def run_mcp(*argv):
    """`standupguy ... mcp` in this process, with one ping waiting on stdin
    -> (exit code, stdout, stderr)."""
    stdin = io.TextIOWrapper(io.BytesIO(PING), encoding="utf-8")
    with mock.patch.object(sys, "stdin", stdin):
        return run_cli(*argv, "mcp")


class TestMcpChecksItsRoots(unittest.TestCase):
    def test_a_path_that_is_not_a_repo_is_refused(self):
        notarepo = tempfile.mkdtemp()
        code, out, err = run_mcp("--root", notarepo)
        self.assertEqual(code, 2)
        self.assertEqual(err, f"error: not a git repo: {notarepo}\n")
        self.assertEqual(out, "")                # it never answered a thing

    def test_one_bad_root_among_good_ones_is_refused(self):
        notarepo = tempfile.mkdtemp()
        code, out, err = run_mcp("--root", make_repo(), "--root", notarepo)
        self.assertEqual(code, 2)
        self.assertEqual(err, f"error: not a git repo: {notarepo}\n")
        self.assertEqual(out, "")

    def test_repos_start_and_answer(self):
        repo = make_repo()
        # a folder inside a repo is fine too: git finds the repo from there
        for roots in ([repo], [repo, os.path.join(repo, "app")]):
            with self.subTest(roots):
                code, out, err = run_mcp(
                    *[a for r in roots for a in ("--root", r)])
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(json.loads(out),
                                 {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_in_a_real_process_it_exits_instead_of_waiting_for_a_client(self):
        notarepo = tempfile.mkdtemp()
        env = dict(os.environ, PYTHONPATH=REPO)
        for argv, cwd in ((["--root", notarepo, "mcp"], REPO),
                          (["mcp"], notarepo)):    # the default root: cwd
            with self.subTest(argv):
                proc = subprocess.run(
                    [sys.executable, "-m", "standupguy", *argv], input=PING,
                    capture_output=True, cwd=cwd, env=env, timeout=60)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, b"")
                lines = proc.stderr.decode("utf-8", "replace").splitlines()
                self.assertEqual(len(lines), 1, lines)
                self.assertTrue(
                    lines[0].startswith("error: not a git repo: "), lines)


# ------------------------------------------------------------------ I-4


def fetch(port, path):
    """GET -> (status, content type, body), for error statuses too."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=10) as resp:
            return (resp.status, resp.headers.get("Content-Type"),
                    resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read().decode("utf-8")


class TestServeHidesItsInternals(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.server = serve_mod.make_server(self.repo, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def move_the_repo_away(self):
        """What makes every request fail for real: git can no longer reach
        the repo the bridge was started with."""
        os.rename(self.repo, self.repo + "-moved")

    def test_a_failing_request_gets_a_generic_500(self):
        self.move_the_repo_away()
        err = io.StringIO()
        for path in ("/digest?since=2026-10-01", "/week?since=2026-10-01",
                     "/digest?since=2026-10-01&format=markdown"):
            with self.subTest(path), contextlib.redirect_stderr(err):
                status, ctype, body = fetch(self.port, path)
            self.assertEqual(status, 500)
            self.assertEqual(ctype, "application/json")
            message = json.loads(body)["error"]
            self.assertIn("internal error", message)
            for leak in ("git", "fatal", "--", "failed", self.repo,
                         os.path.basename(self.repo), "%x1"):
                self.assertNotIn(leak, body, leak)

    def test_the_detail_goes_to_the_servers_stderr(self):
        self.move_the_repo_away()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fetch(self.port, "/digest?since=2026-10-01")
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertTrue(lines[0].startswith("error: GET /digest?since="),
                        lines[0])
        self.assertIn("git log", lines[0])             # git's command line
        # ...and the path (git may spell it with / on Windows: compare the
        # name of the temporary folder)
        self.assertIn(os.path.basename(self.repo), lines[0])

    def test_what_the_client_sent_cannot_drive_the_operators_terminal(self):
        self.move_the_repo_away()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with socket.create_connection(("127.0.0.1", self.port),
                                          timeout=10) as client:
                client.sendall(b"GET /digest?x=\x1b[2J\x07 HTTP/1.0\r\n\r\n")
                reply = b""
                while chunk := client.recv(4096):
                    reply += chunk
        self.assertIn(b" 500 ", reply.split(b"\r\n")[0])
        self.assertIsNone(CONTROLS.search(err.getvalue()), err.getvalue())
        self.assertIn("\\x1b[2J\\x07", err.getvalue())

    def test_a_client_mistake_is_still_a_400_that_says_why(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, _, body = fetch(self.port, "/digest?since=2026-13-45")
        self.assertEqual(status, 400)
        self.assertIn("2026-13-45", json.loads(body)["error"])
        self.assertEqual(err.getvalue(), "")     # nothing went wrong inside

    def test_a_request_that_works_is_unchanged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, _, body = fetch(self.port,
                                    "/digest?since=2026-10-01&all=1")
        self.assertEqual(status, 200)
        subjects = [c["subject"] for c in json.loads(body)["commits"]]
        self.assertIn("feat: add login flow", subjects)
        self.assertEqual(err.getvalue(), "")


# ------------------------------------------------------------------ I-5

# Characters that sit in a file without showing: byte-order mark, zero-width
# and direction marks, bidi overrides, line/paragraph separators, word
# joiner. (Written as escapes, so this file has none of them itself.)
INVISIBLE = re.compile("[\ufeff\u2028\u2029\u200b-\u200f\u202a-\u202e\u2060]")


class TestNoStrayInvisibleCharacters(unittest.TestCase):
    def text_files(self):
        """(path, text) for the top-level files and everything under the
        source, tests and workflows folders (not a venv a developer keeps
        next to them)."""
        paths = [os.path.join(REPO, name) for name in os.listdir(REPO)
                 if os.path.isfile(os.path.join(REPO, name))]
        for sub in ("standupguy", "tests", "workflows"):
            for folder, dirs, files in os.walk(os.path.join(REPO, sub)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                paths += [os.path.join(folder, name) for name in files]
        for path in sorted(paths):
            with open(path, "rb") as f:
                raw = f.read()
            try:
                yield path, raw.decode("utf-8")
            except UnicodeDecodeError:
                pass                              # not text

    def test_source_and_docs_have_none(self):
        seen, found = 0, []
        for path, text in self.text_files():
            seen += 1
            for number, line in enumerate(text.splitlines(), 1):
                for m in INVISIBLE.finditer(line):
                    found.append(f"{os.path.relpath(path, REPO)}:{number}:"
                                 f"{m.start() + 1} U+{ord(m.group()):04X}")
        self.assertGreater(seen, 10)              # it did look at the repo
        self.assertEqual(found, [])

    def test_the_pattern_finds_what_it_is_meant_to(self):
        for cp in (0xFEFF, 0x2028, 0x2029, 0x2060, *range(0x200B, 0x2010),
                   *range(0x202A, 0x202F)):
            with self.subTest(hex(cp)):
                self.assertTrue(INVISIBLE.search(f"a{chr(cp)}b"))
        self.assertFalse(
            INVISIBLE.search("plain text \u2014 \u0141ukasz \u00e9"))


if __name__ == "__main__":
    unittest.main()
