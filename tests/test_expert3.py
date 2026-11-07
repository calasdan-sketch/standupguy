"""Round 3 (pass A) regressions: a hostile repository cannot run anything,
a ticket is not tagged twice, huge subjects are cut, `--scan`, `--stat`,
`--holidays`."""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_dates import commit_at, new_repo
from test_round5 import run_cli
from test_standupguy import git  # also isolates git config/HOME

from standupguy import agents as agents_mod
from standupguy import digest as digest_mod
from standupguy import gitlog as gitlog_mod
from standupguy import refs as refs_mod
from standupguy import scan as scan_mod

NOW = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def repo_with(*subjects):
    tmp = new_repo()
    for s in subjects:
        commit_at(tmp, s, NOW)
    return tmp


class TestHostileRepositoryConfig(unittest.TestCase):
    def test_fsmonitor_command_is_never_run(self):
        tmp = repo_with("feat: x")
        marker = os.path.join(tmp, "pwned.txt")
        # a command a hostile .git/config asks git to run on `git status`
        git(tmp, "config", "core.fsmonitor",
            f'{sys.executable} -c "open(r\'{marker}\',\'w\').write(\'x\')"')
        code, out, err = run_cli("--root", tmp, "--since", "today")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(marker))

    def test_signature_color_and_encoding_settings_do_not_change_the_digest(self):
        tmp = repo_with("feat: x [ABC-1]")
        for key, value in (("log.showSignature", "true"),
                           ("color.ui", "always"), ("log.decorate", "full"),
                           ("i18n.logOutputEncoding", "latin1"),
                           ("core.quotePath", "true")):
            git(tmp, "config", key, value)
        code, out, err = run_cli("--root", tmp, "--since", "today",
                                 "--format", "json")
        self.assertEqual(code, 0, err)
        commits = json.loads(out)["commits"]
        self.assertEqual([c["subject"] for c in commits], ["feat: x [ABC-1]"])

    def test_git_dir_from_the_environment_does_not_redirect_the_read(self):
        a = repo_with("feat: in a")
        b = repo_with("feat: in b")
        with mock.patch.dict(os.environ, {
                "GIT_DIR": os.path.join(b, ".git"), "GIT_WORK_TREE": b}):
            code, out, err = run_cli("--root", a, "--since", "today",
                                     "--format", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual([c["subject"] for c in json.loads(out)["commits"]],
                         ["feat: in a"])

    def test_reading_does_not_write_the_index(self):
        tmp = repo_with("feat: x")
        with open(os.path.join(tmp, "f.txt"), "w") as f:
            f.write("1")
        git(tmp, "add", "f.txt")
        index = os.path.join(tmp, ".git", "index")
        before = os.stat(index).st_mtime_ns
        env = gitlog_mod._git_env()
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        run_cli("--root", tmp, "--since", "today")
        self.assertEqual(os.stat(index).st_mtime_ns, before)
        self.assertFalse(os.path.exists(index + ".lock"))


class TestRepoFilters(unittest.TestCase):
    """`git status` runs a file's clean/process filter, and a repository's own
    config can name any command for it."""

    def filtered_repo(self, marker_command_key, scope="local"):
        tmp = repo_with("feat: x")
        with open(os.path.join(tmp, "x.txt"), "w") as f:
            f.write("hi\n")
        with open(os.path.join(tmp, ".gitattributes"), "w") as f:
            f.write("x.txt filter=evil\n")
        git(tmp, "add", ".")
        git(tmp, "commit", "-qm", "chore: add x")
        marker = os.path.join(tmp, "MARK")
        command = (f'{sys.executable} -c "open(r\'{marker}\',\'a\').write'
                   f'(\'x\')"; cat')
        return tmp, marker, command

    def dirty(self, tmp):
        # same size, newer time: git cannot tell by stat and runs the filter
        path = os.path.join(tmp, "x.txt")
        t = os.stat(path).st_mtime + 100
        os.utime(path, (t, t))

    def test_a_clean_filter_in_the_repo_config_is_not_run(self):
        tmp, marker, command = self.filtered_repo("clean")
        git(tmp, "config", "filter.evil.clean", command)
        git(tmp, "config", "filter.evil.required", "true")
        self.dirty(tmp)
        code, out, err = run_cli("--root", tmp, "--since", "today")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(marker))
        self.assertNotIn("x.txt", out)           # same content: not changed

    def test_a_process_filter_in_the_repo_config_is_not_run(self):
        tmp, marker, command = self.filtered_repo("process")
        git(tmp, "config", "filter.evil.process", command)
        self.dirty(tmp)
        code, out, err = run_cli("--root", tmp, "--since", "today")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(marker))

    def test_overrides_name_only_local_filters(self):
        tmp, marker, command = self.filtered_repo("clean")
        git(tmp, "config", "filter.a.b.clean", command)
        git(tmp, "config", "filter.Evil.required", "true")
        got = set(gitlog_mod._repo_filter_overrides(tmp))
        self.assertEqual(got, {"filter.a.b.clean=",
                               "filter.Evil.required=false"})

    def test_a_filter_from_your_own_global_config_still_runs(self):
        tmp, marker, command = self.filtered_repo("clean")
        mine = os.path.join(tempfile.mkdtemp(), "gitconfig")
        with open(mine, "w") as f:
            f.write("[filter \"evil\"]\n\tclean = " + command.replace(
                "\\", "\\\\").replace('"', '\\"') + "\n")
        self.dirty(tmp)
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": mine}):
            self.assertEqual(gitlog_mod._repo_filter_overrides(tmp), ())
            gitlog_mod.uncommitted(tmp)
        self.assertTrue(os.path.exists(marker))

    def test_a_filter_in_a_submodule_config_is_not_run_either(self):
        sub, marker, command = self.filtered_repo("clean")
        main = repo_with("feat: main")
        out = subprocess.run(
            ["git", "-C", main, "-c", "protocol.file.allow=always",
             "submodule", "add", "-q", sub, "vendored"],
            capture_output=True, env=dict(os.environ))
        if out.returncode:
            self.skipTest("git cannot add a submodule here")
        git(main, "commit", "-qm", "chore: add submodule")
        inner = os.path.join(main, "vendored")
        git(inner, "config", "filter.evil.clean", command)
        git(inner, "config", "filter.evil.required", "true")
        marker = os.path.join(inner, "MARK")
        command = command.replace(os.path.join(sub, "MARK"), marker)
        git(inner, "config", "filter.evil.clean", command)
        self.dirty(inner)
        code, out, err = run_cli("--root", main, "--since", "today")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(marker))

    def test_a_status_that_fails_is_a_warning_not_a_traceback(self):
        tmp = repo_with("feat: x")
        with open(os.path.join(tmp, ".git", "index"), "wb") as f:
            f.write(b"not an index at all")
        code, out, err = run_cli("--root", tmp, "--since", "today")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: could not read uncommitted files", err)
        self.assertNotIn("Traceback", err)
        self.assertIn("## Shipped", out)


class TestTagsAreNotRepeated(unittest.TestCase):
    def test_a_key_the_line_already_shows_is_not_tagged_again(self):
        r = refs_mod.extract("add a [ABC-12]")
        self.assertEqual(refs_mod.tags(r, "add a [ABC-12]"), "")
        self.assertEqual(refs_mod.tags(r, "add a"), "[ABC-12]")

    def test_only_whole_words_count(self):
        r = refs_mod.extract("ABC-12")
        self.assertEqual(refs_mod.tags(r, "see XABC-12 and ABC-123"),
                         "[ABC-12]")
        r = refs_mod.extract("fix #7")
        self.assertEqual(refs_mod.tags(r, "fix #77"), "[#7]")
        self.assertEqual(refs_mod.tags(r, "fix #7"), "")

    def test_the_digest_line_has_one_tag(self):
        tmp = repo_with("feat: add login [ABC-12]")
        code, out, err = run_cli("--root", tmp, "--since", "today")
        self.assertEqual(out.count("ABC-12"), 1, out)


class TestLongAndEmptyText(unittest.TestCase):
    def test_a_huge_subject_is_cut_on_the_line_and_whole_in_json(self):
        tmp = repo_with("feat: " + "A" * 100000)
        code, out, err = run_cli("--root", tmp, "--since", "today")
        self.assertLess(len(out), 2000)
        self.assertIn("…", out)
        code, js, err = run_cli("--root", tmp, "--since", "today",
                                "--format", "json")
        self.assertGreater(len(json.loads(js)["commits"][0]["subject"]),
                           100000)

    def test_a_secret_is_redacted_before_the_cut(self):
        token = "gh" + "p_" + "A1b2C3d4E5" * 3 + "A1b2C3"
        text = "x" * (digest_mod.MAX_LINE_CHARS - 10) + " " + token
        tmp = repo_with("fix: " + text)
        code, out, err = run_cli("--root", tmp, "--since", "today")
        self.assertNotIn("gh" + "p_", out)
        self.assertNotIn("A1b2C3d4", out)

    def test_an_empty_subject_reads_no_message(self):
        tmp = new_repo()
        git(tmp, "commit", "--allow-empty", "--allow-empty-message", "-qm", "")
        code, out, err = run_cli("--root", tmp, "--since", "today")
        self.assertIn("(no message)", out)
        code, out, err = run_cli("--root", tmp, "--since", "today",
                                 "--style", "ytb")
        self.assertIn("(no message)", out)


class TestAgentFastPath(unittest.TestCase):
    def test_no_hint_no_agent_and_the_hint_still_finds_one(self):
        plain = gitlog_mod.Commit("a" * 40, "Dan", "d@x.io", "2026-01-01",
                                  "x", [])
        self.assertIsNone(agents_mod.agent_of(plain))
        ai = gitlog_mod.Commit("b" * 40, "Dan", "d@x.io", "2026-01-01", "x",
                               [], coauthors=["Claude <noreply@anthropic.com>"])
        self.assertEqual(agents_mod.agent_of(ai), "Claude")
        foot = gitlog_mod.Commit("c" * 40, "Dan", "d@x.io", "2026-01-01", "x",
                                 [], body="Generated with Claude Code")
        self.assertEqual(agents_mod.agent_of(foot), "Claude")


class TestHolidays(unittest.TestCase):
    def tearDown(self):
        gitlog_mod.set_holidays(())

    def test_a_holiday_monday_makes_friday_the_last_workday(self):
        tuesday = datetime.date(2026, 10, 13)
        self.assertEqual(gitlog_mod.last_workday(tuesday),
                         datetime.date(2026, 10, 12))
        gitlog_mod.set_holidays({datetime.date(2026, 10, 12)})
        self.assertEqual(gitlog_mod.last_workday(tuesday),
                         datetime.date(2026, 10, 9))

    def test_a_long_run_of_days_off_is_skipped_whole(self):
        today = datetime.date(2026, 10, 13)
        gitlog_mod.set_holidays(today - datetime.timedelta(days=d)
                                for d in range(1, 20))
        self.assertEqual(gitlog_mod.last_workday(today),
                         datetime.date(2026, 9, 23))

    def test_parse(self):
        self.assertEqual(gitlog_mod.parse_holidays("2026-12-25, 2026-12-28,"),
                         frozenset({datetime.date(2026, 12, 25),
                                    datetime.date(2026, 12, 28)}))
        for bad in ("2026-13-01", "xmas", "2026-12-25;2026-12-26",
                    "20261225", "2026-W52-5", "2026-1-5"):
            with self.assertRaises(ValueError):
                gitlog_mod.parse_holidays(bad)

    def test_bad_value_is_an_error_not_a_guess(self):
        tmp = repo_with("feat: x")
        code, out, err = run_cli("--root", tmp, "--holidays", "2026-02-30")
        self.assertEqual(code, 2)
        self.assertIn("--holidays", err)
        with mock.patch.dict(os.environ, {"STANDUPGUY_HOLIDAYS": "oops"}):
            code, out, err = run_cli("--root", tmp)
        self.assertEqual(code, 2)
        self.assertIn("STANDUPGUY_HOLIDAYS", err)
        code, out, err = run_cli("--root", tmp, "--holidays", " ")
        self.assertEqual(code, 2)          # an empty value is an error too

    def test_the_option_moves_the_window(self):
        tmp = repo_with("feat: x")
        with mock.patch.object(gitlog_mod, "_today",
                               return_value=datetime.date(2026, 10, 13)):
            code, out, err = run_cli("--root", tmp, "--holidays",
                                     "2026-10-12")
        self.assertEqual(code, 0, err)
        self.assertIn("Fri 2026-10-09", out)


class TestScan(unittest.TestCase):
    def tree(self):
        base = tempfile.mkdtemp()
        for rel in ("a", "b", "c/node_modules/x", "deep/1/2/3/z", "n/inner"):
            path = os.path.join(base, *rel.split("/"))
            os.makedirs(path)
            git(path, "init", "-qb", "main")
            git(path, "config", "user.email", "dan@example.com")
            git(path, "config", "user.name", "Dan")
            commit_at(path, f"fix: in {rel.split('/')[-1]}", NOW)
        return base

    def test_finds_repos_skips_dependency_folders_and_deep_ones(self):
        base = self.tree()
        found = [os.path.relpath(p, base) for p in scan_mod.find_repos(base)]
        self.assertEqual(found, ["a", "b", os.path.join("n", "inner")])
        deeper = [os.path.relpath(p, base)
                  for p in scan_mod.find_repos(base, 5)]
        self.assertIn(os.path.join("deep", "1", "2", "3", "z"), deeper)
        self.assertNotIn(os.path.join("c", "node_modules", "x"), deeper)
        shouting = os.path.join(base, "Node_Modules", "q")
        os.makedirs(shouting)
        git(shouting, "init", "-qb", "main")
        self.assertFalse([p for p in scan_mod.find_repos(base, 5)
                          if "Node_Modules" in p])

    def test_does_not_look_inside_a_repo_or_follow_a_link(self):
        base = self.tree()
        nested = os.path.join(base, "a", "sub")
        os.makedirs(nested)
        git(nested, "init", "-qb", "main")
        outside = self.tree()
        try:
            os.symlink(outside, os.path.join(base, "link"))
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        found = [os.path.relpath(p, base) for p in scan_mod.find_repos(base, 4)]
        self.assertNotIn(os.path.join("a", "sub"), found)
        self.assertFalse([f for f in found if f.startswith("link")])

    def test_too_many_is_refused_not_cut(self):
        base = self.tree()
        with mock.patch.object(scan_mod, "MAX_REPOS", 2):
            with self.assertRaises(scan_mod.ScanError):
                scan_mod.find_repos(base)
        with mock.patch.object(scan_mod, "MAX_DIRS", 3):
            with self.assertRaises(scan_mod.ScanError):
                scan_mod.find_repos(base, 5)

    def test_not_a_folder_and_bad_depth(self):
        with self.assertRaises(scan_mod.ScanError):
            scan_mod.find_repos(os.path.join(tempfile.gettempdir(), "nope-x"))
        with self.assertRaises(scan_mod.ScanError):
            scan_mod.find_repos(tempfile.gettempdir(), 99)

    def test_cli_shows_only_repos_with_news(self):
        base = tempfile.mkdtemp()
        busy, quiet = os.path.join(base, "busy"), os.path.join(base, "quiet")
        for path, when in ((busy, NOW), (quiet, "2020-01-01T10:00:00+0000")):
            os.makedirs(path)
            git(path, "init", "-qb", "main")
            git(path, "config", "user.email", "dan@example.com")
            git(path, "config", "user.name", "Dan")
            commit_at(path, "fix: something", when)
        code, out, err = run_cli("--scan", base, "--since", "today")
        self.assertEqual(code, 0, err)
        self.assertIn("# Standup — busy", out)
        self.assertNotIn("quiet", out)

    def test_a_repo_given_twice_is_shown_once(self):
        base = self.tree()
        before = os.getcwd()
        try:
            os.chdir(base)
            code, out, err = run_cli("--scan", ".", "--root", "a",
                                     "--since", "today")
        finally:
            os.chdir(before)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("# Standup — a\n"), 1, out)

    def test_cli_nothing_found_and_errors(self):
        empty = tempfile.mkdtemp()
        code, out, err = run_cli("--scan", empty)
        self.assertEqual(code, 0)
        self.assertIn("no git repositories", err)
        code, out, err = run_cli("--scan", os.path.join(empty, "missing"))
        self.assertEqual(code, 2)
        code, out, err = run_cli("--scan", "")
        self.assertEqual(code, 2)
        code, out, err = run_cli("--scan", empty, "--scan-depth", "99")
        self.assertEqual(code, 2)

    def test_cli_json_has_a_repos_list(self):
        base = self.tree()
        code, out, err = run_cli("--scan", base, "--since", "today",
                                 "--format", "json")
        self.assertEqual(code, 0, err)
        names = [r["repo"] for r in json.loads(out)["repos"]]
        self.assertEqual(names, ["a", "b", "inner"])


class TestStat(unittest.TestCase):
    def repo(self):
        tmp = new_repo()
        with open(os.path.join(tmp, "a.txt"), "w") as f:
            f.write("1\n2\n3\n")
        with open(os.path.join(tmp, "bin.dat"), "wb") as f:
            f.write(b"\0\1\2\3")
        git(tmp, "add", ".")
        commit_at(tmp, "feat: add files", NOW)
        with open(os.path.join(tmp, "a.txt"), "w") as f:
            f.write("1\n3\n4\n5\n")
        git(tmp, "add", ".")
        commit_at(tmp, "fix: edit a", NOW)
        return tmp

    def test_numbers(self):
        tmp = self.repo()
        got = {c.subject: (c.added, c.deleted) for c in
               gitlog_mod.commits_since(tmp, "today", stat=True)}
        self.assertEqual(got["feat: add files"], (3, 0))   # binary: neither
        self.assertEqual(got["fix: edit a"], (2, 1))

    def test_off_by_default(self):
        tmp = self.repo()
        self.assertTrue(all(c.added is None for c in
                            gitlog_mod.commits_since(tmp, "today")))
        code, out, err = run_cli("--root", tmp, "--since", "today")
        self.assertNotIn("+3", out)
        code, js, err = run_cli("--root", tmp, "--since", "today",
                                "--format", "json")
        self.assertNotIn("added", json.loads(js)["commits"][0])

    def test_shown_and_in_json(self):
        tmp = self.repo()
        code, out, err = run_cli("--root", tmp, "--since", "today", "--stat")
        self.assertIn("(1 file, +2 -1)", out)
        code, js, err = run_cli("--root", tmp, "--since", "today", "--stat",
                                "--format", "json")
        by = {c["subject"]: c for c in json.loads(js)["commits"]}
        self.assertEqual((by["fix: edit a"]["added"],
                          by["fix: edit a"]["deleted"]), (2, 1))

    def test_a_file_name_with_digits_and_tabs_cannot_forge_numbers(self):
        tmp = new_repo()
        name = "9\t9\tx"
        if os.name == "nt":
            self.skipTest("no tabs in Windows file names")
        with open(os.path.join(tmp, name), "w") as f:
            f.write("1\n")
        git(tmp, "add", "-A")
        commit_at(tmp, "feat: odd name", NOW)
        c = gitlog_mod.commits_since(tmp, "today", stat=True)[0]
        self.assertEqual((c.added, c.deleted), (1, 0))


class TestServeRedactQuery(unittest.TestCase):
    def setUp(self):
        import threading
        import urllib.error
        import urllib.request
        from standupguy import serve as serve_mod
        self.urllib = urllib
        secret = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
        self.tmp = repo_with(f"fix: rotate {secret} for admin@corp.example")
        self.server = serve_mod.make_server(self.tmp, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.secret = secret

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def fetch(self, query):
        url = f"http://127.0.0.1:{self.port}/digest?since=today&all=1{query}"
        try:
            with self.urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.read().decode()
        except self.urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    def test_default_json_is_exact_and_redact_hides_the_commits_too(self):
        status, body = self.fetch("")
        self.assertIn(self.secret, json.loads(body)["commits"][0]["subject"])
        self.assertNotIn(self.secret, json.loads(body)["markdown"])
        status, body = self.fetch("&redact=standard")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertNotIn(self.secret, body)
        self.assertNotIn("admin@corp.example", data["commits"][0]["subject"])
        self.assertNotEqual(data["commits"][0]["email"], "dan@example.com")

    def test_strict_and_bad_values(self):
        status, body = self.fetch("&redact=strict")
        self.assertEqual(status, 200)
        self.assertNotIn(os.path.basename(self.tmp), json.loads(body)["repo"])
        status, body = self.fetch("&redact=bogus")
        self.assertEqual(status, 400)


class TestReadmeMentionsWhatItBuilt(unittest.TestCase):
    def test_new_options_are_documented(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "README.md"), encoding="utf-8") as f:
            text = f.read()
        for needle in ("--scan", "--scan-depth", "--stat", "--holidays",
                       "STANDUPGUY_HOLIDAYS", "redact="):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
