"""Things the command line refuses, and says so.

Panel finding SG-5: `--format json` returned before `--draft` and
`--push-slack` were looked at: nothing was drafted, nothing was posted, no
message, exit code 0. A workflow that believed it had posted to Slack had not.
Now the combination is an error (exit 2) before git is run or anything is sent.

Round-2 panel: the same symptom for an EMPTY value. `--push-slack "$HOOK"`
with $HOOK unset passed "", which is falsy, so nothing was posted, nothing was
said and the exit code was 0. Every option that takes a value now refuses an
empty one (exit 2) instead of quietly meaning "the default".
"""

import ast
import json
import os
import unittest
from unittest import mock

from test_secrets_and_redirects import Fake
from test_round5 import run_cli
from test_standupguy import make_repo  # also isolates git config/HOME

from standupguy import cli as cli_mod
from standupguy import gitlog as gitlog_mod
from standupguy import push as push_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW = ("--since", "2026-10-14")


class TestJsonIsTheFinishedOutput(unittest.TestCase):
    def refused(self, *argv, env=None):
        with mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(gitlog_mod, "_git",
                                  side_effect=AssertionError("git ran")):
            return run_cli("--root", "/nonexistent-repo", *argv)

    def test_json_with_draft_is_refused_before_anything_runs(self):
        model = Fake()
        self.addCleanup(model.stop)
        code, out, err = self.refused(*WINDOW, "--format", "json", "--draft",
                                      "--base-url", model.url)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: "), err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("--format json", err)
        self.assertIn("--draft", err)
        self.assertNotIn("--push-slack", err)
        self.assertEqual(model.requests, [])

    def test_json_with_push_slack_is_refused_and_posts_nothing(self):
        hook = Fake()
        self.addCleanup(hook.stop)
        for argv, env in (
                (("--push-slack", hook.url + "/hook"), {}),
                (("--push-slack",), {"AUTOSTANDUP_SLACK_WEBHOOK": hook.url + "/hook"})):
            with self.subTest(argv=argv):
                code, out, err = self.refused(*WINDOW, "--format", "json",
                                              *argv, env=env)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith("error: "), err)
                self.assertIn("--push-slack", err)
                self.assertNotIn("--draft", err)
                self.assertNotIn(hook.url, err)
        self.assertEqual(hook.requests, [])

    def test_both_flags_are_named(self):
        code, out, err = self.refused(*WINDOW, "--format", "json", "--draft",
                                      "--push-slack", "https://hooks.example.test/x")
        self.assertEqual(code, 2)
        self.assertIn("--draft", err)
        self.assertIn("--push-slack", err)
        self.assertNotIn("hooks.example.test", err)

    def test_the_order_of_the_flags_does_not_matter(self):
        code, out, err = self.refused("--draft", *WINDOW, "--format=json")
        self.assertEqual(code, 2)
        self.assertIn("--draft", err)

    def test_json_alone_and_the_other_flags_alone_still_work(self):
        repo = make_repo()
        code, out, err = run_cli("--root", repo, *WINDOW, "--format", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(len(json.loads(out)["commits"]), 2)
        model = Fake()
        self.addCleanup(model.stop)
        code, out, err = run_cli("--root", repo, *WINDOW, "--format", "markdown",
                                 "--draft", "--base-url", model.url)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("Yesterday", out)

    def test_serve_and_mcp_are_not_digest_runs(self):
        # --format only means something for a digest; `serve` and `mcp` have
        # always ignored it, and still do
        for cmd in ("serve", "mcp"):
            with self.subTest(cmd=cmd), \
                    mock.patch("standupguy.cli.serve_mod.serve", return_value=0), \
                    mock.patch("standupguy.cli.mcp_mod.serve", return_value=0):
                code, out, err = run_cli("--root", make_repo(), cmd,
                                         "--format", "json", "--draft")
                self.assertEqual((code, err), (0, ""))

    def test_the_readme_and_the_help_say_so(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            flat = " ".join(f.read().split())
        self.assertTrue("cannot be combined with `--draft` or `--push-slack`" in flat,
                        "the README does not say it")
        helptext = " ".join(cli_mod.build_parser().format_help().split())
        self.assertTrue("not with --draft or --push-slack" in helptext,
                        "--help does not say it")


class TestAnEmptyValueIsAMistake(unittest.TestCase):
    # option -> what the message says it needs
    OPTIONS = (("--root", "a repo path"),
               ("--author", "a name or e-mail address"),
               ("--since", "a git date"),
               ("--until", "a git date"),
               ("--push-slack", "a webhook URL"),
               ("--base-url", "a model endpoint URL"),
               ("--model", "a model name"),
               ("--host", "a bind address"))

    def refused(self, *argv, env=None):
        # nothing may run: no git, no server, no request, no post
        with mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(gitlog_mod, "_git",
                                  side_effect=AssertionError("git ran")), \
                mock.patch.object(push_mod, "push_slack",
                                  side_effect=AssertionError("posted")), \
                mock.patch("standupguy.cli.serve_mod.serve",
                           side_effect=AssertionError("serve ran")), \
                mock.patch("standupguy.cli.mcp_mod.serve",
                           side_effect=AssertionError("mcp ran")):
            return run_cli(*argv)

    def test_an_empty_push_slack_posts_nothing_and_says_so(self):
        # the panel's repro: `standupguy --push-slack ""` printed the digest
        # and exited 0. The environment holds a working webhook, and an empty
        # value must not quietly fall back to that either.
        code, out, err = self.refused(
            "--root", make_repo(), "--all", *WINDOW, "--push-slack", "",
            env={"AUTOSTANDUP_SLACK_WEBHOOK": "https://hooks.slack.com/x"})
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertTrue(err.startswith("error: --push-slack needs a webhook "
                                       "URL"), err)
        self.assertIn("empty", err)
        self.assertNotIn("hooks.slack.com", err)

    def test_every_option_that_takes_a_value_refuses_an_empty_one(self):
        for flag, what in self.OPTIONS:
            for value in ("", "   "):
                for cmd in ((), ("serve",), ("mcp",)):
                    with self.subTest(flag=flag, value=value, cmd=cmd):
                        code, out, err = self.refused(*cmd, flag, value)
                        self.assertEqual(code, 2, err)
                        self.assertEqual(out, "")
                        self.assertEqual(len(err.strip().splitlines()), 1, err)
                        self.assertTrue(
                            err.startswith(f"error: {flag} needs {what}, "),
                            err)
                        self.assertIn("empty", err)

    def test_one_empty_root_among_good_ones_is_refused(self):
        code, out, err = self.refused("--root", make_repo(), "--root", "")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: --root needs a repo path"), err)

    def test_the_empty_value_is_named_before_a_conflict_with_json(self):
        # `--format json --push-slack ""` has two things wrong; the empty
        # value is the cause, so it is the one that is named
        code, out, err = self.refused("--format", "json", "--push-slack", "")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--push-slack needs a webhook URL", err)
        self.assertNotIn("--format json", err)

    def test_a_bare_push_slack_still_means_the_environment_variable(self):
        repo = make_repo()
        hook = "https://hooks.slack.com/services/T/x"
        with mock.patch.dict(os.environ, {"AUTOSTANDUP_SLACK_WEBHOOK": hook}), \
                mock.patch.object(push_mod, "push_slack") as push:
            code, out, err = run_cli("--root", repo, "--all", *WINDOW,
                                     "--push-slack")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(push.call_args[0][0], hook)

    def test_a_missing_webhook_is_still_the_error_it_was(self):
        # an empty environment variable counts as not set
        with mock.patch.dict(os.environ, {"AUTOSTANDUP_SLACK_WEBHOOK": ""}), \
                mock.patch.object(push_mod, "push_slack",
                                  side_effect=AssertionError("posted")):
            code, out, err = run_cli("--root", make_repo(), "--all", *WINDOW,
                                     "--push-slack")
        self.assertEqual(code, 2)
        self.assertIn("no webhook", err)

    def test_a_real_value_is_not_touched(self):
        repo = make_repo()
        code, out, err = run_cli("--root", repo, "--author", "dan@example.com",
                                 "--since", "2026-10-14", "--until",
                                 "2026-10-15", "--model", "m")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("add login flow", out)

    def test_no_option_that_takes_a_value_is_tested_for_truthiness(self):
        # the pattern behind the finding: `if args.push_slack:` is False for
        # "", so an empty value looked exactly like no value. Ask with
        # `is not None` (the lists --root and --allow-host cannot be empty).
        with open(os.path.join(REPO, "standupguy", "cli.py"),
                  encoding="utf-8") as f:
            tree = ast.parse(f.read())
        text_options = {"author", "since", "until", "push_slack", "base_url",
                        "model", "host", "token"}

        def bare(node):
            return (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "args" and node.attr in text_options)

        found = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                tested = [node.test]
            elif isinstance(node, ast.BoolOp):
                tested = node.values
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                tested = [node.operand]
            elif isinstance(node, ast.comprehension):
                tested = node.ifs
            else:
                continue
            found += [(t.lineno, t.attr) for t in tested if bare(t)]
        self.assertEqual(found, [], "an option is tested for truthiness "
                                    "(use `is not None`): line, option")

    def test_the_readme_and_the_help_say_so(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            flat = " ".join(f.read().split())
        for phrase in ("gets an empty one", "never a quiet fall-back",
                       "a bare `--push-slack` posts to"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")
        helptext = " ".join(cli_mod.build_parser().format_help().split())
        self.assertTrue("An empty value is an error" in helptext,
                        "--help does not say it")


if __name__ == "__main__":
    unittest.main()
