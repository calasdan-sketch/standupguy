"""Which day a commit belongs to, and what "yesterday" means.

Panel findings:
SG-4  `--since`/`--until` filter on the COMMITTER date, but the day labels were
      the AUTHOR date: `git commit --date=2026-09-27T10:00` made today, then
      `standupguy --since today --until today --format json` listed a commit
      "dated" 2026-09-27 in a window that holds only today. A rebase, an amend
      or a cherry-pick moves the committer date and keeps the author date, so
      this is an everyday case. Now one date decides both: the committer date,
      as a local calendar day.
SG-8  "yesterday" was left to git, which subtracts 24 hours: in the first hour
      after midnight on the day after the spring-forward Sunday it returned
      Saturday's commits.
"""

import contextlib
import datetime
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from test_round5 import run_cli
from test_standupguy import git  # also isolates git config/HOME

from standupguy import cli as cli_mod
from standupguy import digest as digest_mod
from standupguy import gitlog as gitlog_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def new_repo():
    tmp = tempfile.mkdtemp()
    git(tmp, "init", "-qb", "main")
    git(tmp, "config", "user.email", "dan@example.com")
    git(tmp, "config", "user.name", "Dan")
    return tmp


def commit_at(tmp, subject, committed, authored=None):
    """An empty commit whose committer date and author date are given."""
    env = {"GIT_COMMITTER_DATE": committed,
           "GIT_AUTHOR_DATE": authored or committed,
           "GIT_AUTHOR_NAME": "Dan", "GIT_AUTHOR_EMAIL": "dan@example.com",
           "GIT_COMMITTER_NAME": "Dan", "GIT_COMMITTER_EMAIL": "dan@example.com"}
    git(tmp, "commit", "--allow-empty", "-qm", subject, env=env)


def local_day(stamp):
    return datetime.datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d")


class TestADayIsTheCommitterDate(unittest.TestCase):
    def test_the_label_is_the_day_the_window_found_the_commit_on(self):
        tmp = new_repo()
        # written in September, rebased onto main on the 14th
        commit_at(tmp, "feat: written long ago", "2026-10-14T10:00:00",
                  authored="2026-09-27T10:00:00")
        found = gitlog_mod.commits_since(tmp, "2026-10-14", until="2026-10-14")
        self.assertEqual([(c.subject, c.date) for c in found],
                         [("feat: written long ago", "2026-10-14")])
        # the day it was first written is not a day this window can find it on
        self.assertEqual(
            gitlog_mod.commits_since(tmp, "2026-09-27", until="2026-09-27"), [])

    def test_every_commit_of_a_window_is_labelled_inside_it(self):
        tmp = new_repo()
        commit_at(tmp, "old work, rebased", "2026-10-14T09:00:00",
                  authored="2026-08-02T09:00:00")
        commit_at(tmp, "fresh work", "2026-10-14T11:00:00")
        commit_at(tmp, "amended later", "2026-10-15T08:00:00",
                  authored="2026-10-01T08:00:00")
        found = gitlog_mod.commits_since(tmp, "2026-10-14", until="2026-10-15")
        self.assertEqual(sorted(c.date for c in found),
                         ["2026-10-14", "2026-10-14", "2026-10-15"])

    def test_the_day_is_the_local_day_of_the_committer_date(self):
        tmp = new_repo()
        # 23:30 in Los Angeles is another calendar day almost everywhere
        # east of it; the author date is somewhere else entirely
        stamp = "2026-10-14T23:30:00-07:00"
        commit_at(tmp, "late night", stamp, authored="2026-01-05T12:00:00+09:00")
        found = gitlog_mod.commits_since(tmp, "2026-10-01")
        self.assertEqual(found[0].date, local_day(stamp))

    def test_the_headers_of_the_week_and_the_sprint_view(self):
        tmp = new_repo()
        commit_at(tmp, "feat: moved by a rebase", "2026-10-14T10:00:00",
                  authored="2026-09-27T10:00:00")
        for md in (digest_mod.weekly_markdown(tmp, None, since="2026-10-01"),
                   digest_mod.sprint_markdown(tmp, "2026-10-01")):
            self.assertIn("## 2026-10-14", md)
            self.assertNotIn("2026-09-27", md)

    def test_the_json_of_the_command_line(self):
        tmp = new_repo()
        commit_at(tmp, "feat: moved by a rebase", "2026-10-14T10:00:00",
                  authored="2026-09-27T10:00:00")
        code, out, err = run_cli("--root", tmp, "--since", "2026-10-14",
                                 "--until", "2026-10-14", "--format", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual([c["date"] for c in json.loads(out)["commits"]],
                         ["2026-10-14"])

    def test_the_readme_says_which_date(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            flat = " ".join(f.read().split())
        for phrase in ("committer date", "author date"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")


# ------------------------------------------------------------------ SG-8

# US and Canadian Central time, written as a POSIX rule so that it needs no
# time-zone database (Winnipeg keeps it): forward on the second Sunday of
# March, back on the first Sunday of November, both at 02:00.
POSIX_TZ = "CST6CDT,M3.2.0/2,M11.1.0/2"

# oldest first: git's --since walk assumes dates decrease from HEAD
WORK = [
    ("2026-03-01T12:00:00-06:00", "sunday mar 1"),
    ("2026-03-02T12:00:00-06:00", "monday mar 2"),
    ("2026-03-07T12:00:00-06:00", "saturday mar 7"),
    ("2026-03-08T12:00:00-05:00", "sunday mar 8"),      # clocks went forward at 02:00
    ("2026-10-31T12:00:00-05:00", "saturday oct 31"),
    ("2026-11-01T00:30:00-05:00", "early bird nov 1"),  # the first 00:30 of the day
    ("2026-11-01T12:00:00-06:00", "sunday nov 1"),      # clocks went back at 02:00
    ("2026-11-02T12:00:00-06:00", "monday nov 2"),
]


@contextlib.contextmanager
def it_is(stamp):
    """Pretend it is `stamp` (an ISO time with its offset) for git and for us."""
    moment = datetime.datetime.fromisoformat(stamp)
    env = {"TZ": POSIX_TZ, "GIT_TEST_DATE_NOW": str(int(moment.timestamp()))}
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(gitlog_mod, "_today", return_value=moment.date()):
        yield


class TestSinceArgument(unittest.TestCase):
    TODAY = datetime.date(2026, 10, 17)

    def arg(self, value, end=False, today=None):
        with mock.patch.object(gitlog_mod, "_today",
                               return_value=today or self.TODAY):
            return gitlog_mod._since_to_arg(value, end=end)

    def test_the_calendar_words_become_explicit_days(self):
        for value, day in (
                ("today", "2026-10-17"), ("yesterday", "2026-10-16"),
                ("Yesterday", "2026-10-16"), (" YESTERDAY ", "2026-10-16"),
                ("1 day ago", "2026-10-16"), ("3 days ago", "2026-10-14"),
                (" 3  days  ago ", "2026-10-14"), ("0 days ago", "2026-10-17"),
                ("10 DAYS AGO", "2026-10-07"), ("1 week ago", "2026-10-10"),
                ("2 weeks ago", "2026-10-03"), ("2.days.ago", "2026-10-15"),
                ("1.week.ago", "2026-10-10")):
            with self.subTest(value):
                self.assertEqual(self.arg(value), f"{day} 00:00")
                self.assertEqual(self.arg(value, end=True), f"{day} 23:59:59")

    def test_across_the_ends_of_months_and_years(self):
        day = datetime.date
        self.assertEqual(self.arg("yesterday", today=day(2026, 3, 1)),
                         "2026-02-28 00:00")
        self.assertEqual(self.arg("yesterday", today=day(2028, 3, 1)),
                         "2028-02-29 00:00")
        self.assertEqual(self.arg("yesterday", today=day(2026, 1, 1)),
                         "2025-12-31 00:00")
        self.assertEqual(self.arg("14 days ago", today=day(2026, 1, 3)),
                         "2025-12-20 00:00")

    def test_everything_else_is_still_gits(self):
        for value in ("last monday", "3 months ago", "1 year ago", "friday",
                      "last week", "two days ago", "yesterday!"):
            with self.subTest(value):
                self.assertEqual(self.arg(value), f"{value} 00:00")
                self.assertEqual(self.arg(value, end=True), f"{value} 23:59:59")
        self.assertEqual(self.arg("2026-10-14"), "2026-10-14 00:00")
        self.assertEqual(self.arg("2026-10-14", end=True), "2026-10-14 23:59:59")

    def test_a_time_of_day_means_it_is_taken_as_given(self):
        for value in ("yesterday noon", "yesterday 09:00", "2 hours ago", "now",
                      "midnight", "2026-10-14 15:30", "@1792128212", ""):
            with self.subTest(value):
                self.assertEqual(self.arg(value), value)
                self.assertEqual(self.arg(value, end=True), value)

    def test_a_number_too_big_for_a_date_is_left_to_git(self):
        for value in ("999999 weeks ago", "1000000 days ago",
                      "9" * 5000 + " days ago"):
            with self.subTest(value[:20]):
                self.assertEqual(self.arg(value), f"{value} 00:00")

    def test_today_is_the_real_one(self):
        self.assertEqual(gitlog_mod._today(), datetime.date.today())

    def test_the_readme_says_so(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            flat = " ".join(f.read().split())
        for phrase in ("whole local calendar days", "24 hours"):
            self.assertTrue(phrase in flat,
                            f"the README does not say: {phrase!r}")
        helptext = " ".join(cli_mod.build_parser().format_help().split())
        self.assertTrue("whole local calendar days" in helptext,
                        "--help does not say it")


class TestWholeCalendarDays(unittest.TestCase):
    """The panel's repro: at 00:10 on the Monday after the clocks went
    forward, git's "yesterday" (24 hours before now) is Saturday."""

    @classmethod
    def setUpClass(cls):
        cls.repo = new_repo()
        for stamp, subject in WORK:
            commit_at(cls.repo, subject, stamp)
        # does this git, on this platform, take the clock and the zone from
        # the environment, and have the hazard? Without that nothing here
        # shows anything
        with it_is("2026-03-09T00:10:00-05:00"):
            said = gitlog_mod._git(cls.repo, "rev-parse",
                                   "--since=yesterday 00:00").strip()
        saturday = datetime.datetime.fromisoformat("2026-03-07T00:00:00-06:00")
        cls.staged = said == f"--max-age={int(saturday.timestamp())}"

    def setUp(self):
        if not self.staged:
            self.skipTest("this git does not take TZ and GIT_TEST_DATE_NOW, "
                          "or counts the days right already")

    def subjects(self, since, until=None):
        return sorted(c.subject for c in
                      gitlog_mod.commits_since(self.repo, since, until=until))

    def test_yesterday_after_the_clocks_went_forward(self):
        with it_is("2026-03-09T00:10:00-05:00"):
            for word in ("yesterday", "1 day ago", "1.day.ago"):
                with self.subTest(word):
                    self.assertEqual(self.subjects(word, word), ["sunday mar 8"])
            self.assertEqual(self.subjects("2 days ago", "2 days ago"),
                             ["saturday mar 7"])
            for word in ("1 week ago", "7 days ago"):
                with self.subTest(word):
                    self.assertEqual(self.subjects(word, word), ["monday mar 2"])

    def test_yesterday_before_midnight_on_the_day_the_clocks_went_back(self):
        # git: 24 hours before 23:30 on the 25-hour day is the same day
        with it_is("2026-11-01T23:30:00-06:00"):
            for word in ("yesterday", "1 day ago"):
                with self.subTest(word):
                    self.assertEqual(self.subjects(word, word),
                                     ["saturday oct 31"])

    def test_today_starts_at_midnight_on_the_day_the_clocks_went_back(self):
        # git: "today 00:00" there is 01:00, so the commit at 00:30 is lost
        with it_is("2026-11-01T23:30:00-06:00"):
            self.assertEqual(self.subjects("today", "today"),
                             ["early bird nov 1", "sunday nov 1"])

    def test_the_command_line_default_window(self):
        with it_is("2026-03-09T00:10:00-05:00"):
            code, out, err = run_cli("--root", self.repo, "--all", "--until",
                                     "yesterday", "--format", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual([c["subject"] for c in json.loads(out)["commits"]],
                         ["sunday mar 8"])


if __name__ == "__main__":
    unittest.main()
