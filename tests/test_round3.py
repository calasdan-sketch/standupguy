"""Panel round 3 regressions: --author matching, --sprint, dates, Windows.

- --author must match like a case-insensitive substring of the git author
  name OR email (never a case-sensitive regex), including unicode names
  and needles with trailing spaces or regex metacharacters.
- --sprint is a real N-day window (or --since/--until) grouped by LOCAL
  day with a "Carried over" section (WIP commits + uncommitted work).
- --since values are handed to git verbatim, with " 00:00" appended only
  when no time of day was given; day headers use the local timezone, not
  the committer's.
- Non-ASCII commit subjects print on a cp1252 console without
  UnicodeEncodeError.
- pyproject declares the package explicitly (flat-layout auto-discovery
  refused to build the wheel) and the MIT license string.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime

try:
    import tomllib
except ImportError:             # Python 3.10: no TOML reader in the stdlib
    tomllib = None

from test_standupguy import commit, git  # also isolates git config/HOME

from standupguy import cli as cli_mod
from standupguy import digest as digest_mod
from standupguy import gitlog as gitlog_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GREEK = "Ξένος Καλάς"
GREEK_EMAIL = "xenos@example.com"


def make_two_author_repo() -> str:
    tmp = tempfile.mkdtemp()
    git(tmp, "init", "-qb", "main")
    git(tmp, "config", "user.email", "dan+work@example.com")
    git(tmp, "config", "user.name", "Dan Calas")
    commit(tmp, "Dan Calas", "dan+work@example.com", "2026-10-13T10:00:00",
           "feat: alpha", {"a.py": "1"})
    # trailing space in the name as typed; git itself trims it on commit
    commit(tmp, GREEK + " ", GREEK_EMAIL, "2026-10-14T09:00:00",
           "fix: βήτα crash 🐛", {"b.py": "2"})
    commit(tmp, "Dan Calas", "dan+work@example.com", "2026-10-14T16:00:00",
           "wip: gamma half done", {"c.py": "3"})
    commit(tmp, "Dan Calas", "dan+work@example.com", "2026-10-15T11:00:00",
           "docs: delta notes", {"d.md": "4"})
    with open(os.path.join(tmp, "e.py"), "w", encoding="utf-8") as f:
        f.write("uncommitted")
    return tmp


def _subjects(tmp, author, since="2026-10-01", until=None):
    return [c.subject for c in
            gitlog_mod.commits_since(tmp, since, author, until)]


class TestAuthorFilter(unittest.TestCase):
    def test_email_and_name_substrings_case_insensitive(self):
        tmp = make_two_author_repo()
        dan = {"feat: alpha", "wip: gamma half done", "docs: delta notes"}
        self.assertEqual(set(_subjects(tmp, "dan+work@example.com")), dan)
        self.assertEqual(set(_subjects(tmp, "DAN CALAS")), dan)
        self.assertEqual(set(_subjects(tmp, "calas")), dan)  # Καλάς != Calas
        self.assertEqual(set(_subjects(tmp, "Dan Calas <dan+work@example.com>")),
                         dan)
        self.assertEqual(_subjects(tmp, "teammate"), [])

    def test_unicode_name_any_case_and_trailing_space(self):
        tmp = make_two_author_repo()
        for needle in (GREEK, GREEK + "  ", "  " + GREEK, "ξένος", "ΚΑΛΆΣ",
                       GREEK_EMAIL.upper()):
            self.assertEqual(_subjects(tmp, needle), ["fix: βήτα crash 🐛"],
                             repr(needle))

    def test_regex_metacharacters_are_literal(self):
        tmp = make_two_author_repo()
        # '+' and '.' are literal; '[' used to make git fail ("Unmatched [")
        self.assertEqual(len(_subjects(tmp, "dan+work")), 3)
        self.assertEqual(_subjects(tmp, "dan[work"), [])
        self.assertEqual(_subjects(tmp, "d.n"), [])  # no regex dot

    def test_author_matches_unit(self):
        c = gitlog_mod.Commit("x", "Dan Calas", "dan+work@example.com",
                              "2026-10-14", "s", [])
        self.assertTrue(gitlog_mod.author_matches(c, "CALAS"))
        self.assertTrue(gitlog_mod.author_matches(c, " dan+work "))
        self.assertTrue(gitlog_mod.author_matches(c, ""))
        self.assertTrue(gitlog_mod.author_matches(c, None))
        self.assertFalse(gitlog_mod.author_matches(c, "bob"))

    def test_default_author_is_git_user_email(self):
        tmp = make_two_author_repo()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["--root", tmp, "--since", "2026-10-01",
                                 "--format", "json"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual({c["author"] for c in data["commits"]}, {"Dan Calas"})
        self.assertIn("email", data["commits"][0])


class TestSinceNormalization(unittest.TestCase):
    def test_words_passed_verbatim_with_midnight(self):
        # (today, yesterday, N days ago and N weeks ago are worked out here
        # as calendar days instead: see test_dates.TestSinceArgument)
        f = gitlog_mod._since_to_arg
        self.assertEqual(f(" 3 months ago "), "3 months ago 00:00")
        self.assertEqual(f("last monday"), "last monday 00:00")
        self.assertEqual(f("2026-10-14"), "2026-10-14 00:00")

    def test_explicit_times_untouched(self):
        f = gitlog_mod._since_to_arg
        for v in ("2026-10-14 15:30", "2026-10-14T09:00", "yesterday noon",
                  "midnight", "now", "2 hours ago", "30 minutes ago", "5pm",
                  "@1792128212", "1792128212"):
            self.assertEqual(f(v), v)
        self.assertEqual(f(""), "")

    def test_git_accepts_the_normalized_words(self):
        # end-to-end: a relative word with " 00:00" is a valid git date
        tmp = make_two_author_repo()
        for since in ("yesterday", "3 days ago", "last monday", "today"):
            gitlog_mod.commits_since(tmp, since)  # must not raise

    def test_until_bounds_the_range(self):
        tmp = make_two_author_repo()
        # a bare --until date is INCLUSIVE (through 23:59:59 local time)
        self.assertEqual(_subjects(tmp, None, "2026-10-14", "2026-10-14"),
                         ["wip: gamma half done", "fix: βήτα crash 🐛"])
        self.assertEqual(_subjects(tmp, None, "2026-10-14", "2026-10-15"),
                         ["docs: delta notes", "wip: gamma half done",
                          "fix: βήτα crash 🐛"])
        # an explicit time is taken literally
        self.assertEqual(_subjects(tmp, None, "2026-10-14", "2026-10-14 12:00"),
                         ["fix: βήτα crash 🐛"])


class TestLocalDates(unittest.TestCase):
    def test_day_header_is_local_time_not_committer_tz(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        # 23:30 in Los Angeles: a different calendar day almost everywhere
        # east of it. --date=short would print the committer's day.
        stamp = "2026-10-14T23:30:00-07:00"
        commit(tmp, "D", "d@e.c", stamp, "feat: late night", {"x": "1"})
        expected = datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d")
        cs = gitlog_mod.commits_since(tmp, "2026-10-01")
        self.assertEqual(cs[0].date, expected)
        md = digest_mod.weekly_markdown(tmp, None, since="2026-10-01")
        self.assertIn(f"## {expected}", md)
        md = digest_mod.sprint_markdown(tmp, "2026-10-01")
        self.assertIn(f"## {expected}", md)


class TestSprint(unittest.TestCase):
    def test_sprint_groups_by_day_with_carried_over(self):
        tmp = make_two_author_repo()
        md = digest_mod.sprint_markdown(tmp, "2026-10-01", None, None)
        self.assertTrue(md.startswith("# Sprint review — "))
        self.assertIn("## 2026-10-13", md)
        self.assertIn("## 2026-10-14", md)
        self.assertIn("## 2026-10-15", md)
        self.assertLess(md.index("## 2026-10-13"), md.index("## 2026-10-14"))
        self.assertIn("## Carried over", md)
        carried = md.split("## Carried over")[1]
        self.assertIn("gamma half done", carried)      # WIP commit
        self.assertIn("_(WIP commit)_", carried)
        self.assertIn("`e.py` _(uncommitted)_", carried)
        shipped = md.split("## Carried over")[0]
        self.assertNotIn("gamma half done", shipped)   # not counted as shipped
        self.assertIn("3 commits shipped", md)
        self.assertIn("dates in local time", md)
        self.assertIn("Highlights for sprint review", md)

    def test_sprint_window_and_range(self):
        tmp = make_two_author_repo()
        md = digest_mod.sprint_markdown(tmp, "2026-10-14", "2026-10-14", None)
        self.assertIn("2026-10-14 → 2026-10-14", md)
        self.assertNotIn("## 2026-10-13", md)
        self.assertNotIn("## 2026-10-15", md)
        self.assertIn("## 2026-10-14", md)
        md = digest_mod.sprint_markdown(tmp, "2026-10-14", "2026-10-15", None)
        self.assertIn("## 2026-10-15", md)  # bare --until is inclusive

    def test_nothing_carried_over(self):
        tmp = make_two_author_repo()
        os.remove(os.path.join(tmp, "e.py"))
        md = digest_mod.sprint_markdown(tmp, "2026-10-15", None, None)
        self.assertIn("_nothing carried over_", md)

    def test_is_wip(self):
        for s in ("wip: x", "WIP x", "[WIP] x", "feat: [wip] y", "Draft: z"):
            self.assertTrue(digest_mod.is_wip(s), s)
        for s in ("fix: wipe cache", "feat: swipe left", "docs: delta"):
            self.assertFalse(digest_mod.is_wip(s), s)

    def test_cli_sprint_flag_forms(self):
        tmp = make_two_author_repo()

        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli_mod.main(["--root", tmp, "--all", *argv])
            self.assertEqual(code, 0)
            return buf.getvalue()

        out = run("--sprint")  # default window: 14 days
        self.assertTrue(out.startswith("# Sprint review"))
        self.assertIn(f"since {digest_mod.DEFAULT_SPRINT_DAYS} days ago", out)
        out = run("--sprint", "30")
        self.assertIn("since 30 days ago", out)
        out = run("--sprint", "--since", "2026-10-14", "--until", "2026-10-15")
        self.assertIn("2026-10-14 → 2026-10-15", out)
        self.assertIn("## Carried over", out)
        data = json.loads(run("--sprint", "--since", "2026-10-01",
                              "--format", "json"))
        self.assertEqual(data["since"], "2026-10-01")
        self.assertIsNone(data["until"])
        self.assertTrue(data["label"].startswith("sprint"))
        wip = [c for c in data["commits"] if c["wip"]]
        self.assertEqual([c["subject"] for c in wip], ["wip: gamma half done"])

    def test_week_honours_since(self):
        tmp = make_two_author_repo()
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_mod.main(["--root", tmp, "--all", "--week", "--since",
                          "2026-10-15"])
        out = buf.getvalue()
        self.assertIn("since 2026-10-15", out)
        self.assertNotIn("## 2026-10-13", out)

    def test_bad_sprint_days_rejected(self):
        with self.assertRaises(SystemExit) as cm, \
                redirect_stdout(io.StringIO()):
            import contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                cli_mod.main(["--sprint", "0"])
        self.assertEqual(cm.exception.code, 2)


class TestConsoleEncoding(unittest.TestCase):
    def test_non_ascii_subject_prints_on_cp1252_console(self):
        tmp = make_two_author_repo()
        raw = io.BytesIO()
        strict = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with redirect_stdout(strict):
            code = cli_mod.main(["--root", tmp, "--all", "--since",
                                 "2026-10-01"])
            strict.flush()
        self.assertEqual(code, 0)
        text = raw.getvalue().decode("cp1252")
        self.assertIn("crash", text)       # the Greek/emoji line survived
        self.assertIn("## Shipped", text)

    def test_empty_digest_emoji_prints_on_cp1252_console(self):
        tmp = tempfile.mkdtemp()
        git(tmp, "init", "-qb", "main")
        raw = io.BytesIO()
        strict = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with redirect_stdout(strict):
            code = cli_mod.main(["--root", tmp, "--all"])
            strict.flush()
        self.assertEqual(code, 0)
        self.assertIn("Touch grass", raw.getvalue().decode("cp1252"))


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class TestPackaging(unittest.TestCase):
    def test_pyproject_is_explicit_about_the_flat_layout(self):
        with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
            pp = tomllib.load(f)
        self.assertEqual(pp["tool"]["setuptools"]["packages"], ["standupguy"])
        self.assertEqual(pp["project"]["license"], "MIT")
        self.assertTrue(pp["project"]["classifiers"])
        self.assertIn("setuptools>=77", pp["build-system"]["requires"][0])


if __name__ == "__main__":
    unittest.main()
