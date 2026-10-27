"""Round 9 regressions.

R9-1 `--author X` listed the local user's uncommitted files as X's work.
R9-2 STANDUPGUY_* env vars (AUTOSTANDUP_* stay as fallbacks).
R9-3 A plain-http, non-local model URL warns that the API key is unencrypted.
R9-4 README: no CRLF bullet, absolute workflow link on PyPI, new env names.
"""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from test_standupguy import cli_mod, draft_mod, gitlog_mod, make_repo, mcp_mod
from standupguy import config as config_mod
from standupguy import serve as serve_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestInProgressBelongsToTheCurrentUser(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()        # user dan@example.com, wip.py uncommitted

    def test_default_author_shows_it(self):
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01")
        self.assertIn("In progress", out)
        self.assertIn("wip.py", out)

    def test_other_author_does_not(self):
        code, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                               "--author", "t@example.com")
        self.assertEqual(code, 0)
        self.assertNotIn("In progress", out)
        self.assertNotIn("wip.py", out)

    def test_json_and_sprint_follow_the_same_rule(self):
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--author", "t@example.com", "--format", "json")
        self.assertEqual(json.loads(out)["in_progress"], [])
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--author", "t@example.com", "--sprint")
        self.assertNotIn("wip.py", out)

    def test_own_email_name_and_all_show_it(self):
        for extra in (["--author", "dan@example.com"], ["--author", "DAN"],
                      ["--all"]):
            _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                                *extra)
            self.assertIn("wip.py", out, extra)

    def test_a_substring_of_my_email_is_not_me(self):
        self.assertFalse(gitlog_mod.is_current_user(self.repo, "dan"[:2]))
        self.assertEqual(gitlog_mod.uncommitted_for(self.repo, "an@"), [])

    def test_mcp_author_argument(self):
        sections = mcp_mod._call_tool(
            {"since": "2026-10-01", "author": "t@example.com"}, [self.repo])
        self.assertNotIn("wip.py", json.dumps(sections))
        sections = mcp_mod._call_tool({"since": "2026-10-01"}, [self.repo])
        self.assertIn("wip.py", json.dumps(sections))

    def test_serve_author_param(self):
        status, _, body = serve_mod._payload(
            [self.repo], {"since": ["2026-10-01"], "author": ["t@example.com"]},
            False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["in_progress"], [])
        _, _, body = serve_mod._payload(
            [self.repo], {"since": ["2026-10-01"]}, False)
        self.assertIn("wip.py", json.loads(body)["in_progress"])


class TestEnvNames(unittest.TestCase):
    def test_new_name_wins_old_name_falls_back(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("STANDUPGUY_MODEL", "AUTOSTANDUP_MODEL"):
                os.environ.pop(k, None)
            self.assertEqual(config_mod.env("MODEL", "d"), "d")
            os.environ["AUTOSTANDUP_MODEL"] = "old"
            self.assertEqual(config_mod.env("MODEL"), "old")
            os.environ["STANDUPGUY_MODEL"] = "new"
            self.assertEqual(config_mod.env("MODEL"), "new")
            os.environ["STANDUPGUY_MODEL"] = ""       # empty = not set
            self.assertEqual(config_mod.env("MODEL"), "old")

    def test_draft_config_reads_both(self):
        with mock.patch.dict(os.environ, {"STANDUPGUY_BASE_URL": "http://h/v1/",
                                          "AUTOSTANDUP_API_KEY": "k"}):
            os.environ.pop("AUTOSTANDUP_BASE_URL", None)
            cfg = draft_mod._cfg()
        self.assertEqual(cfg["base_url"], "http://h/v1")
        self.assertEqual(cfg["api_key"], "k")

    def test_slack_webhook_new_name(self):
        env = {"STANDUPGUY_SLACK_WEBHOOK": ""}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("AUTOSTANDUP_SLACK_WEBHOOK", None)
            code, _, err = run_cli("--root", make_repo(), "--push-slack")
        self.assertEqual(code, 2)
        self.assertIn("STANDUPGUY_SLACK_WEBHOOK", err)


class TestInsecureKeyWarning(unittest.TestCase):
    w = staticmethod(draft_mod.insecure_key_warning)

    def test_warns_for_plain_http_remote_with_a_key(self):
        msg = self.w("http://models.example.com/v1", "sk-real")
        self.assertIn("unencrypted", msg)
        self.assertNotIn("sk-real", msg)

    def test_quiet_for_local_https_or_default_key(self):
        self.assertIsNone(self.w("http://localhost:11434/v1", "sk-real"))
        self.assertIsNone(self.w("http://127.0.0.1:1/v1", "sk-real"))
        self.assertIsNone(self.w("https://api.example.com/v1", "sk-real"))
        self.assertIsNone(self.w("http://models.example.com/v1", "ollama"))

    def test_draft_prints_the_warning_before_sending(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                self.assertRaises(draft_mod.DraftError):
            draft_mod.draft("# x", base_url="http://models.invalid/v1",
                            api_key="sk-real", timeout=2)
        self.assertIn("unencrypted", err.getvalue())


class TestReadmeAndMetadata(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            self.readme = f.read()

    def test_no_crlf_bullet(self):
        self.assertNotIn("Line endings", self.readme)

    def test_workflow_link_is_absolute(self):
        self.assertIn("](https://github.com/calasdan-sketch/standupguy/blob/"
                      "main/workflows/activepieces-standup-digest.json)",
                      self.readme)
        self.assertNotIn("](workflows/", self.readme)

    def test_both_env_names_documented_and_pip_install(self):
        for name in ("STANDUPGUY_API_KEY", "AUTOSTANDUP_API_KEY",
                     "STANDUPGUY_SLACK_WEBHOOK"):
            self.assertIn(name, self.readme)
        self.assertIn("pip install standupguy", self.readme)

    def test_metadata(self):
        with open(os.path.join(REPO, "pyproject.toml"), encoding="utf-8") as f:
            py = f.read()
        for needle in ("Calas Automations Inc.", "Python :: 3.14", "Changelog"):
            self.assertIn(needle, py)


if __name__ == "__main__":
    unittest.main()
