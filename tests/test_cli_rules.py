"""Things the command line refuses, and says so.

Panel finding SG-5: `--format json` returned before `--draft` and
`--push-slack` were looked at: nothing was drafted, nothing was posted, no
message, exit code 0. A workflow that believed it had posted to Slack had not.
Now the combination is an error (exit 2) before git is run or anything is sent.
"""

import json
import os
import unittest
from unittest import mock

from test_secrets_and_redirects import Fake
from test_round5 import run_cli
from test_standupguy import make_repo  # also isolates git config/HOME

from standupguy import cli as cli_mod
from standupguy import gitlog as gitlog_mod

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


if __name__ == "__main__":
    unittest.main()
