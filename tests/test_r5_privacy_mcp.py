"""Round 5: what must not leak into a standup, an MCP server's manners, the
git-forensics edge (shallow clones), the pinned author line, and test
methods earlier rounds did not use (seeded property tests, mutation-style
sanity checks, golden files).

Every secret-shaped value is assembled at run time.
"""

import contextlib
import io
import json
import os
import random
import re
import string
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GOLDEN = os.path.join(HERE, "golden")
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

from test_standupguy import make_repo, git   # noqa: E402  (isolates git/HOME)

import standupguy                            # noqa: E402
from standupguy import cli as cli_mod        # noqa: E402
from standupguy import formats as formats_mod  # noqa: E402
from standupguy import mcp as mcp_mod        # noqa: E402
from standupguy import redact as redact_mod  # noqa: E402

AUTHOR = "Calas Automations Inc."


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli_mod.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def _read(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


def std(text, profile="standard", terms=()):
    return redact_mod.make(profile, terms).text(text)


# ---- the author line -------------------------------------------------------

class TestAuthorLine(unittest.TestCase):
    def test_author_is_calas_automations_inc_everywhere(self):
        self.assertEqual(standupguy.__author__, AUTHOR)
        self.assertIn(f'authors = [{{ name = "{AUTHOR}" }}]',
                      _read("pyproject.toml"))
        self.assertIn(f"Copyright (c) 2026 {AUTHOR}", _read("LICENSE"))
        code, out, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn(AUTHOR, out)
        self.assertIn(standupguy.__version__, out)

    def test_no_email_was_invented(self):
        text = _read("pyproject.toml")
        block = re.search(r"(?m)^authors = .*$", text).group(0)
        self.assertNotIn("@", block)
        self.assertNotIn("maintainers", text)

    def test_doctor_names_the_maker(self):
        tmp = make_repo()
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            code, out, _ = run_cli("doctor")
        finally:
            os.chdir(cwd)
        self.assertIn(AUTHOR, out)

    def test_old_handles_are_not_used_as_the_author(self):
        for name in ("README.md", "CHANGELOG.md", "pyproject.toml", "LICENSE"):
            self.assertNotRegex(_read(name), r"(?i)dan calas|calasdan-sketch\b"
                                r"(?!/standupguy)", name)


# ---- redaction: the shapes a privacy review found --------------------------

def _b64ish(rng, n=18):
    return "".join(rng.choice(string.ascii_letters) for _ in range(n)) + "9=="


class TestRedactionRound5(unittest.TestCase):
    def test_webhook_urls_are_secrets_whole(self):
        rng = random.Random(1)
        tail = "/".join("".join(rng.choice(string.ascii_uppercase)
                                for _ in range(9)) for _ in range(3))
        for url in (
                "https://hooks.slack" + ".com/services/T" + tail,
                "https://discord" + ".com/api/webhooks/1234567890/abcDEF_gh-IJ",
                "https://outlook.office" + ".com/webhook/aaaa@bbbb/IncomingWebhook/cccc",
                "https://acme.webhook.office" + ".com/webhookb2/x@y/IncomingWebhook/z/w",
                "https://hooks.zapier" + ".com/hooks/catch/123/abc/",
                "https://api.telegram" + ".org/bot12345:AAAbbbCCC_dd-EE"):
            out = std(f"notify via {url} now")
            self.assertEqual(out, "notify via {secret} now", url)
            # strict as well
            self.assertNotIn(url.rstrip("/").split("/")[-1], std(url, "strict"))

    def test_basic_auth_header(self):
        rng = random.Random(2)
        b64 = _b64ish(rng)
        self.assertNotIn(b64, std(f"Authorization: Basic {b64}"))

    def test_quoted_passphrase_goes_whole(self):
        out = std('add "password": "correct horse battery staple" to fixture')
        self.assertNotIn("horse", out)
        self.assertNotIn("staple", out)
        out = std("set secret = 'two words here'")
        self.assertNotIn("words", out)

    def test_home_folder_user_name(self):
        self.assertEqual(std("bump /Users/dana/app/x.py"), "bump ~/app/x.py")
        self.assertEqual(std("open C:\\Users\\dana\\Desktop\\x"),
                         "open ~\\Desktop\\x")
        self.assertEqual(std("open c:/Users/dana/x"), "open ~/x")
        # a web route is not a person; /home/NAME is hidden only in strict
        self.assertEqual(std("add /home/dashboard route"),
                         "add /home/dashboard route")
        self.assertEqual(std("see /home/dana/x", "strict"), "see ~/x")
        self.assertEqual(std("C:\\Users\\Public\\x"), "C:\\Users\\Public\\x")
        self.assertEqual(std("GET /users/123 and /users/me"),
                         "GET /users/123 and /users/me")

    def test_identity_numbers(self):
        self.assertEqual(std("SSN 123-45-6789 ok"), "SSN {id-number} ok")
        self.assertEqual(std("SIN 046 454 286 ok"), "SIN {id-number} ok")
        self.assertEqual(std("card 4111 1111 1111 1111"), "card {card}")
        self.assertEqual(std("card 4111111111111111"), "card {card}")

    def test_numbers_that_are_not_identity_numbers_stay(self):
        for text in ("timestamp 1700000000000", "call 204-227-5308",
                     "date 2026-10-14", "ticket 123-456-789 a",   # fails Luhn
                     "card 4111 1111 1111 1112", "id 4111111111111112",
                     "order 000-12-3456", "port 12345678901234567"):
            self.assertEqual(std(text), text, text)

    def test_private_ipv6(self):
        self.assertEqual(std("bind fd00::1 and fe80::1ff:fe23:4567:890a"),
                         "bind {ip} and {ip}")
        self.assertEqual(std("docs use 2001:db8::1"), "docs use 2001:db8::1")

    def test_internal_host_with_more_labels_goes_whole(self):
        self.assertEqual(std("clone git.corp.example.com now"),
                         "clone {internal-url} now")
        self.assertEqual(std("go to wiki.corp"), "go to {internal-url}")

    def test_file_names_with_local_in_them_are_files(self):
        for name in ("appsettings.local.json", "next.config.local.js",
                     "docker-compose.local.yml", "app.local.env"):
            self.assertEqual(std(f"edit {name} please"),
                             f"edit {name} please", name)
        # ... but a host with a port or a path is a host
        self.assertEqual(std("open app.local.json:8080/x"),
                         "open {internal-url}")

    def test_prose_is_left_alone(self):
        for text in ("basic infrastructure cleanup for internationalization",
                     "Use Basic auth for the admin page",
                     "password reset flow", 'docs: "password": ""',
                     "fix token refresh logic"):
            self.assertEqual(std(text), text, text)

    def test_total_and_bounded_on_random_text(self):
        """Seeded property test (hypothesis is not a dependency): for
        20,000 random strings built from the characters redaction cares
        about, redaction never raises, never grows a string without bound,
        and redacting again never makes it longer."""
        rng = random.Random(20261108)
        alphabet = list(":0123456789abcdefFD./\\ -\"'=@_<>[]") + [
            "Users", "home", "password", "basic ", "corp", "local", "fd00",
            "fe80", "http://", "https://", "token=", "AKIA", "ghp_"]
        r = redact_mod.make("standard")
        for _ in range(20000):
            s = "".join(rng.choice(alphabet)
                        for _ in range(rng.randint(0, 50)))
            once = r.text(s)
            self.assertLessEqual(len(once), len(s) * 3 + 40, repr(s))
            # (not strictly idempotent: `password:{internal-url}` reads as
            # a value the second time round; it must only never grow)
            self.assertLessEqual(len(r.text(once)), len(once), repr(s))

    def test_a_planted_secret_never_survives_any_surroundings(self):
        """Seeded: the same secret inside 300 random sentences."""
        rng = random.Random(77)
        up = string.ascii_uppercase + string.digits
        secrets = ["AKIA" + "".join(rng.choice(up) for _ in range(16)),
                   "ghp_" + "".join(rng.choice(string.ascii_letters)
                                    for _ in range(36)),
                   "10.%d.%d.%d" % (rng.randint(0, 255), rng.randint(0, 255),
                                    rng.randint(1, 254))]
        words = "fix add the server for prod when on at see a b c".split()
        for secret in secrets:
            for _ in range(100):
                left = " ".join(rng.choice(words)
                                for _ in range(rng.randint(0, 5)))
                right = " ".join(rng.choice(words)
                                 for _ in range(rng.randint(0, 5)))
                sep = rng.choice([" ", ": ", " (", "=", " \""])
                text = f"{left}{sep}{secret} {right}"
                self.assertNotIn(secret, std(text), text)

    def test_hostile_long_input_stays_fast(self):
        import time
        for text in (":" * 400000, "fd00" + ":0" * 100000, "1234-" * 100000,
                     "/Users/" * 50000, "basic " + "A" * 300000,
                     "a." * 100000 + "corp"):
            t = time.time()
            std(text)
            self.assertLess(time.time() - t, 8.0)


class TestMutationSanity(unittest.TestCase):
    """Break a key condition on purpose and check that a test above notices.
    If the broken version still passes, the test was not testing it."""

    def test_luhn_check_matters(self):
        with mock.patch.object(redact_mod, "_luhn", lambda d: True):
            self.assertNotEqual(std("ticket 123-456-789 a"),
                                "ticket 123-456-789 a")

    def test_file_extension_list_matters(self):
        with mock.patch.object(redact_mod, "_FILE_EXTS", frozenset()):
            self.assertNotEqual(std("edit appsettings.local.json"),
                                "edit appsettings.local.json")

    def test_card_prefix_matters(self):
        with mock.patch.object(redact_mod, "_CARD_PREFIX_RE",
                               re.compile(r"")):
            # a 16-digit Luhn-valid number that is not a card brand
            self.assertEqual(std("id 7992739871300008"), "id {card}")
        self.assertEqual(std("id 7992739871300008"), "id 7992739871300008")

    def test_private_ip_rule_matters(self):
        with mock.patch.object(redact_mod, "_private_ip", lambda m: True):
            self.assertEqual(std("pin 8.8.8.8"), "pin {ip}")
        self.assertEqual(std("pin 8.8.8.8"), "pin 8.8.8.8")

    def test_off_profile_really_is_off(self):
        self.assertEqual(std("SSN 123-45-6789", "off"), "SSN 123-45-6789")


# ---- MCP manners -------------------------------------------------------------

def rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method,
            "params": params if params is not None else {}}


class TestMcpManners(unittest.TestCase):
    def test_protocol_version_is_negotiated(self):
        for asked in mcp_mod.PROTOCOL_VERSIONS:
            r = mcp_mod.handle(rpc("initialize",
                                   {"protocolVersion": asked}), None)
            self.assertEqual(r["result"]["protocolVersion"], asked)
        for odd in ("1999-01-01", None, 7, ["x"]):
            r = mcp_mod.handle(rpc("initialize", {"protocolVersion": odd}),
                               None)
            self.assertEqual(r["result"]["protocolVersion"],
                             mcp_mod.PROTOCOL_VERSIONS[0])
        r = mcp_mod.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": "nonsense"}, None)
        self.assertIn("protocolVersion", r["result"])
        self.assertEqual(r["result"]["serverInfo"]["version"],
                         standupguy.__version__)

    def test_the_tool_says_it_only_reads(self):
        tool = mcp_mod.handle(rpc("tools/list"), None)["result"]["tools"][0]
        self.assertEqual(tool["annotations"],
                         {"readOnlyHint": True, "destructiveHint": False,
                          "idempotentHint": True, "openWorldHint": False})

    def _repo_with_secrets(self):
        tmp = make_repo()
        hook = "https://hooks.slack" + ".com/services/T000/B000/" + "X" * 24
        git(tmp, "commit", "--allow-empty", "-qm",
            f"fix: call {hook} for Initech", env={
                "GIT_AUTHOR_DATE": "2026-10-14T17:00:00",
                "GIT_COMMITTER_DATE": "2026-10-14T17:00:00"})
        return tmp, hook

    def _ask(self, tmp, opts, **arguments):
        args = {"since": "2026-10-14", "until": "2026-10-15", **arguments}
        r = mcp_mod.handle(rpc("tools/call", {"name": "what_did_i_ship",
                                              "arguments": args}),
                           [tmp], opts)
        return r["result"]["content"][0]["text"]

    def test_the_owner_picks_redaction_the_agent_cannot_turn_it_off(self):
        from standupguy import digest as digest_mod
        tmp, hook = self._repo_with_secrets()
        opts = digest_mod.Options(
            redactor=redact_mod.make("standard", ["Initech"]), escape=False)
        text = self._ask(tmp, opts)
        self.assertNotIn("hooks.slack", text)
        self.assertNotIn("Initech", text)
        # an agent asking for "redact: off" (or anything else) changes nothing
        text = self._ask(tmp, opts, redact="off", redact_terms=[])
        self.assertNotIn("hooks.slack", text)
        self.assertNotIn("Initech", text)

    def test_mcp_command_reads_redact_flags(self):
        captured = {}

        def fake_serve(roots, opts=None, **kw):
            captured["opts"] = opts
            return 0
        tmp = make_repo()
        with mock.patch.object(mcp_mod, "serve", fake_serve):
            code, _, _ = run_cli("mcp", "--root", tmp, "--redact", "strict",
                                 "--redact-term", "Initech")
        self.assertEqual(code, 0)
        red = captured["opts"].redactor
        self.assertEqual(red.profile, "strict")
        self.assertNotIn("initech", red.text("an Initech thing").lower())
        self.assertFalse(captured["opts"].escape)
        with mock.patch.dict(os.environ, {"STANDUPGUY_REDACT": "off"}), \
                mock.patch.object(mcp_mod, "serve", fake_serve):
            run_cli("mcp", "--root", tmp)
        self.assertEqual(captured["opts"].redactor.profile, "off")


# ---- git forensics: shallow clones -------------------------------------------

class TestShallowClone(unittest.TestCase):
    def test_shallow_clone_gets_a_note_and_a_full_clone_does_not(self):
        src = make_repo()
        clone = tempfile.mkdtemp()
        env = dict(os.environ)
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        "file://" + src.replace(os.sep, "/"),
                        os.path.join(clone, "c")], check=True, env=env,
                       capture_output=True)
        c = os.path.join(clone, "c")
        git(c, "config", "user.email", "dan@example.com")
        git(c, "config", "user.name", "Dan")
        code, out, err = run_cli("--root", c, "--since", "2026-10-01",
                                 "--all")
        self.assertEqual(code, 0)
        self.assertIn("shallow clone", err)
        self.assertIn("git fetch --unshallow", err)
        # the digest itself is unchanged by the note, and JSON stays clean
        code, out, err = run_cli("--root", c, "--since", "2026-10-01",
                                 "--all", "--format", "json")
        json.loads(out)
        code, out, err = run_cli("--root", src, "--since", "2026-10-01",
                                 "--all")
        self.assertNotIn("shallow", err)

    def test_is_shallow_is_false_when_git_cannot_say(self):
        from standupguy import gitlog
        self.assertFalse(gitlog.is_shallow(tempfile.mkdtemp()))


# ---- --by-author: the engineering manager's view --------------------------

class TestByAuthor(unittest.TestCase):
    def _team(self):
        from test_standupguy import commit
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        git(tmp, "config", "user.email", "ann@example.com")
        git(tmp, "config", "user.name", "Ann")
        plan = [("Ann", "ann@example.com", "feat: one"),
                ("Bo", "bo@example.com", "fix: two"),
                ("Bo", "BO@example.com", "fix: three"),      # same person
                ("Cy [bot]", "cy@example.com", "chore: four @channel")]
        for i, (n, e, m) in enumerate(plan):
            commit(tmp, n, e, f"2026-10-14T1{i}:00:00", m, {f"f{i}": str(i)})
        return tmp

    def _run(self, tmp, *extra):
        return run_cli("--root", tmp, "--since", "2026-10-14", "--until",
                       "2026-10-15", "--all", "--no-inflight", *extra)

    def test_one_section_per_person_busiest_first(self):
        code, out, _ = self._run(self._team(), "--by-author")
        self.assertEqual(code, 0)
        heads = re.findall(r"(?m)^## Shipped - (.*)$", out)
        self.assertEqual(heads, ["Bo", "Ann", "Cy [bot]"])
        bo = out.split("## Shipped - Bo")[1].split("## Shipped - Ann")[0]
        self.assertIn("two", bo)
        self.assertIn("three", bo)
        self.assertNotIn("one", bo)
        self.assertIn("[at]channel", out)           # still neutralised

    def test_without_the_flag_nothing_changes(self):
        tmp = self._team()
        _, plain, _ = self._run(tmp)
        self.assertNotIn("Shipped -", plain)
        self.assertEqual(plain.count("## Shipped"), 1)

    def test_a_single_author_gets_the_ordinary_layout(self):
        tmp = make_repo()
        _, a, _ = run_cli("--root", tmp, "--since", "2026-10-14", "--no-inflight")
        _, b, _ = run_cli("--root", tmp, "--since", "2026-10-14", "--no-inflight",
                          "--by-author")
        self.assertEqual(a, b)

    def test_strict_shows_no_names(self):
        code, out, _ = self._run(self._team(), "--by-author", "--redact",
                                 "strict")
        self.assertEqual(re.findall(r"(?m)^## Shipped - (.*)$", out),
                         ["person-1", "person-2", "person-3"])
        for name in ("Ann", "Bo", "Cy"):
            self.assertNotIn(name, out)

    def test_clashing_flags_are_errors(self):
        tmp = self._team()
        for extra in (("--style", "ytb"), ("--week",), ("--sprint",)):
            code, out, err = self._run(tmp, "--by-author", *extra)
            self.assertEqual(code, 2, extra)
            self.assertIn("--by-author cannot be combined", err)
            self.assertEqual(out, "")


# ---- local-first is a promise: no network without --draft / --push-slack -----

class TestNoNetwork(unittest.TestCase):
    def test_digest_formats_and_mcp_never_open_a_socket(self):
        import socket

        def refuse(*a, **k):
            raise AssertionError("a socket was opened")
        tmp = make_repo()
        with mock.patch.object(socket, "socket", refuse), \
                mock.patch.object(socket, "create_connection", refuse), \
                mock.patch.object(socket, "getaddrinfo", refuse):
            for argv in (["--since", "2026-10-14"],
                         ["--since", "2026-10-14", "--format", "slack"],
                         ["--since", "2026-10-14", "--format", "json"],
                         ["--since", "2026-10-14", "--style", "ytb", "--all"],
                         ["--since", "2026-10-14", "--all", "--by-author"],
                         ["--week"], ["--sprint"],
                         ["--since", "2026-10-14", "--draft", "--dry-run"],
                         ["--since", "2026-10-14", "--push-slack",
                          "https://hooks.example/x", "--dry-run"],
                         ["doctor"]):
                code, out, err = run_cli("--root", tmp, *argv)
                self.assertIn(code, (0, 2), (argv, err))
                self.assertNotIn("a socket was opened", err + out, argv)
            r = mcp_mod.handle(rpc("tools/call", {
                "name": "what_did_i_ship",
                "arguments": {"since": "2026-10-14"}}), [tmp])
            self.assertNotIn("isError", r["result"])


# ---- a non-UTF-8 locale and a non-ASCII folder name --------------------------

class TestOddLocale(unittest.TestCase):
    def test_ascii_locale_with_accented_folders_and_a_linked_worktree(self):
        """LC_ALL=C with Python's UTF-8 modes off made the file system's
        encoding ASCII; the path git printed for the repo (`caf\u00e9`) then
        raised UnicodeEncodeError in the worktree check: a traceback instead
        of a digest."""
        base = tempfile.mkdtemp(prefix="sp ace ")
        repo = os.path.join(base, "caf\u00e9 repo")
        os.mkdir(repo)
        git(repo, "init", "-qb", "main")
        git(repo, "config", "user.email", "dan@example.com")
        git(repo, "config", "user.name", "Dan")
        from test_standupguy import commit
        commit(repo, "Dan", "dan@example.com", "2026-10-14T10:00:00",
               "feat: first", {"a.txt": "a"})
        git(repo, "worktree", "add", "-q", "-b", "side",
            os.path.join(base, "t\u00e4st wt"))
        commit(os.path.join(base, "t\u00e4st wt"), "Dan", "dan@example.com",
               "2026-10-14T11:00:00", "feat: on the side", {"b.txt": "b"})
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0",
                   PYTHONCOERCECLOCALE="0", PYTHONPATH=REPO)
        env.pop("PYTHONIOENCODING", None)
        for argv in (["--since", "2026-10-14", "--until", "2026-10-15"],
                     ["--since", "2026-10-14", "--until", "2026-10-15",
                      "--format", "json"]):
            p = subprocess.run([sys.executable, "-m", "standupguy",
                                "--root", repo, *argv], env=env,
                               capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8",
                                                              "replace"))
            self.assertNotIn(b"Traceback", p.stderr)
            self.assertIn(b"first", p.stdout)


# ---- git forensics: what the history says, not a replacement ref -------------

class TestReplaceRefs(unittest.TestCase):
    def test_a_replace_ref_cannot_rewrite_a_commit_in_the_digest(self):
        from test_standupguy import commit
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        git(tmp, "config", "user.email", "dan@example.com")
        git(tmp, "config", "user.name", "Dan")
        commit(tmp, "Dan", "dan@example.com", "2026-10-14T10:00:00",
               "feat: the real subject", {"a": "1"})
        real = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        commit(tmp, "Dan", "dan@example.com", "2026-10-14T11:00:00",
               "feat: forged subject", {"b": "2"})
        fake = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        git(tmp, "replace", real, fake)
        code, out, _ = run_cli("--root", tmp, "--since", "2026-10-14",
                               "--until", "2026-10-15", "--no-inflight",
                               "--no-fold")
        self.assertIn("the real subject", out)
        self.assertEqual(out.count("forged subject"), 1)    # its own commit


# ---- differential test against an independent reference ----------------------

class TestWindowDifferential(unittest.TestCase):
    def test_plain_date_windows_match_a_python_reference(self):
        """40 seeded commits by 3 people over 12 days. For 25 seeded
        (since, until, author) triples the shas standupguy returns must equal
        the shas an independent filter (plain Python on the dates and e-mails
        written into the repo) says belong in the window: a bare `since` date
        starts at local midnight, a bare `until` date includes that whole
        day."""
        import datetime
        from test_standupguy import commit
        from standupguy import gitlog
        rng = random.Random(5150)
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        git(tmp, "config", "user.email", "a@example.com")
        git(tmp, "config", "user.name", "A")
        people = [("A", "a@example.com"), ("B", "b@example.com"),
                  ("C", "c@example.org")]
        rows = []
        stamps = sorted(
            datetime.datetime(2026, 10, 1) + datetime.timedelta(
                minutes=rng.randint(0, 12 * 24 * 60)) for _ in range(40))
        for i, ts in enumerate(stamps):
            who = rng.choice(people)
            commit(tmp, who[0], who[1], ts.strftime("%Y-%m-%dT%H:%M:%S"),
                   f"feat: change {i}", {f"f{i}.txt": str(i)})
            rows.append((ts, who[1]))
        shas = gitlog.commits_since(tmp, "2026-09-01", None, None)
        by_subject = {c.subject: c.sha for c in shas}
        self.assertEqual(len(by_subject), 40)
        for _ in range(25):
            d1 = datetime.date(2026, 10, rng.randint(1, 12))
            d2 = d1 + datetime.timedelta(days=rng.randint(0, 6))
            email = rng.choice([None, "a@example.com", "b@example.com",
                                "C@EXAMPLE.ORG"])
            want = {by_subject[f"feat: change {i}"]
                    for i, (ts, em) in enumerate(rows)
                    if d1 <= ts.date() <= d2
                    and (email is None or em == email.lower())}
            got = {c.sha for c in gitlog.commits_since(
                tmp, d1.isoformat(), email, d2.isoformat())}
            self.assertEqual(got, want, (d1, d2, email))


# ---- the 0.1.0 -> now upgrade path ------------------------------------------

#: the keys `--format json` had in the version on PyPI (0.1.0)
JSON_KEYS_0_1_0 = {"repo", "label", "commits", "in_progress"}
COMMIT_KEYS_0_1_0 = {"sha", "author", "date", "subject", "files", "pr"}


class TestUpgradeFromPublished(unittest.TestCase):
    def test_json_still_has_every_key_the_published_version_had(self):
        tmp = make_repo()
        code, out, _ = run_cli("--root", tmp, "--since", "2026-10-01",
                               "--all", "--format", "json")
        data = json.loads(out)
        self.assertTrue(JSON_KEYS_0_1_0 <= set(data), set(data))
        self.assertTrue(COMMIT_KEYS_0_1_0 <= set(data["commits"][0]),
                        set(data["commits"][0]))

    def test_the_tool_name_and_arguments_of_0_1_0_still_work(self):
        tmp = make_repo()
        r = mcp_mod.handle(rpc("tools/call", {
            "name": "what_did_i_ship",
            "arguments": {"since": "2026-10-14", "author": "dan@example.com",
                          "root": tmp}}), [tmp])
        self.assertNotIn("isError", r["result"])
        self.assertIn("add login flow", r["result"]["content"][0]["text"])

    def test_environment_variables_of_0_1_0_are_still_read(self):
        from standupguy import draft
        with mock.patch.dict(os.environ, {"AUTOSTANDUP_MODEL": "m-x",
                                          "AUTOSTANDUP_BASE_URL":
                                          "http://localhost:1/v1"}):
            cfg = draft.dry_run("x", base_url=None, model=None)
        self.assertIn("m-x", cfg)


# ---- golden files ------------------------------------------------------------

MD_INPUT = """# Standup \u2014 demo
_since 2026-10-14_

## Shipped
### Features
- add **login** flow, see [docs]\\(http://x) [PR #12] (2 files) `abc1234`
- ping @channel and <!here> for \\<b> (1 file) `def5678`

### Fixes
- null pointer on _logout_ [ABC-9] (1 file) `0123456`

## In progress
- `wip.py`

## Blockers
_none reported \u2014 fill in yours_
"""


class TestGoldenFormats(unittest.TestCase):
    def _golden(self, name, text):
        path = os.path.join(GOLDEN, name)
        if os.environ.get("UPDATE_GOLDEN") == "1":
            os.makedirs(GOLDEN, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        with open(path, encoding="utf-8") as f:     # CRLF checkouts read as LF
            self.assertEqual(text, f.read(), f"{name} changed; if on purpose "
                             "rerun with UPDATE_GOLDEN=1 and read the diff")

    def test_each_format_matches_its_golden_file(self):
        for fmt in formats_mod.FORMATS:
            self._golden(f"convert-{fmt}.txt",
                         formats_mod.convert(MD_INPUT, fmt))

    def test_whole_digest_matches_its_golden_file(self):
        tmp = make_repo()
        for style in ("standup", "ytb"):
            code, out, _ = run_cli("--root", tmp, "--since", "2026-10-14",
                                   "--until", "2026-10-15", "--all",
                                   "--no-inflight", "--style", style)
            self.assertEqual(code, 0)
            out = re.sub(r"(?m)^(# [^\n]*?\u2014 ).*$", r"\1REPO", out)
            out = re.sub(r"`[0-9a-f]{7,40}`", "`SHA`", out)
            self._golden(f"digest-{style}.txt", out)

    def test_json_keys_are_golden(self):
        tmp = make_repo()
        code, out, _ = run_cli("--root", tmp, "--since", "2026-10-14",
                               "--until", "2026-10-15", "--all",
                               "--format", "json")
        data = json.loads(out)
        shape = {"top": sorted(data), "commit": sorted(data["commits"][0])}
        self._golden("json-keys.txt", json.dumps(shape, indent=1) + "\n")


if __name__ == "__main__":
    unittest.main()
