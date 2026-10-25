"""0.2.0: the agent ledger, verify mode, the Claude Code plugin and the
GitHub Actions example."""

import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_standupguy import (cli_mod, commit, digest_mod, draft_mod,  # noqa: F401
                             git, gitlog_mod, make_repo)

from standupguy import push as push_mod
from standupguy import serve as serve_mod  # noqa: F401
from standupguy import verify as verify_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def agent_repo():
    """3 commits on 2026-10-14: two credit Claude (one of them as Opus)."""
    tmp = make_repo()
    commit(tmp, "Dan", "dan@example.com", "2026-10-15T09:00:00",
           f"feat: add billing export\n\nbody text\n\n{CLAUDE}",
           {"billing/export.py": "1"})
    commit(tmp, "Dan", "dan@example.com", "2026-10-15T10:00:00",
           "fix: retry on timeout\n\nCo-authored-by: Claude Opus 4.5 "
           "<noreply@anthropic.com>\nCo-authored-by: Pat <pat@example.com>",
           {"net/retry.py": "1"})
    commit(tmp, "Dan", "dan@example.com", "2026-10-15T11:00:00",
           "docs: explain the export", {"docs/export.md": "1"})
    return tmp


# ---------------------------------------------------------------- ledger
class TestTrailerParsing(unittest.TestCase):
    def test_claude_is_recognised_by_name_or_address(self):
        for line in ("Co-Authored-By: Claude <noreply@anthropic.com>",
                     "co-authored-by: Claude Opus 4.5 <x@y.z>",
                     "CO-AUTHORED-BY: Some Bot <bot@anthropic.com>",
                     "Co-Authored-By: claude-code <whatever@example.com>"):
            self.assertEqual(len(gitlog_mod.agents_in_message(
                "subject\n\n" + line)), 1, line)

    def test_other_people_and_other_lines_are_not_counted(self):
        for text in ("subject\n\nCo-Authored-By: Pat <pat@example.com>",
                     "subject\n\nSigned-off-by: Claude <noreply@anthropic.com>",
                     "subject mentions Claude but has no trailer",
                     "subject\n\nthanks to Claude (Co-Authored-By: Claude)",
                     ""):
            self.assertEqual(gitlog_mod.agents_in_message(text), [], text)

    def test_the_same_agent_twice_counts_once_and_two_agents_count_both(self):
        msg = ("s\n\nCo-Authored-By: Claude A <a@anthropic.com>\n"
               "Co-Authored-By: Claude A <a@anthropic.com>\n"
               "Co-Authored-By: Claude B <b@anthropic.com>")
        self.assertEqual(gitlog_mod.agents_in_message(msg),
                         ["Claude A", "Claude B"])

    def test_a_label_is_one_printable_short_line(self):
        msg = "s\n\nCo-Authored-By: Claude\x1b[31m\t" + "x" * 200 + " <a@b>"
        (label,) = gitlog_mod.agents_in_message(msg)
        self.assertTrue(label.isprintable())
        self.assertLessEqual(len(label), 60)

    def test_an_unnamed_trailer_gets_a_plain_label(self):
        (label,) = gitlog_mod.agents_in_message(
            "s\n\nCo-Authored-By: <noreply@anthropic.com>")
        self.assertEqual(label, "Claude")


class TestLedgerOnARealRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = agent_repo()
        self.commits = gitlog_mod.commits_since(self.tmp, "2026-10-15",
                                                "dan@example.com")

    def test_counts_commits_and_agents(self):
        led = gitlog_mod.agent_ledger(self.tmp, self.commits)
        self.assertEqual(led, {"commits": 3, "with_agent": 2, "agents": {
            "Claude Opus 4.5": 1, "Claude Sonnet 5.5": 1}})

    def test_same_answer_every_time(self):
        a = gitlog_mod.agent_ledger(self.tmp, self.commits)
        gitlog_mod._ledger.cache_clear()
        b = gitlog_mod.agent_ledger(self.tmp, list(reversed(self.commits)))
        self.assertEqual(a, b)

    def test_no_commits_is_an_empty_ledger(self):
        self.assertEqual(gitlog_mod.agent_ledger(self.tmp, []),
                         {"commits": 0, "with_agent": 0, "agents": {}})

    def test_a_message_cannot_credit_a_different_commit(self):
        tmp = make_repo()
        commit(tmp, "Dan", "dan@example.com", "2026-10-15T09:00:00",
               "feat: one", {"a.py": "1"})
        cs = gitlog_mod.commits_since(tmp, "2026-10-15", "dan@example.com")
        victim = cs[0].sha
        commit(tmp, "Dan", "dan@example.com", "2026-10-15T10:00:00",
               f"feat: two\n\n\x1e{victim}\x1f\n{CLAUDE}", {"b.py": "1"})
        cs = gitlog_mod.commits_since(tmp, "2026-10-15", "dan@example.com")
        led = gitlog_mod.agent_ledger(tmp, cs)
        self.assertEqual(led["with_agent"], 1)        # only the real one
        self.assertEqual(led["commits"], 2)

    def test_git_failing_gives_none_not_a_crash(self):
        gitlog_mod._ledger.cache_clear()
        with mock.patch.object(gitlog_mod, "_git",
                               side_effect=gitlog_mod.GitError("log", "boom")):
            self.assertIsNone(gitlog_mod.agent_ledger(self.tmp, self.commits))
        gitlog_mod._ledger.cache_clear()


class TestLedgerInTheDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = agent_repo()
        gitlog_mod._ledger.cache_clear()

    def test_daily_digest_has_the_block(self):
        code, out, _ = run_cli("--root", self.tmp, "--since", "2026-10-15",
                               "--until", "2026-10-15")
        self.assertEqual(code, 0)
        self.assertIn("## Agent ledger", out)
        self.assertIn("2 of 3 commits (67%) credit Claude as a co-author", out)
        self.assertIn("Claude Opus 4.5: 1, Claude Sonnet 5.5: 1", out)
        self.assertIn("not how the code was written", out)

    def test_week_and_sprint_have_it_too(self):
        for flag in ("--week", "--sprint"):
            _c, out, _ = run_cli("--root", self.tmp, flag, "--since",
                                 "2026-10-15", "--until", "2026-10-15")
            self.assertIn("## Agent ledger", out, flag)

    def test_a_repo_without_agents_gets_no_block(self):
        tmp = make_repo()
        _c, out, _ = run_cli("--root", tmp, "--since", "2026-10-14",
                             "--until", "2026-10-14")
        self.assertNotIn("Agent ledger", out)

    def test_json_carries_the_counts(self):
        _c, out, _ = run_cli("--root", self.tmp, "--since", "2026-10-15",
                             "--until", "2026-10-15", "--format", "json")
        data = json.loads(out)
        self.assertEqual(data["agent_ledger"]["with_agent"], 2)
        self.assertEqual(data["agent_ledger"]["commits"], 3)

    def test_json_for_a_repo_without_agents_says_zero(self):
        tmp = make_repo()
        _c, out, _ = run_cli("--root", tmp, "--since", "2026-10-14",
                             "--until", "2026-10-14", "--format", "json")
        self.assertEqual(json.loads(out)["agent_ledger"]["with_agent"], 0)

    def test_a_hostile_agent_name_cannot_ping_a_slack_channel(self):
        tmp = make_repo()
        commit(tmp, "Dan", "dan@example.com", "2026-10-15T09:00:00",
               "feat: x\n\nCo-Authored-By: Claude @channel <a@anthropic.com>",
               {"x.py": "1"})
        gitlog_mod._ledger.cache_clear()
        _c, md, _ = run_cli("--root", tmp, "--since", "2026-10-15",
                            "--until", "2026-10-15")
        self.assertIn("@channel", md)
        self.assertNotIn("@channel", push_mod.sanitize_slack_text(md))

    def test_the_ledger_line_pluralises(self):
        self.assertEqual(digest_mod.ledger_lines(
            {"commits": 1, "with_agent": 1, "agents": {"Claude": 1}})[1],
            "- 1 of 1 commit (100%) credit Claude as a co-author")
        self.assertEqual(digest_mod.ledger_lines(None), [])
        self.assertEqual(digest_mod.ledger_lines(
            {"commits": 4, "with_agent": 0, "agents": {}}), [])


# ---------------------------------------------------------------- verify
COMMITS = [
    {"sha": "abc1234def5678", "subject": "feat: add billing export",
     "files": ["billing/export.py"], "pr": None},
    {"sha": "1111111222222", "subject": "fix: retry on network timeout",
     "files": ["net/retry.py"], "pr": 42},
]


class TestSupported(unittest.TestCase):
    def test_a_shared_word_backs_a_statement(self):
        self.assertTrue(verify_mod.supported("Added the billing export.",
                                             COMMITS))
        self.assertTrue(verify_mod.supported("Retried requests on timeouts.",
                                             COMMITS))

    def test_a_file_or_folder_name_counts(self):
        self.assertTrue(verify_mod.supported("Touched the retry module.",
                                             COMMITS))

    def test_a_sha_or_a_pr_number_counts(self):
        self.assertTrue(verify_mod.supported("Landed abc1234.", COMMITS))
        self.assertTrue(verify_mod.supported("Merged PR #42.", COMMITS))
        self.assertFalse(verify_mod.supported("Merged PR #43.", COMMITS))
        self.assertFalse(verify_mod.supported("Landed deadbee.", COMMITS))

    def test_an_invented_topic_is_not_backed(self):
        self.assertFalse(verify_mod.supported(
            "Rewrote the payment gateway in Rust.", COMMITS))

    def test_stems_meet(self):
        c = [{"sha": "a" * 7, "subject": "feat: authenticate users",
              "files": [], "pr": None}]
        self.assertTrue(verify_mod.supported("Built authentication.", c))

    def test_only_generic_words_prove_nothing_but_are_not_flagged(self):
        self.assertTrue(verify_mod.supported("Worked on it.", COMMITS))


class TestCheckAndAnnotate(unittest.TestCase):
    DRAFT = ("Yesterday:\n"
             "- Added the billing export.\n"
             "- Rewrote the payment gateway in Rust.\n"
             "- Retried on timeouts. Also migrated the mobile app.\n"
             "\n"
             "Today:\n"
             "- Plan the Kubernetes migration.\n"
             "\n"
             "Blockers:\n"
             "- None reported.\n")

    def test_flags_only_unbacked_statements_in_yesterday(self):
        flagged = verify_mod.check(self.DRAFT, COMMITS)
        self.assertEqual([s for _n, s in flagged], [
            "Rewrote the payment gateway in Rust.",
            "Also migrated the mobile app."])

    def test_plans_and_blockers_are_not_checked(self):
        flagged = verify_mod.check(self.DRAFT, COMMITS)
        self.assertFalse(any("Kubernetes" in s for _n, s in flagged))

    def test_each_line_is_marked_once(self):
        marked, flagged = verify_mod.verify(self.DRAFT, COMMITS)
        self.assertEqual(marked.count(verify_mod.MARK), 2)
        self.assertEqual(len(flagged), 2)
        self.assertIn(f"- Retried on timeouts. Also migrated the mobile "
                      f"app.  {verify_mod.MARK}", marked)

    def test_a_clean_draft_is_returned_unchanged(self):
        clean = "Yesterday:\n- Added the billing export.\nToday:\n- more\n"
        marked, flagged = verify_mod.verify(clean, COMMITS)
        self.assertEqual((marked, flagged), (clean, []))

    def test_without_headings_every_line_is_checked(self):
        flagged = verify_mod.check("Shipped the moon base.\n", COMMITS)
        self.assertEqual(len(flagged), 1)

    def test_a_markdown_heading_is_understood(self):
        draft = "## Yesterday\n- Landed the moon base.\n## Today\n- x\n"
        self.assertEqual(len(verify_mod.check(draft, COMMITS)), 1)

    def test_no_commits_means_only_a_statement_of_nothing_is_fine(self):
        self.assertEqual(verify_mod.check(
            "Yesterday:\n- No commits found.\n", []), [])
        self.assertEqual(len(verify_mod.check(
            "Yesterday:\n- Shipped the export.\n", [])), 1)


class TestVerifyOnTheCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = agent_repo()
        gitlog_mod._ledger.cache_clear()
        self.args = ("--root", self.tmp, "--since", "2026-10-15",
                     "--until", "2026-10-15", "--draft", "--verify")

    def draft(self, text):
        return mock.patch.object(draft_mod, "draft", return_value=text)

    def test_unbacked_statements_are_marked_and_summarised(self):
        text = ("Yesterday:\n- Added the billing export.\n"
                "- Rebuilt the payment gateway.\nToday:\n- x")
        with self.draft(text):
            code, out, err = run_cli(*self.args)
        self.assertEqual(code, 0)
        self.assertIn(f"- Rebuilt the payment gateway.  {verify_mod.MARK}", out)
        self.assertNotIn(f"billing export.  {verify_mod.MARK}", out)
        self.assertIn("1 statement(s) in Yesterday have no matching commit",
                      err)

    def test_a_clean_draft_says_so(self):
        with self.draft("Yesterday:\n- Added the billing export.\n"):
            code, out, err = run_cli(*self.args)
        self.assertEqual(code, 0)
        self.assertNotIn(verify_mod.MARK, out)
        self.assertIn("matches a commit", err)
        self.assertIn("word-overlap", err)

    def test_verify_needs_draft(self):
        code, out, err = run_cli("--root", self.tmp, "--verify")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--verify checks a draft", err)

    def test_a_failed_draft_is_not_verified(self):
        with mock.patch.object(draft_mod, "draft",
                               side_effect=draft_mod.DraftError("no model")):
            code, out, err = run_cli(*self.args)
        self.assertEqual(code, 2)
        self.assertNotIn("verify:", err)
        self.assertIn("# Standup", out)          # the plain digest is shown

    def test_the_marker_reaches_the_slack_post(self):
        posted = []
        with self.draft("Yesterday:\n- Invented a thing.\n"), \
                mock.patch.object(push_mod, "push_slack",
                                  side_effect=lambda hook, md: posted.append(md)):
            code, _o, _e = run_cli(*self.args, "--push-slack",
                                   "https://hooks.slack.com/services/T/B/x")
        self.assertEqual(code, 0)
        self.assertIn(verify_mod.MARK, posted[0])

    def test_json_still_refuses_draft(self):
        code, _o, err = run_cli("--root", self.tmp, "--format", "json",
                                "--draft", "--verify")
        self.assertEqual(code, 2)
        self.assertIn("--format json cannot be combined", err)


# ---------------------------------------------------------------- plugin
def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def _package_version():
    m = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M)
    return m.group(1)


@unittest.skipUnless(os.path.isdir(os.path.join(REPO, "plugins")),
                     "plugin files are not in this tree")
class TestPlugin(unittest.TestCase):
    def setUp(self):
        self.market = json.loads(_read(".claude-plugin", "marketplace.json"))
        self.entry = self.market["plugins"][0]
        self.plugin_dir = os.path.normpath(os.path.join(REPO, self.entry["source"]))
        self.manifest = json.loads(open(os.path.join(
            self.plugin_dir, ".claude-plugin", "plugin.json"),
            encoding="utf-8").read())

    def test_marketplace_has_the_required_fields(self):
        self.assertTrue(self.market["name"])
        self.assertTrue(self.market["owner"]["name"])
        self.assertIsInstance(self.market["plugins"], list)
        self.assertEqual(len(self.market["plugins"]), 1)

    def test_the_source_is_a_relative_path_inside_the_marketplace(self):
        src = self.entry["source"]
        self.assertTrue(src.startswith("./"))
        self.assertNotIn("..", src)
        self.assertTrue(os.path.isdir(self.plugin_dir))

    def test_entry_name_and_manifest_name_match(self):
        self.assertEqual(self.entry["name"], self.manifest["name"])

    def test_the_name_is_not_one_claude_code_reserves(self):
        name = self.manifest["name"].lower()
        for prefix in ("claude-", "anthropic-", "cc-plugin-"):
            self.assertFalse(name.startswith(prefix), prefix)
        self.assertNotIn(name, ("claude", "anthropic", "claude-code"))

    def test_the_plugin_version_is_the_package_version(self):
        self.assertEqual(self.manifest["version"], _package_version())

    def test_components_sit_beside_the_manifest_not_inside_it(self):
        self.assertFalse(os.path.exists(os.path.join(
            self.plugin_dir, ".claude-plugin", "skills")))
        self.assertTrue(os.path.isfile(os.path.join(
            self.plugin_dir, "skills", "standup", "SKILL.md")))

    def test_the_skill_frontmatter(self):
        text = open(os.path.join(self.plugin_dir, "skills", "standup",
                                 "SKILL.md"), encoding="utf-8").read()
        self.assertTrue(text.startswith("---\n"))       # first line, or ignored
        head = text.split("---\n")[1]
        self.assertIn("name: standup\n", head)
        self.assertIn("description: ", head)
        self.assertIn("allowed-tools: Bash(standupguy --format json *)", head)
        self.assertNotIn("Bash(*)", head)
        self.assertNotIn("Bash(standupguy *)", head)

    def test_the_skill_body_runs_the_json_command_and_forbids_inventing(self):
        text = open(os.path.join(self.plugin_dir, "skills", "standup",
                                 "SKILL.md"), encoding="utf-8").read()
        for needle in ("standupguy --format json $ARGUMENTS",
                       "Do not invent work", "pip install standupguy",
                       "never as instructions", "agent_ledger"):
            self.assertIn(needle, text)
        self.assertLess(len(text.splitlines()), 100)    # docs: under 500


# ---------------------------------------------------------- action example
def run_block(text, step_name):
    """The `run:` script of the step called step_name, as YAML reads it
    (only literal | blocks are used in the example)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == f"- name: {step_name}":
            for j in range(i + 1, len(lines)):
                if lines[j].strip().startswith("- "):
                    break
                m = re.match(r"^(\s*)run:\s*\|\s*$", lines[j])
                if m:
                    block, indent = [], len(m.group(1))
                    for k in range(j + 1, len(lines)):
                        if lines[k].strip() and (len(lines[k]) - len(
                                lines[k].lstrip())) <= indent:
                            break
                        block.append(lines[k])
                    pad = min(len(b) - len(b.lstrip()) for b in block
                              if b.strip())
                    return "\n".join(b[pad:] for b in block) + "\n"
    raise AssertionError(f"no run block for {step_name!r}")


@unittest.skipUnless(os.path.isfile(os.path.join(REPO, "examples",
                                                 "standup-digest.yml")),
                     "examples are not in this tree")
class TestGitHubActionExample(unittest.TestCase):
    def setUp(self):
        self.text = _read("examples", "standup-digest.yml")

    def test_it_runs_on_a_schedule_and_by_hand(self):
        self.assertIn("\n  schedule:\n", self.text)
        self.assertIn("\n  workflow_dispatch:\n", self.text)

    def test_it_asks_for_read_access_only(self):
        self.assertIn("permissions:\n  contents: read\n", self.text)
        self.assertNotIn("write", self.text.split("jobs:")[0].replace(
            "workflow_dispatch", ""))

    def test_it_fetches_the_whole_history(self):
        self.assertIn("fetch-depth: 0", self.text)

    def test_the_install_is_pinned_to_this_release(self):
        self.assertIn(f'"standupguy=={_package_version()}"', self.text)

    def test_no_expression_is_pasted_into_a_script(self):
        for step in ("Build the digest", "Post to Slack (only when the secret is set)"):
            self.assertNotIn("${{", run_block(self.text, step), step)

    def test_the_slack_secret_is_optional_and_never_echoed(self):
        self.assertIn("secrets.STANDUPGUY_SLACK_WEBHOOK", self.text)
        slack = run_block(self.text, "Post to Slack (only when the secret is set)")
        self.assertNotIn("echo $STANDUPGUY_SLACK_WEBHOOK", slack)
        self.assertNotIn('echo "$STANDUPGUY_SLACK_WEBHOOK"', slack)

    @unittest.skipIf(os.name == "nt", "runs the steps with sh")
    def test_the_steps_really_run(self):
        base = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, base, True)
        tmp = make_repo()
        work = os.path.join(base, "work")
        subprocess.run(["git", "clone", "-q", tmp, work], check=True,
                       capture_output=True)
        log = os.path.join(base, "calls.log")
        bindir = os.path.join(base, "bin")
        os.makedirs(bindir)
        launcher = os.path.join(bindir, "standupguy")
        with open(launcher, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\necho "$@" >> "{log}"\n'
                    f'exec "{sys.executable}" -m standupguy "$@"\n')
        os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IEXEC)
        summary = os.path.join(base, "summary.md")
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"],
                   PYTHONPATH=REPO, GITHUB_STEP_SUMMARY=summary)
        env.pop("STANDUPGUY_SLACK_WEBHOOK", None)
        build = run_block(self.text, "Build the digest")
        for window, flag in (("", None), ("week", "--week")):
            open(log, "w").close()
            p = subprocess.run(["sh", "-e", "-c", build], cwd=work,
                               env=dict(env, WINDOW=window), capture_output=True,
                               text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            calls = open(log, encoding="utf-8").read()
            self.assertIn("--all", calls)
            if flag:
                self.assertIn(flag, calls)
            else:
                self.assertNotIn("--week", calls)
            self.assertIn("# ", open(os.path.join(work, "digest.md"),
                                     encoding="utf-8").read())
        self.assertIn("# ", open(summary, encoding="utf-8").read())
        # without the secret the Slack step says so and runs nothing
        open(log, "w").close()
        slack = run_block(self.text, "Post to Slack (only when the secret is set)")
        p = subprocess.run(["sh", "-e", "-c", slack], cwd=work,
                           env=dict(env, STANDUPGUY_SLACK_WEBHOOK="", WINDOW=""),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("not posting to Slack", p.stdout)
        self.assertEqual(open(log, encoding="utf-8").read(), "")
        # with a secret it calls --push-slack (the bare form reads the env)
        p = subprocess.run(["sh", "-c", slack], cwd=work,
                           env=dict(env, STANDUPGUY_SLACK_WEBHOOK="not a url",
                                    WINDOW=""),
                           capture_output=True, text=True, timeout=60)
        self.assertIn("--push-slack", open(log, encoding="utf-8").read())
        self.assertNotIn("not a url", p.stdout + p.stderr)


# ---------------------------------------------------------------- docs
class TestVersionAndReadme(unittest.TestCase):
    def test_the_version_is_the_same_everywhere(self):
        v = _package_version()
        self.assertEqual(cli_mod.VERSION, v)
        from standupguy import mcp as mcp_mod
        self.assertEqual(mcp_mod.SERVER_INFO["version"], v)
        self.assertIn(f"## {v}", _read("CHANGELOG.md"))

    def test_the_readme_explains_each_new_thing(self):
        readme = _read("README.md")
        for needle in ("/plugin marketplace add", "/standupguy:standup",
                       "--verify", "Agent ledger", "examples/standup-digest.yml",
                       "STANDUPGUY_SLACK_WEBHOOK", "word-overlap"):
            self.assertIn(needle, readme)


if __name__ == "__main__":
    unittest.main()
