"""Expert round 2 (0.3.0).

R  refs: ticket keys, issue numbers, squash/merge pull requests
G  grouping by verb, branch name and files; folding of fixup/WIP/sync noise
X  redaction profiles and Markdown/mrkdwn escaping
F  other branches, stashes, worktrees and reflog-only commits
A  agent-assisted commits and (opt-in) transcript sessions
O  output formats, the Yesterday/Today/Blockers style, the clipboard
B  the committed benchmark still says what the README says
"""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_round5 import run_cli
from test_standupguy import commit, git, make_repo

from standupguy import agents as agents_mod
from standupguy import digest as digest_mod
from standupguy import formats as formats_mod
from standupguy import gitlog as gitlog_mod
from standupguy import inflight as inflight_mod
from standupguy import redact as redact_mod
from standupguy import refs as refs_mod
from standupguy import themes as themes_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = os.name == "posix"


class FakeCommit:
    def __init__(self, subject, files=(), date="2026-10-14", body="",
                 coauthors=(), author="Dan", pr=None):
        self.subject, self.files, self.date = subject, list(files), date
        self.body, self.coauthors, self.author = body, list(coauthors), author
        self.email, self.pr_number, self.sha = "d@x.io", pr, "ab" * 20


# ------------------------------------------------------------------ R

class TestRefs(unittest.TestCase):
    def test_tickets_issues_prs(self):
        r = refs_mod.extract("feat(auth): ABC-123 add SSO (#412)")
        self.assertEqual((r.tickets, r.prs, r.issues),
                         (("ABC-123",), (412,), ()))
        r = refs_mod.extract("Fix flaky test, closes #77")
        self.assertEqual((r.issues, r.closes), ((77,), (77,)))
        r = refs_mod.extract("Merge pull request #55 from dan/ENG-9-login")
        self.assertEqual((r.prs, r.tickets), ((55,), ("ENG-9",)))
        self.assertEqual(refs_mod.extract("see GH-12").issues, (12,))
        r = refs_mod.extract("Merged in feature/x (pull request #7)")
        self.assertEqual(r.prs, (7,))

    def test_body_counts(self):
        r = refs_mod.extract("tidy", "Fixes #5 and resolves #6\n\nPLAT-3")
        self.assertEqual((r.issues, r.closes, r.tickets),
                         ((5, 6), (5, 6), ("PLAT-3",)))

    def test_gitlab_merge_request(self):
        r = refs_mod.extract("Merge branch 'x' into 'main'",
                             "See merge request group/proj!9")
        self.assertEqual(r.prs, (9,))

    def test_look_alikes_are_not_keys(self):
        for text in ("Support UTF-8 names", "Switch to SHA-256",
                     "Patch CVE-2024-4417", "Add HTTP-2", "Bump GPT-4",
                     "use utf-8 and proj-10", "color #fff", "x A-1",
                     "ISO-8601 dates", "v1-2 compat", "a&#39;b",
                     "path/to/#12thing"):
            self.assertFalse(refs_mod.extract(text), text)

    def test_tags_and_caps(self):
        r = refs_mod.extract(" ".join(f"AB-{i}" for i in range(1, 40)))
        self.assertEqual(len(r.tickets), refs_mod.MAX_REFS)
        self.assertEqual(refs_mod.tags(refs_mod.extract("ABC-1 x #3 (#2)")),
                         "[ABC-1] [#3] [PR #2]")


# ------------------------------------------------------------------ G

class TestGrouping(unittest.TestCase):
    def test_verbs(self):
        for subject, group in [
                ("Add login page", "feat"), ("Fix crash on logout", "fix"),
                ("Refactor billing", "refactor"), ("Update README", "docs"),
                ("Bump lodash to 4.17.21", "chore"),
                ("Add tests for parser", "test"),
                ("Fix typo in CONTRIBUTING", "docs"),
                ("Fix flaky test", "test"), ("Patch XSS in comments", "fix"),
                ("Update dependencies", "chore"), ("Update", "other"),
                ("Initial commit", "other"), ("Revert \"Add x\"", "chore"),
                ("feat!: drop v1", "feat"), ("ABC-1: fix bug", "fix"),
                ("[ABC-1] Add export", "feat"), (":bug: leak", "fix")]:
            self.assertEqual(themes_mod.classify_smart(subject), group,
                             subject)

    def test_merge_branch_names_the_group(self):
        c = themes_mod.classify_smart
        self.assertEqual(c("Merge pull request #1 from a/feature/x"), "feat")
        self.assertEqual(c("Merge pull request #1 from a/bugfix/x"), "fix")
        self.assertEqual(c("Merge pull request #1 from a/hotfix/x"), "fix")
        self.assertEqual(c("Merge pull request #1 from a/dependabot/npm/x"),
                         "chore")
        self.assertEqual(c("Merge pull request #1 from a/new-nav"), "other")
        self.assertEqual(c("Merge branch 'main' of github.com:a/b"), "other")

    def test_files_decide_when_words_do_not(self):
        c = themes_mod.classify_smart
        self.assertEqual(c("misc", ["docs/a.md", "README.md"]), "docs")
        self.assertEqual(c("misc", ["tests/test_a.py"]), "test")
        self.assertEqual(c("misc", ["package-lock.json"]), "chore")
        self.assertEqual(c("misc", ["src/a.py", "README.md"]), "other")

    def test_clean_subject(self):
        cs = themes_mod.clean_subject
        self.assertEqual(cs("feat(auth): ABC-123 add SSO (#412)"), "add SSO")
        self.assertEqual(cs("[ABC-1] Add export"), "Add export")
        self.assertEqual(cs("ABC-1: Add export"), "Add export")
        self.assertEqual(cs("Merge pull request #5 from dan/feature/add-login"),
                         "merged add login")
        self.assertEqual(cs("(#12)"), "(#12)")      # never an empty line


class TestFold(unittest.TestCase):
    def fold(self, *subjects, **kw):
        return themes_mod.fold([FakeCommit(s) for s in subjects], **kw)

    def texts(self, folded):
        return [i.text for i in folded.items]

    def test_noise_is_folded_and_counted(self):
        f = self.fold("Add login", "wip", "tmp", "fixup! Add login",
                      "Merge branch 'main' into dev", "Fix bug")
        self.assertEqual(self.texts(f), ["Add login", "Fix bug"])
        self.assertEqual((f.junk, f.fixups, f.syncs), (2, 1, 1))
        self.assertIn("3 fixup/WIP/save commits", f.note())
        self.assertIn("1 branch sync", f.note())

    def test_real_work_that_looks_like_noise_stays(self):
        keep = ["wip: login form", "Fix typo in README", "Add save button",
                "Merge pull request #5 from a/feature/x", "Update billing",
                "ABC-1 wip", "Lint the YAML files in CI"]
        self.assertEqual(len(self.fold(*keep).items), len(keep))

    def test_fixup_without_target_is_the_news(self):
        f = self.fold("fixup! Add login", "squash! Add login")
        self.assertEqual(self.texts(f), ["Add login"])

    def test_revert_pair_cancels(self):
        f = self.fold("Add dark mode", 'Revert "Add dark mode"', "Fix x")
        self.assertEqual(self.texts(f), ["Fix x"])
        self.assertEqual(f.reverted, ["Add dark mode"])
        f = self.fold('Revert "Add dark mode"')
        self.assertEqual(len(f.items), 1)           # nothing to cancel

    def test_duplicates_on_one_day(self):
        f = self.fold("Add login", "Add login")
        self.assertEqual([(i.text, i.count) for i in f.items],
                         [("Add login", 2)])

    def test_no_fold_keeps_everything(self):
        self.assertEqual(len(self.fold("wip", "x", do_fold=False).items), 2)

    def test_many_commits_are_fast(self):
        subjects = [f"fixup! Add thing {i % 50}" for i in range(20000)] + \
                   [f"Add thing {i}" for i in range(50)]
        t0 = time.time()
        self.fold(*subjects)
        self.assertLess(time.time() - t0, 5)


# ------------------------------------------------------------------ X

class TestRedaction(unittest.TestCase):
    def secrets(self):
        # built at run time: no secret-shaped literal in this file
        return {
            "aws": "AK" + "IA" + "ABCDEFGH12345678",
            "gh": "gh" + "p_" + "a1B2c3D4e5" * 4,
            "stripe": "sk_" + "live_" + "q1w2e3r4t5y6u7i8o9p0a1s2",
            "slack": "xo" + "xb-" + "1234567890-1234567890-abcdefghijklmnop",
            "openai": "sk" + "-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1",
            "jwt": "ey" + "Jabcdefghij.ey" + "Jklmnopqrst.sig1234567890",
        }

    def test_secrets_go(self):
        r = redact_mod.make()
        for kind, secret in self.secrets().items():
            out = r.text(f"fix: use {secret} in tests")
            self.assertNotIn(secret, out, kind)
            self.assertIn("{secret}", out)

    def test_assignments_and_private_key(self):
        r = redact_mod.make()
        self.assertEqual(r.text("set password=hunter22x here"),
                         "set password={secret} here")
        self.assertEqual(r.text("token refresh logic"), "token refresh logic")
        self.assertEqual(r.text("auth: login flow"), "auth: login flow")
        pem = "-----BEGIN RSA " + "PRIVATE KEY-----\nabc\n-----END RSA " \
              "PRIVATE KEY-----"
        self.assertEqual(r.text("x " + pem + " y"), "x {secret} y")

    def test_urls(self):
        r = redact_mod.make()
        self.assertEqual(r.text("see https://github.com/o/r/pull/3?x=1#c"),
                         "see https://github.com/o/r/pull/3")
        self.assertEqual(r.text("https://u:pw@host.example.com/a"),
                         "https://host.example.com/a")
        for internal in ("http://wiki.corp/x", "https://intranet/a",
                         "http://localhost:8080/", "http://10.1.2.3/x",
                         "https://jira.internal.example.com/browse/A-1",
                         "https://app.staging.example.com/", "wiki.corp/page"):
            self.assertIn("{internal-url}", r.text(internal), internal)
        self.assertNotIn("wiki", r.text("see https://wiki.corp/p?id=1 now"))

    def test_emails_ips_and_shas(self):
        r = redact_mod.make()
        self.assertEqual(r.text("ask a.b@example.com"), "ask {email}")
        self.assertEqual(r.text("db at 192.168.1.5 and 8.8.8.8"),
                         "db at {ip} and 8.8.8.8")
        sha = "ab12" * 10
        self.assertEqual(r.text("revert " + sha), "revert " + sha)
        self.assertEqual(r.text("Add dark mode (#12) v1.2.3"),
                         "Add dark mode (#12) v1.2.3")

    def test_profiles_and_terms(self):
        self.assertEqual(redact_mod.make("off").text("a@b.com"), "a@b.com")
        s = redact_mod.make("strict")
        self.assertEqual(s.text("see https://github.com/o/r"), "see {link}")
        self.assertEqual(s.repo("acme-core"), "project-1")
        self.assertEqual(s.repo("acme-web"), "project-2")
        self.assertEqual(s.repo("acme-core"), "project-1")
        t = redact_mod.make("standard", ["Globex", "project  zed"])
        self.assertEqual(t.text("GLOBEX asked"), "{redacted} asked")
        self.assertEqual(redact_mod.make("off", ["x"]).text("x"), "x")
        with self.assertRaises(ValueError):
            redact_mod.make("loud")

    def test_hostile_long_input_stays_fast(self):
        r = redact_mod.make()
        for text in ("a" * 1_000_000, "a." * 100_000, "a@" * 100_000,
                     "http://" * 50_000, "-----BEGIN PRIVATE KEY-----" * 3000,
                     "token" + " " * 100_000):
            t0 = time.time()
            r.text(text)
            self.assertLess(time.time() - t0, 3, text[:12])

    def test_escape_only_what_acts(self):
        e = digest_mod.md_escape
        self.assertEqual(e("[click](http://x)"), "[click]\\(http://x)")
        self.assertEqual(e("<!here> and <@U1> <b>"),
                         "[at]here and \\<@U1> \\<b>".replace("\\<@U1>",
                                                              "\\<\\@U1>"))
        self.assertEqual(e("ping @alice and @channel"),
                         "ping \\@alice and [at]channel")
        self.assertEqual(e("a `b"), "a \\`b")
        self.assertEqual(e("a `b` c"), "a `b` c")
        self.assertEqual(e("x[0] < y > z *bold* snake_case"),
                         "x[0] < y > z *bold* snake_case")


class TestRedactionInTheDigest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.secret = "sk_" + "live_" + "q1w2e3r4t5y6u7i8o9p0a1s2"
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T09:00:00",
               f"fix: use {self.secret} see https://wiki.corp/x?t=1 "
               "[go](http://evil.example) @here", {"a.txt": "1"})

    def test_default_redacts_and_escapes(self):
        code, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01")
        self.assertEqual(code, 0)
        self.assertNotIn(self.secret, out)
        self.assertNotIn("wiki.corp", out)
        self.assertIn("{secret}", out)
        self.assertIn("]\\(", out)                  # link neutralised
        self.assertIn("[at]here", out)

    def test_off_and_term_and_strict(self):
        code, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                               "--redact", "off")
        self.assertIn(self.secret, out)
        code, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                               "--redact-term", "login")
        self.assertNotIn("login", out.split("\n", 3)[3].lower())
        code, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                               "--redact", "strict")
        self.assertIn("# Standup — project-1", out)
        self.assertNotIn(os.path.basename(self.repo), out)

    def test_environment_default(self):
        with mock.patch.dict(os.environ, {"STANDUPGUY_REDACT": "off"}):
            _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01")
        self.assertIn(self.secret, out)

    def test_json_is_exact_unless_asked(self):
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--format", "json")
        self.assertIn(self.secret, out)
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--format", "json", "--redact", "standard")
        self.assertNotIn(self.secret, out)
        self.assertIn("{secret}", out)

    def test_draft_prompt_is_redacted(self):
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--draft", "--dry-run")
        self.assertNotIn(self.secret, out)

    def test_slack_text_is_redacted(self):
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--push-slack", "https://hooks.example/x",
                            "--dry-run")
        self.assertNotIn(self.secret, out)

    def test_bad_environment_profile_is_an_error(self):
        with mock.patch.dict(os.environ, {"STANDUPGUY_REDACT": "strcit"}):
            code, out, err = run_cli("--root", self.repo)
        self.assertEqual(code, 2)
        self.assertIn("STANDUPGUY_REDACT", err)
        self.assertEqual(out, "")

    def test_copy_gets_escapes_not_control_characters(self):
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T10:00:00",
               "fix: bad \x1b[2J screen", {"esc.txt": "1"})
        seen = []
        with mock.patch.object(formats_mod, "copy",
                               side_effect=lambda t: seen.append(t) or "t"):
            run_cli("--root", self.repo, "--since", "2026-10-01", "--copy")
        self.assertNotIn("\x1b", seen[0])
        self.assertIn("\\x1b", seen[0])

    def test_bad_profile_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            run_cli("--root", self.repo, "--redact", "loud")


# ------------------------------------------------------------------ F

class TestInFlight(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.env = {"GIT_AUTHOR_NAME": "Dan", "GIT_AUTHOR_EMAIL":
                    "dan@example.com", "GIT_COMMITTER_NAME": "Dan",
                    "GIT_COMMITTER_EMAIL": "dan@example.com",
                    "GIT_AUTHOR_DATE": "2026-10-15T10:00:00",
                    "GIT_COMMITTER_DATE": "2026-10-15T10:00:00"}

    def g(self, *a):
        git(self.repo, *a, env=self.env)

    def test_other_branch_stash_worktree_and_lost_commit(self):
        self.g("checkout", "-qb", "feature/search")
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T10:00:00",
               "feat: ABC-5 search filters", {"s.py": "1"})
        self.g("checkout", "-q", "main")
        self.g("checkout", "-qb", "other")
        commit(self.repo, "Teammate", "t@example.com", "2026-10-15T10:00:00",
               "feat: not mine", {"t.py": "1"})
        self.g("checkout", "-q", "main")
        with open(os.path.join(self.repo, "st.txt"), "w") as fh:
            fh.write("x")
        self.g("add", "st.txt")
        self.g("stash")
        wt = self.repo + "-wt"
        self.g("worktree", "add", "-q", "-b", "wt-branch", wt)
        with open(os.path.join(wt, "dirty.py"), "w") as fh:
            fh.write("x")
        self.g("checkout", "-qb", "scratch")
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T11:00:00",
               "feat: lost work", {"l.py": "1"})
        self.g("checkout", "-q", "main")
        self.g("branch", "-D", "scratch")
        found = inflight_mod.find(self.repo, "2026-10-01", None,
                                  "dan@example.com")
        kinds = {(i.kind, i.name) for i in found}
        self.assertIn(("branch", "feature/search"), kinds)
        self.assertNotIn(("branch", "other"), kinds)      # a teammate's
        self.assertTrue(any(i.kind == "stash" for i in found))
        self.assertIn(("worktree", os.path.basename(wt)), kinds)
        lost = [i for i in found if i.kind == "lost"]
        self.assertEqual([i.subject for i in lost], ["feat: lost work"])
        branch = next(i for i in found if i.kind == "branch")
        self.assertIn("local only", branch.detail)
        md = digest_mod.digest_markdown(self.repo, "2026-10-01", "x",
                                        "dan@example.com")
        self.assertIn("## Also in flight", md)
        self.assertIn("branch `feature/search`", md)
        self.assertIn("reflog only", md)
        self.assertIn(os.path.basename(wt), md)
        self.assertNotIn(self.repo, md)             # names, never paths

    def test_pushed_branch_is_said_to_be_pushed(self):
        bare = tempfile.mkdtemp()
        git(bare, "init", "-q", "--bare")
        self.g("remote", "add", "origin", bare)
        self.g("checkout", "-qb", "feature/a")
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T10:00:00",
               "feat: a", {"a2.py": "1"})
        self.g("push", "-q", "-u", "origin", "feature/a")
        self.g("checkout", "-q", "main")
        (b,) = [i for i in inflight_mod.find(self.repo, "2026-10-01", None,
                                             "dan@example.com")
                if i.kind == "branch"]
        self.assertIn("pushed", b.detail)
        self.assertNotIn("local only", b.detail)

    def test_off_and_empty_repo(self):
        self.g("checkout", "-qb", "b")
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T10:00:00",
               "feat: x", {"x2.py": "1"})
        self.g("checkout", "-q", "main")
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--no-inflight")
        self.assertNotIn("in flight", out)
        empty = tempfile.mkdtemp()
        git(empty, "init", "-qb", "main")
        self.assertEqual(inflight_mod.find(empty, "2026-10-01", None, None),
                         [])

    def test_json_has_in_flight(self):
        self.g("checkout", "-qb", "b2")
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T10:00:00",
               "feat: x", {"x3.py": "1"})
        self.g("checkout", "-q", "main")
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--format", "json")
        names = [i["name"] for i in json.loads(out)["in_flight"]]
        self.assertIn("b2", names)


# ------------------------------------------------------------------ A

class TestAgents(unittest.TestCase):
    def test_flags(self):
        a = agents_mod.agent_of
        self.assertEqual(a(FakeCommit("x", coauthors=[
            "Claude <noreply@anthropic.com>"])), "Claude")
        self.assertEqual(a(FakeCommit("x", coauthors=[
            "Copilot <copilot@github.com>"])), "Copilot")
        self.assertEqual(a(FakeCommit("x", author="Dan (aider)")), "Aider")
        self.assertEqual(a(FakeCommit("x", body="Generated with Claude Code")),
                         "Claude")
        self.assertIsNone(a(FakeCommit("x", coauthors=["Bob <bob@x.io>"])))
        self.assertIsNone(a(FakeCommit("Add claude support")))

    def test_summary(self):
        cs = [FakeCommit("a", coauthors=["Claude <n@anthropic.com>"]),
              FakeCommit("b"), FakeCommit("c", author="x (aider)")]
        self.assertEqual(agents_mod.summary(cs),
                         "_Agent-assisted: 2 of 3 commits (Aider 1, Claude 1)_")
        self.assertEqual(agents_mod.summary([FakeCommit("a")]), "")

    def test_in_the_digest_and_json(self):
        repo = make_repo()
        commit(repo, "Dan", "dan@example.com", "2026-10-15T09:00:00",
               "feat: add widget\n\nCo-authored-by: Claude <noreply@anthropic.com>",
               {"c.txt": "1"})
        _, out, _ = run_cli("--root", repo, "--since", "2026-10-01")
        self.assertIn("_(with Claude)_", out)
        self.assertIn("Agent-assisted: 1 of", out)
        _, out, _ = run_cli("--root", repo, "--since", "2026-10-01",
                            "--format", "json")
        self.assertIn("Claude", [c["agent"] for c in json.loads(out)["commits"]])

    def write_transcripts(self, base, cwd):
        proj = os.path.join(base, "projects", "p1")
        os.makedirs(proj)
        today = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lines = [
            "not json", "[1,2]", json.dumps({"type": "summary"}),
            json.dumps({"type": "user", "cwd": cwd, "timestamp": today,
                        "message": {"role": "user",
                                    "content": "fix the login redirect "
                                               "bug please"}}),
            json.dumps({"type": "user", "cwd": cwd, "timestamp": today,
                        "message": {"role": "user", "content": [
                            {"type": "text", "text": "and add a test"}]}}),
            json.dumps({"type": "user", "cwd": cwd, "timestamp": today,
                        "isSidechain": True,
                        "message": {"role": "user", "content": "sub"}}),
            json.dumps({"type": "user", "cwd": cwd, "timestamp": today,
                        "message": {"role": "user", "content": [
                            {"type": "tool_result", "content": "x"}]}}),
        ]
        with open(os.path.join(proj, "s1.jsonl"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        other = os.path.join(proj, "s2.jsonl")
        with open(other, "w") as fh:
            fh.write(json.dumps({"type": "user", "cwd": "/elsewhere",
                                 "timestamp": today, "message": {
                                     "role": "user", "content": "hi"}}) + "\n")
        return os.path.join(base, "projects")

    def test_sessions_are_counted_for_this_repo_only(self):
        base, repo = tempfile.mkdtemp(), make_repo()
        pdir = self.write_transcripts(base, os.path.realpath(repo))
        import datetime
        found = agents_mod.sessions(repo, datetime.date.today() -
                                    datetime.timedelta(days=1),
                                    projects_dir=pdir)
        self.assertEqual(found, [{"prompts": 2,
                                  "first": "fix the login redirect bug please"}])
        self.assertEqual(agents_mod.sessions(
            repo, datetime.date.today() + datetime.timedelta(days=2),
            projects_dir=pdir), [])
        self.assertEqual(agents_mod.sessions(repo, datetime.date.today(),
                                             projects_dir="/no/such"), [])

    def test_off_unless_asked(self):
        base, repo = tempfile.mkdtemp(), make_repo()
        self.write_transcripts(base, os.path.realpath(repo))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": base}):
            with mock.patch.object(agents_mod, "sessions") as spy:
                run_cli("--root", repo, "--since", "2026-10-01")
                spy.assert_not_called()
            _, out, _ = run_cli("--root", repo, "--since", "yesterday",
                                "--agent-sessions")
        self.assertIn("## Agent sessions", out)
        self.assertIn("1 Claude Code session, 2 requests", out)
        self.assertIn("fix the login redirect bug please", out)


# ------------------------------------------------------------------ O

class TestFormats(unittest.TestCase):
    MD = ("# Standup — r\n_since x_\n\n## Shipped\n### Fixes\n"
          "- ping [at]here `abc` **bold** \\<\\@u>\n\n## Blockers\n_none_\n")

    def test_plain(self):
        out = formats_mod.convert(self.MD, "plain")
        self.assertIn("Standup — r\n", out)
        self.assertIn("SHIPPED", out)
        self.assertIn("  Fixes:", out)
        self.assertIn("- ping [at]here abc bold <@u>", out)
        self.assertNotIn("`", out)

    def test_slack(self):
        out = formats_mod.convert(self.MD, "slack")
        self.assertIn("*Shipped*", out)
        self.assertIn("_Fixes_", out)
        self.assertIn("• ping", out)
        self.assertIn("*bold*", out)
        self.assertIn("&lt;@u&gt;", out)            # escaped for Slack

    def test_teams_and_markdown(self):
        out = formats_mod.convert(self.MD, "teams")
        self.assertIn("**Shipped**", out)
        self.assertIn("- ping", out)
        self.assertEqual(formats_mod.convert(self.MD, "markdown"), self.MD)
        with self.assertRaises(ValueError):
            formats_mod.convert(self.MD, "rtf")

    def test_mass_mentions_are_neutral_in_every_format(self):
        for fmt in ("plain", "slack", "teams"):
            out = formats_mod.convert("- @channel <!here> @everyone", fmt)
            self.assertNotRegex(out, r"@channel|<!here>|@everyone", fmt)


class TestClipboard(unittest.TestCase):
    def which_in(self, *names):
        return lambda n: f"/bin/{n}" if n in names else None

    def test_selection(self):
        cc = formats_mod.clipboard_command
        self.assertEqual(cc({}, "darwin", self.which_in("pbcopy"))[0],
                         ["pbcopy"])
        self.assertEqual(cc({}, "win32", self.which_in("clip")),
                         (["clip"], "utf-16"))
        self.assertEqual(cc({"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}, "linux",
                            self.which_in("wl-copy", "xclip"))[0], ["wl-copy"])
        self.assertEqual(cc({"DISPLAY": ":0"}, "linux",
                            self.which_in("xsel"))[0][0], "xsel")
        self.assertEqual(cc({}, "linux", self.which_in("clip.exe")),
                         (["clip.exe"], "utf-16"))
        self.assertIsNone(cc({}, "linux", self.which_in()))
        self.assertIsNone(cc({}, "darwin", self.which_in()))

    @unittest.skipUnless(POSIX, "needs a shell script")
    def test_copy_runs_the_tool_with_the_text_on_stdin(self):
        d = tempfile.mkdtemp()
        sink = os.path.join(d, "got.txt")
        tool = os.path.join(d, "xclip")
        with open(tool, "w") as fh:
            fh.write(f"#!/bin/sh\ncat > '{sink}'\n")
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC)
        env = {"PATH": d + os.pathsep + "/usr/bin:/bin", "DISPLAY": ":0"}
        with mock.patch.dict(os.environ, env, clear=True):
            repo = make_repo()
            code, out, err = run_cli("--root", repo, "--since", "2026-10-01",
                                     "--copy", "--format", "plain")
        self.assertEqual(code, 0)
        self.assertIn("copied to the clipboard (xclip)", err)
        with open(sink, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), out)

    def test_no_tool_is_a_warning_not_a_failure(self):
        repo = make_repo()
        with mock.patch.object(formats_mod, "clipboard_command",
                               return_value=None):
            code, out, err = run_cli("--root", repo, "--since", "2026-10-01",
                                     "--copy")
        self.assertEqual(code, 0)
        self.assertIn("warning: not copied: no clipboard tool found", err)
        self.assertIn("# Standup", out)

    def test_flag_conflicts(self):
        repo = make_repo()
        for argv in (["--format", "slack", "--push-slack", "http://x/y"],
                     ["--copy", "--format", "json"]):
            code, _, err = run_cli("--root", repo, *argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("error:", err)


class TestStyles(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T09:00:00",
               "ABC-1: add SSO login (#40)", {"sso.py": "1"})
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T09:30:00",
               "fix(auth): ABC-1 redirect loop", {"sso.py": "2"})
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T10:00:00",
               "docs: waiting on legal for the privacy page", {"p.md": "1"})

    def test_ytb(self):
        with open(os.path.join(self.repo, "draft.py"), "w") as fh:
            fh.write("1")
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--style", "ytb")
        self.assertIn("## Since 2026-10-01", out)
        self.assertIn("**ABC-1**: add SSO login; redirect loop", out)
        self.assertIn("[PR #40]", out)
        self.assertIn("## Today", out)
        self.assertIn("uncommitted: `draft.py`", out)
        self.assertIn("## Blockers\n- possibly: waiting on legal", out)

    def test_ytb_heading_names_the_day(self):
        import datetime
        with mock.patch.object(gitlog_mod, "_today",
                               return_value=datetime.date(2026, 11, 2)):
            _, out, _ = run_cli("--root", self.repo, "--style", "ytb")
        self.assertIn("## Friday", out)
        with mock.patch.object(gitlog_mod, "_today",
                               return_value=datetime.date(2026, 11, 3)):
            _, out, _ = run_cli("--root", self.repo, "--style", "ytb")
        self.assertIn("## Yesterday", out)

    def test_group_by_ticket(self):
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--group", "ticket")
        self.assertIn("### ABC-1\n- redirect loop [ABC-1]", out)
        self.assertIn("### No ticket", out)

    def test_weekly_and_sprint_use_the_same_lines(self):
        for flag in ("--week", "--sprint"):
            _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                                flag)
            self.assertIn("add SSO login [ABC-1] [PR #40]", out)

    def test_noise_note_and_no_fold(self):
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T11:00:00",
               "wip", {"w.txt": "1"})
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01")
        self.assertIn("_Folded away: 1 fixup/WIP/save commit._", out)
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--no-fold")
        self.assertIn("- wip (1 file)", out)
        self.assertNotIn("Folded away", out)

    def test_mailmap_joins_old_addresses(self):
        commit(self.repo, "Dan C", "old@elsewhere.org", "2026-10-15T12:00:00",
               "feat: from my old address", {"o.txt": "1"})
        with open(os.path.join(self.repo, ".mailmap"), "w") as fh:
            fh.write("Dan <dan@example.com> <old@elsewhere.org>\n")
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--author", "dan@example.com")
        self.assertIn("from my old address", out)

    def test_body_refs_reach_the_json(self):
        commit(self.repo, "Dan", "dan@example.com", "2026-10-15T13:00:00",
               "tidy up parser\n\nCloses #9\nPLAT-7", {"b.txt": "1"})
        _, out, _ = run_cli("--root", self.repo, "--since", "2026-10-01",
                            "--format", "json")
        c = [c for c in json.loads(out)["commits"]
             if c["subject"] == "tidy up parser"][0]
        self.assertEqual(c["refs"]["tickets"], ["PLAT-7"])
        self.assertEqual(c["refs"]["closes"], [9])
        self.assertNotIn("Closes", json.dumps(c))       # the body is not shown


# ------------------------------------------------------------------ B

class TestBenchmark(unittest.TestCase):
    def test_committed_results_are_what_the_code_gives(self):
        sys.path.insert(0, os.path.join(ROOT, "bench"))
        try:
            import bench
            got = json.loads(json.dumps(bench.run(), sort_keys=True))
        finally:
            sys.path.pop(0)
        with open(os.path.join(ROOT, "bench", "results.json")) as fh:
            self.assertEqual(got, json.load(fh))

    def test_floors(self):
        with open(os.path.join(ROOT, "bench", "results.json")) as fh:
            res = json.load(fh)
        g = res["grouping_accuracy"]
        self.assertGreaterEqual(g["holdout"]["0.3.0"], 0.9)
        self.assertGreaterEqual(g["generated"]["0.3.0"], 0.9)
        self.assertGreater(g["holdout"]["0.3.0"],
                           3 * g["holdout"]["0.2.0 prefix only"])
        r = res["refs"]["holdout"]["0.3.0"]
        self.assertGreaterEqual(r["precision"], 0.95)
        self.assertGreaterEqual(r["recall"], 0.95)
        self.assertEqual(res["redaction"]["leaked"], 0)
        self.assertEqual(res["redaction"]["changed_by_mistake"], 0)
        self.assertTrue(res["noise_folding"]["real_work_folded_by_mistake"]
                        .startswith("0/"))


if __name__ == "__main__":
    unittest.main()
