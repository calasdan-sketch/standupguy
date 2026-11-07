"""Git ground truth: commits, files, merges, uncommitted work."""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import secrets
import subprocess
import sys
import unicodedata


@dataclasses.dataclass
class Commit:
    sha: str
    author: str
    email: str
    date: str          # committer date, YYYY-MM-DD in the LOCAL timezone of this machine
    subject: str
    files: list[str]
    pr_number: int | None = None
    # the people named in Co-authored-by: trailers ("Name <email>"), as
    # written by whoever made the commit
    coauthors: list[str] = dataclasses.field(default_factory=list)
    # the message body (everything after the subject), cut at MAX_BODY_CHARS.
    # Read only to find ticket and pull-request numbers and agent footers;
    # it is never printed.
    body: str = ""
    # lines added / removed (`--stat`); None when not asked for. A binary
    # file counts for neither.
    added: int | None = None
    deleted: int | None = None


class GitNotFound(RuntimeError):
    """`git` is not on PATH (or not installed)."""


class GitError(RuntimeError):
    """A git command exited with an error. str() is a one-line summary (git's
    message cut at 200 characters); `stderr` is everything git said."""

    def __init__(self, args_text: str, stderr: str):
        super().__init__(f"git {args_text} failed: {stderr.strip()[:200]}")
        self.stderr = stderr.strip()


#: How long one git command may run before it is given up on, in seconds.
GIT_TIMEOUT = 60


class GitTimeout(RuntimeError):
    """git did not finish within GIT_TIMEOUT seconds (a repository on a drive
    that went away, say). str() is one line for a person: the git command
    (its name only, no paths) and how long it was given."""

    def __init__(self, command: str, seconds):
        unit = "second" if seconds == 1 else "seconds"
        super().__init__(
            f"git {command} did not finish within {seconds} {unit} - is the "
            "repository on a slow or disconnected drive?")


GIT_NOT_FOUND_MESSAGE = (
    "git not found on PATH. Install Git (https://git-scm.com/downloads; on "
    "Windows: Git for Windows), then open a new terminal.")


#: Settings forced on every git call. A repository (or the user's own
#: config) can say `core.fsmonitor = <command>`, which git RUNS on
#: `git status`; `log.showSignature` runs `gpg.program` for every commit and
#: prints its output into the log; `color.ui = always`, `log.decorate`,
#: `i18n.logOutputEncoding` and `core.quotePath` change the bytes this module
#: parses. A digest tool only reads, so none of that may apply to it.
SAFE_CONFIG = (
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.quotePath=false",
    "log.showSignature=false",
    "log.decorate=no",
    "log.abbrevCommit=false",
    "log.follow=false",
    "color.ui=false",
    "i18n.logOutputEncoding=UTF-8",
    "core.pager=cat",
)

#: Variables that make `git -C <root>` talk to some OTHER repository (set,
#: for example, inside a git hook or `git rebase --exec`), or that point
#: git at a trace file. The explicit root always wins.
_DROPPED_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EXTERNAL_DIFF", "GIT_PAGER",
    "GIT_TRACE", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF",
    "GIT_TRACE_PACKET", "GIT_TRACE_SETUP",
)


def _git_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
    # `git status` normally refreshes the index and writes it back: a read
    # of someone's repository must not take its lock or change it.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    if extra:
        env.update(extra)
    return env


def _git(root: str, *args: str, env: dict | None = None,
         config: tuple = ()) -> str:
    full_env = _git_env(env)
    config = [x for c in (*SAFE_CONFIG, *config) for x in ("-c", c)]
    # git prints UTF-8 (commit messages, author names): decode it by hand,
    # never with the console code page, never dying on a stray byte, and
    # never in text mode — universal newlines would turn a lone CR inside a
    # commit subject into a newline and corrupt the record around it.
    try:
        p = subprocess.run(["git", *config, "-C", root, *args], capture_output=True,
                           timeout=GIT_TIMEOUT, env=full_env)
    except FileNotFoundError:
        raise GitNotFound(GIT_NOT_FOUND_MESSAGE) from None
    except subprocess.TimeoutExpired:
        raise GitTimeout(args[0] if args else "", GIT_TIMEOUT) from None
    if p.returncode != 0:
        raise GitError(" ".join(args), p.stderr.decode("utf-8", "replace"))
    return p.stdout.decode("utf-8", "replace")


_MERGE_RE = re.compile(r"merge pull request #(\d+)", re.IGNORECASE)

# Does a git date string already carry a time of day? Then it is passed to
# git untouched. Matches 10:00, 2026-10-14T10:00, noon/midnight/now/tea,
# "3 hours ago", 5pm, @1700000000 and raw unix timestamps: nine digits or
# more, which is where git itself starts reading a bare number as seconds
# since 1970. Eight digits are a date, see _COMPACT_DATE_RE.
_HAS_TIME_RE = re.compile(
    r"\d{1,2}:\d{2}"
    r"|\b(?:noon|midnight|now|tea|hours?|minutes?|seconds?)\b"
    r"|\d\s*[ap]m\b"
    r"|^@\d+$"
    r"|^\d{9,}$",
    re.IGNORECASE)

# YYYYMMDD. Git reads a bare eight-digit number as "now" (it takes one for a
# date only when a time of day follows it), so the digest came back empty
# without a word. Here it is the day it says, like 2026-10-14: _since_to_arg
# turns it into that day, check_date refuses one that is not a real day.
_COMPACT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")

# An ISO-looking calendar date (optionally followed by a time). These are
# the values git's fuzzy parser gets WRONG without complaint: it turned
# "2026-13-45" into October 13 and said nothing.
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$")

# Words git's approxidate understands. Anything else in a date string is
# silently ignored by git ("notadate" means "now"), so we warn about it.
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_WEEKDAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday",
             "friday", "saturday")
# git counts in words as well: "two days ago", "one friday ago"
_NUMBER_WORDS = ("one", "two", "three", "four", "five", "six", "seven",
                 "eight", "nine", "ten")
_DATE_WORDS = {
    "now", "today", "yesterday", "noon", "midnight", "tea", "never",
    "am", "pm", "ago", "last", "this", "next",
    "second", "seconds", "minute", "minutes", "hour", "hours",
    "day", "days", "week", "weeks", "month", "months", "year", "years",
    "fortnight", "utc", "gmt", "z", "t", *_NUMBER_WORDS,
}


def _weekday(word: str) -> str | None:
    """The weekday a word names the way git reads it, else None: a prefix of
    the plural ("mon", "monday", "mondays"), 3 letters or more."""
    if len(word) < 3:
        return None
    return next((day for day in _WEEKDAYS if (day + "s").startswith(word)),
                None)


def _known_date_word(word: str) -> bool:
    if word in _DATE_WORDS:
        return True
    # git matches month/weekday names by prefix, 3 letters or more
    return len(word) >= 3 and (
        any(name.startswith(word) for name in _MONTHS)
        or _weekday(word) is not None)


# What may stand right before a weekday name for git to read it: "last
# monday", "2 mondays ago", "one friday ago".
_COUNT_WORDS = ("last", *_NUMBER_WORDS)


# A numeric date with its year, wherever it stands in a value: 2026-10-12,
# 2026/10/12, 10/12/2026, 12.10.2026.
_NUMBER_DATE_RE = re.compile(
    r"\b(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})\b")
# A time of day ("09:00:00", "5pm"): its numbers are not a day of the month.
_CLOCK_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?|\d{1,2}\s*[ap]m\b",
                       re.IGNORECASE)


def _names_its_day(s: str) -> bool:
    """Does `s` pin a calendar day by its date, whatever weekday it also
    names? A numeric date (2026-10-12, 10/12/2026), or a month name next to a
    day of the month: "Sun Oct 18 09:00:00 2026 -0500" (what `git log` prints,
    and `--since "$(git log -1 --format=%cd)"` pastes), "Sun, 18 Oct 2026"
    (RFC 2822), "monday 12 october". Git reads the date and ignores the
    weekday (even a wrong one), so there is nothing to warn about."""
    if _NUMBER_DATE_RE.search(s):
        return True
    words = re.findall(r"[a-z]+|[0-9]+", _CLOCK_RE.sub(" ", s.lower()))
    return (any(len(w) >= 3 and any(name.startswith(w) for name in _MONTHS)
                for w in words)
            and any(len(w) <= 2 and w.isdigit() and 1 <= int(w) <= 31
                    for w in words))


def _skipped_weekday(s: str) -> str | None:
    """The first weekday name in `s` that git will skip, else None.

    Git's date parser reads a weekday name only right after a count ("last
    monday", "2 mondays ago"). Anywhere else the word is skipped without a
    word, so a bare "monday" (also "this monday", "monday 9am") means TODAY."""
    words = re.findall(r"[a-z]+|[0-9]+", s.lower())
    for i, word in enumerate(words):
        day = _weekday(word)
        if day is None:
            continue
        before = words[i - 1] if i else ""
        if not (before.isdigit() or before in _COUNT_WORDS):
            return day
    return None


def check_date(value: str) -> str | None:
    """Sanity-check a --since/--until value BEFORE git guesses at it.

    Raises ValueError for an ISO-looking date that is not a real date
    ("2026-13-45", "2026-02-30"). Returns a warning string when the value
    contains words git's date parser does not know (it ignores them and
    quietly falls back to "now"), else None.
    """
    s = (value or "").strip()
    if not s or _CALENDAR_DAY_RE.match(s):
        return None
    m = _ISO_DATE_RE.match(s) or _COMPACT_DATE_RE.match(s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            datetime.date(y, mo, d)
        except ValueError as e:
            raise ValueError(f"{s!r} is not a valid date ({e})") from None
        return None
    unknown = [w for w in re.findall(r"[a-z]+", s.lower())
               if not _known_date_word(w)]
    if unknown:
        return (f"'{s}' is not a date git understands (unknown: "
                f"{', '.join(unknown)}); git will guess — probably 'now'. "
                f"Try 'yesterday', '3 days ago', 'last monday' or 2026-10-14.")
    day = _skipped_weekday(s)
    if day and not _names_its_day(s):
        return (f"'{s}': git skips a weekday name unless 'last' or a number "
                f"comes right before it (a bare '{day}' means today). Use "
                f"'last {day}' for the most recent {day.capitalize()} before "
                f"today, or a date such as 2026-10-12.")
    return None


# today, yesterday, N days ago, N weeks ago (git also takes "2.days.ago"),
# and standupguy's own "last workday" (git has no such word)
_CALENDAR_DAY_RE = re.compile(
    r"^(?:(today)|(yesterday)|([0-9]{1,6})[\s.]+(day|week)s?[\s.]+ago"
    r"|(last[\s.]+work(?:ing)?[\s.]*day))$",
    re.IGNORECASE)

#: The value the daily digest starts from when no --since is given.
LAST_WORKDAY = "last workday"

#: Default working days, Monday=0 ... Sunday=6: Monday to Friday.
DEFAULT_WORKDAYS = frozenset(range(5))

# The working days "last workday" counts back over. Set once at start-up
# (--workdays, STANDUPGUY_WORKDAYS) with set_workdays().
_workdays = DEFAULT_WORKDAYS

def _day_number(word: str) -> int:
    """Monday=0 ... Sunday=6 for a weekday name of 3 letters or more."""
    w = word.strip().lower()
    for i in range(7):
        full = _WEEKDAYS[(i + 1) % 7]      # _WEEKDAYS starts on Sunday
        if len(w) >= 3 and full.startswith(w):
            return i
    raise ValueError(f"{word.strip()[:40]!r} is not a weekday")


def parse_workdays(spec: str) -> frozenset:
    """The working days `spec` names, Monday=0 ... Sunday=6.

    `spec` is a comma-separated list of weekdays and ranges: "mon-fri" (the
    default), "sun-thu", "mon,tue,thu", "fri-mon" (a range may wrap past
    Sunday). Names are 3 letters or more, any case. Raises ValueError for
    anything else - an empty list included: with no working day there is no
    "last workday" to count back to."""
    days: set[int] = set()
    parts = [p for p in (spec or "").split(",")]
    if not any(p.strip() for p in parts):
        raise ValueError("needs at least one weekday, e.g. mon-fri")
    for part in parts:
        if not part.strip():
            raise ValueError(f"{spec.strip()[:40]!r} has an empty item")
        first, sep, last = part.partition("-")
        start = _day_number(first)
        if not sep:
            days.add(start)
            continue
        end = _day_number(last)
        i = start
        days.add(i)
        while i != end:
            i = (i + 1) % 7
            days.add(i)
    return frozenset(days)


def set_workdays(days) -> None:
    """Make `days` (from parse_workdays) the working days "last workday"
    counts back over."""
    global _workdays
    days = frozenset(days)
    if not days or not days <= set(range(7)):
        raise ValueError("needs at least one weekday, e.g. mon-fri")
    _workdays = days


_holidays: frozenset = frozenset()


def parse_holidays(spec: str) -> frozenset:
    """The days off `spec` names: comma-separated ISO dates
    (`2026-12-25,2026-12-28`). standupguy knows no public holiday calendar
    (they differ by country and region): you say which days were not
    workdays. Raises ValueError for anything that is not a real date."""
    days = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            # strictly YYYY-MM-DD: Python 3.11+ would also take 20261225
            # and 2026-W52-5, Python 3.10 would not
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
                raise ValueError(part)
            days.add(datetime.date.fromisoformat(part))
        except ValueError:
            raise ValueError(f"{part[:40]!r} is not a date like "
                             "2026-12-25") from None
        if len(days) > 400:
            raise ValueError("more than 400 days off")
    return frozenset(days)


def set_holidays(days) -> None:
    """Days `last_workday` skips even when they fall on a working weekday."""
    global _holidays
    _holidays = frozenset(days)


def last_workday(today: datetime.date | None = None) -> datetime.date:
    """The most recent working day BEFORE `today` (default: today): Friday
    on a Monday, a Saturday or a Sunday; yesterday on Tuesday to Friday.
    A day listed with --holidays is skipped like a weekend."""
    today = today or _today()
    # a working weekday turns up within a week, so within 7 * (days off + 1)
    # days at most: every day off can push the search back one more week
    for back in range(1, 7 * (len(_holidays) + 1) + 1):
        day = today - datetime.timedelta(days=back)
        if day.weekday() in _workdays and day not in _holidays:
            return day
    raise AssertionError("no working day")     # set_workdays refuses that


def _today() -> datetime.date:
    """Today's date on this machine. A function so a test can pick the day."""
    return datetime.date.today()


def _calendar_day(value: str) -> datetime.date | None:
    """The local calendar day a bare `today`, `yesterday`, `N days ago` or
    `N weeks ago` stands for, else None.

    Git counts these in blocks of 24 hours from now, which is a day too early
    or too late in the first or last hour after midnight on the day after a
    clock change: at 00:10 on the Monday after the clocks went forward,
    git's "yesterday" is Saturday. Calendar arithmetic does not care how long
    the days were."""
    m = _CALENDAR_DAY_RE.match(value)
    if not m:
        return None
    today = _today()
    if m.group(5):
        return last_workday(today)
    try:
        if m.group(1):
            return today
        if m.group(2):
            return today - datetime.timedelta(days=1)
        days = int(m.group(3)) * (7 if m.group(4).lower() == "week" else 1)
        return today - datetime.timedelta(days=days)
    except OverflowError:       # before year 1: leave it to git
        return None


def calendar_day(value: str) -> datetime.date | None:
    """The local day `value` stands for when standupguy works it out itself
    (today, yesterday, N days/weeks ago, last workday), else None."""
    return _calendar_day((value or "").strip())


def _compact_day(value: str) -> datetime.date | None:
    """The day a bare YYYYMMDD stands for, else None (also when the digits
    are no day: 20261345)."""
    m = _COMPACT_DATE_RE.match(value)
    if not m:
        return None
    try:
        return datetime.date(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def _since_to_arg(since: str, end: bool = False) -> str:
    """Normalize a `since`/`until` value for git.

    Git's approxidate fills in the CURRENT time of day for a bare date or
    a word like "yesterday" ("2026-10-14" run at 15:00 means 2026-10-14
    15:00; "yesterday" means 24 hours ago) — silently dropping that
    morning's commits. For a standup you want whole days, so any value
    without an explicit time gets " 00:00" appended (or " 23:59:59" when
    it is the END of the range, so `--until 2026-10-15` includes the
    15th).

    The calendar words (today, yesterday, N days ago, N weeks ago) are
    turned into the explicit local date here, see _calendar_day, and so is a
    bare YYYYMMDD (see _COMPACT_DATE_RE); any other word is passed to git
    VERBATIM — git does the date math, in its own local timezone, exactly as
    it would for the user typing it:

        yesterday      -> "2026-10-16 00:00"     (end: "2026-10-16 23:59:59")
        3 days ago     -> "2026-10-14 00:00"     (today is 2026-10-17)
        2026-10-14     -> "2026-10-14 00:00"     (end: "2026-10-14 23:59:59")
        20261014       -> "2026-10-14 00:00"
        last monday    -> "last monday 00:00"

    Values that already carry a time ("2026-10-14 15:30", "noon", "now",
    "2 hours ago", "@1700000000") pass through unchanged.
    """
    s = (since or "").strip()
    if not s or _HAS_TIME_RE.search(s):
        return s
    day = _calendar_day(s)
    if day is None:
        day = _compact_day(s)
    if day is not None:
        s = day.isoformat()
    return f"{s} 23:59:59" if end else f"{s} 00:00"


def _fold(s: str) -> str:
    """Case- and width-insensitive form for author matching (NFKC + casefold),
    so 'ΞΈΝΟΣ' finds 'Ξένος' and trailing spaces never matter."""
    return unicodedata.normalize("NFKC", s or "").casefold().strip()


# A whole e-mail address: something@domain.tld, no spaces or angle brackets.
_EMAIL_RE = re.compile(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+")


def _address(coauthor: str) -> str:
    """The e-mail address of a "Name <email>" trailer value (the last <...>),
    or the whole value when it has none."""
    m = re.search(r"<([^<>]*)>\s*$", coauthor)
    return m.group(1) if m else coauthor


def author_matches(commit: Commit, needle: str | None) -> bool:
    """Does `needle` select this commit's author?

    Like `git log --author` but predictable, never a regex (so
    'dan+work@x.com' or 'J. Doe [bot]' match literally and can't make git
    fail), never case-sensitive, and whitespace around the needle is
    ignored. An empty needle matches everyone.

    - A whole e-mail address (``dan@example.com``, the default from
      ``git config user.email``) matches that address EXACTLY: the author's,
      or the address of a ``Co-authored-by: Name <email>`` trailer. A
      substring would also find ``jordan@example.com``.
    - Anything else (``calas``, ``teammate@co``, ``"Dan <dan@x.com>"``) is a
      case-insensitive substring of the author NAME, the author EMAIL, the
      combined "Name <email>" form, or a co-author trailer.

    Co-authors count because work you paired on is your work too.
    """
    n = _fold(needle or "")
    if not n:
        return True
    if _EMAIL_RE.fullmatch(n):
        return n == _fold(commit.email) or any(
            n == _fold(_address(c)) for c in commit.coauthors)
    name, email = _fold(commit.author), _fold(commit.email)
    if n in name or n in email or n in f"{name} <{email}>":
        return True
    return any(n in _fold(c) for c in commit.coauthors)


# One record per commit: RS (0x1e) + sha US (0x1f) name US email US date US
# subject, then (with -z) the touched paths NUL-separated; -z keeps paths
# raw (no quoting), so a '|' in an author name or subject, or a path that
# happens to start with "COMMIT:", cannot break the parse.
#
# Every separator is followed by a random per-call TOKEN. A commit subject,
# an author name or even a file name can hold any byte but NUL — RS and US
# included — so with fixed separators a hostile commit closes its own
# record and writes a fake one (fake sha, author, date), or shifts the
# fields of a real one. The token is picked at run time, after the commit
# was made: no commit can contain it. (token="" is the bare format.)
#
# The date is the COMMITTER date (%cd), the one --since and --until filter on,
# so a commit is never labelled with a day outside the window that found it.
# (The author date, %ad, survives a rebase, an amend or a cherry-pick; the
# committer date does not.)
#
# A sixth field holds the Co-authored-by: trailers, valueonly and unfolded,
# separated by GS (0x1d) + the token. (A git whose %(trailers) does not
# know these options prints the placeholder as it is; parse_log ignores it.)
def _log_format(token: str = "") -> str:
    rs, us, gs = "%x1e" + token, "%x1f" + token, "%x1d" + token
    trailers = ("%(trailers:key=Co-authored-by,valueonly,unfold,"
                f"separator={gs})")
    return f"--pretty=format:{rs}%H{us}%aN{us}%aE{us}%cd{us}%s{us}{trailers}"


#: Longest message body kept per commit (characters).
MAX_BODY_CHARS = 4000


def _bodies(root: str, window: list, token: str) -> dict:
    """sha -> message body for the commits of the window. A second, small
    `git log` (a body has line breaks of its own, which the main record
    cannot carry). Bodies are optional: if git fails, there are none."""
    rs, us = "\x1e" + token, "\x1f" + token
    try:
        out = _git(root, "log", "-z", *window, f"--format={rs}%H{us}%b",
                   "HEAD")
    except (GitNotFound, GitTimeout):
        raise
    except RuntimeError:
        return {}
    bodies = {}
    for record in out.split(rs):
        sha, sep, body = record.partition(us)
        sha = sha.strip("\0\n")
        if sep and _SHA_RE.fullmatch(sha):
            bodies[sha] = body.strip("\0")[:MAX_BODY_CHARS]
    return bodies


def _numstat(root: str, window: list, token: str) -> dict:
    """sha -> (lines added, lines removed) for the commits of the window,
    from one `git log --numstat`. Renames are not followed (a moved file is
    a delete and an add), so a move shows as churn rather than hiding it.
    Optional: if git fails, there are no numbers."""
    rs = "\x1e" + token
    try:
        out = _git(root, "log", *window, "--no-renames", "--numstat",
                   f"--format={rs}%H", "HEAD")
    except (GitNotFound, GitTimeout):
        raise
    except RuntimeError:
        return {}
    stats: dict = {}
    sha = None
    for line in out.split("\n"):
        if line.startswith(rs):
            sha = line[len(rs):].strip()
            if _SHA_RE.fullmatch(sha):
                stats[sha] = [0, 0]
            else:
                sha = None
            continue
        if sha is None:
            continue
        m = _NUMSTAT_RE.match(line)
        if m:
            a, d = m.groups()
            if a != "-":
                stats[sha][0] += int(a)
            if d != "-":
                stats[sha][1] += int(d)
    return {k: (v[0], v[1]) for k, v in stats.items()}


_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t")


#: At most this many co-authors are kept per commit, each cut at
#: MAX_COAUTHOR_CHARS: a commit can carry thousands of trailers.
MAX_COAUTHORS = 20
MAX_COAUTHOR_CHARS = 200


def _coauthors(field: str, token: str) -> list[str]:
    if not field or field.startswith("%("):     # old git: no trailers
        return []
    names = [" ".join(v.split())[:MAX_COAUTHOR_CHARS]
             for v in field.split("\x1d" + token)]
    return [n for n in names if n][:MAX_COAUTHORS]


# What a record must look like to be believed: a hex sha (the exact set of
# real ones is checked against `git rev-list`, see commits_since) and the
# YYYY-MM-DD that --date=format-local:%Y-%m-%d prints.
_SHA_RE = re.compile(r"[0-9a-f]+")
_DAY_RE = re.compile(r"\d+-\d\d-\d\d")


def parse_log(out: str, token: str = "") -> list[Commit]:
    """Parse the output of `git log -z <_log_format(token)> --name-only`.

    A record that is not five fields (six with the co-author trailers), or
    whose sha is not hex or whose date is not YYYY-MM-DD, is dropped rather
    than guessed at."""
    rs, us = "\x1e" + token, "\x1f" + token
    commits: list[Commit] = []
    for record in out.split(rs):
        header, _, body = record.partition("\n")
        header = header.strip("\0")
        if not header:
            continue
        fields = header.split(us, 5)
        if len(fields) == 5:
            fields.append("")
        if len(fields) != 6:
            continue
        sha, an, ae, ad, subject, trailers = fields
        if not (_SHA_RE.fullmatch(sha) and _DAY_RE.fullmatch(ad)):
            continue
        m = _MERGE_RE.search(subject)
        commits.append(Commit(
            sha=sha, author=an, email=ae, date=ad, subject=subject,
            files=[p for p in body.split("\0") if p],
            pr_number=int(m.group(1)) if m else None,
            coauthors=_coauthors(trailers, token)))
    return commits


def commits_since(root: str, since: str, author: str | None = None,
                  until: str | None = None, stat: bool = False
                  ) -> list[Commit]:
    """since/until: 'yesterday', '7 days ago', '2026-10-01', or git date
    syntax (see _since_to_arg; a bare `until` date is inclusive).
    author: see author_matches."""
    # --date=format-local: the day header is the LOCAL date of this
    # machine, not the committer's timezone (--date=short), so a 23:30
    # PDT commit lands on the right day for a user in Winnipeg. It applies
    # to %cd, the committer date (see _log_format).
    window = [f"--since={_since_to_arg(since)}"]
    if until:
        window.append(f"--until={_since_to_arg(until, end=True)}")
    token = secrets.token_hex(8)    # see _log_format
    try:
        out = _git(root, "log", "-z", *window, "--date=format-local:%Y-%m-%d",
                   _log_format(token), "--name-only", "HEAD")
        # Second opinion: the shas git itself lists for the same range.
        # Asked AFTER the log, so a commit made in between can only add.
        real = set(_git(root, "rev-list", *window, "HEAD").split())
    except (GitNotFound, GitTimeout):
        raise
    except RuntimeError as e:
        # empty repo (no commits yet) -> no history, not an error
        if "unknown revision" in str(e):
            return []
        raise
    commits = [c for c in parse_log(out, token) if c.sha in real]
    if commits:
        bodies = _bodies(root, window, token)
        for c in commits:
            c.body = bodies.get(c.sha, "")
        if stat:
            numbers = _numstat(root, window, token)
            for c in commits:
                if c.sha in numbers:
                    c.added, c.deleted = numbers[c.sha]
    if author:
        commits = [c for c in commits if author_matches(c, author)]
    return commits


_FILTER_KEY_RE = re.compile(r"filter\..+\.(clean|smudge|process|required)$",
                            re.I | re.S)


def _repo_filter_overrides(root: str) -> tuple:
    """`-c` settings that switch off every content filter this repository's
    OWN config defines. `git status` runs a file's `clean` (or `process`)
    filter to compare it with the index, and a repository can name any
    command there: `.gitattributes` says `* filter=x`, `.git/config` says
    `filter.x.clean = <command>`. Filters from your system or global config
    (git-lfs, say) are yours and stay on. Needs a git that can say where a
    setting came from (`--show-scope`, 2.26); without one, or if the list
    cannot be read, this raises and `uncommitted` reports nothing rather
    than run a command nobody vetted."""
    out = _git(root, "config", "--list", "-z", "--show-scope")
    found = []
    fields = out.split("\0")
    # with -z each setting is two fields: its scope, then "key\nvalue"
    for scope, entry in zip(fields[0::2], fields[1::2]):
        if scope not in ("local", "worktree"):
            continue
        key = entry.partition("\n")[0]
        m = _FILTER_KEY_RE.fullmatch(key)
        if not m:
            continue
        found.append(key.rsplit(".", 1)[0] + (".required=false"
                     if m.group(1).lower() == "required"
                     else "." + m.group(1).lower() + "="))
    return tuple(dict.fromkeys(found))


def uncommitted(root: str) -> list[str]:
    """Repo-relative paths with uncommitted changes. A bare repository has no
    working tree (and `git status` refuses to run there): nothing in it can
    be uncommitted."""
    if _git(root, "rev-parse", "--is-inside-work-tree").strip() != "true":
        return []
    # -z: paths come back raw and NUL-separated — no C-style quoting of
    # non-ASCII ("caf\303\251.py"), spaces or quotes to undo.
    try:
        # --ignore-submodules=dirty: still tells a submodule moved to another
        # commit, but does not start git inside it (its own config could
        # carry the filters and commands this function is shutting off)
        out = _git(root, "status", "--porcelain", "-z",
                   "--ignore-submodules=dirty",
                   config=_repo_filter_overrides(root))
    except (GitNotFound, GitTimeout):
        raise
    except GitError as e:
        # status can fail for reasons of its own (a broken index, a git too
        # old to say where a setting came from): the digest is still worth
        # having. Say so, once per kind, instead of a traceback or a quiet
        # empty list.
        said = " ".join("".join(ch if ch.isprintable() else " "
                                for ch in e.stderr).split())[:160]
        print("warning: could not read uncommitted files"
              + (f" ({said})" if said else ""), file=sys.stderr)
        return []
    tokens = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # a rename/copy is followed by the ORIGINAL path; skip it
        paths.append(path)
    return paths


def default_author(root: str) -> str:
    try:
        return _git(root, "config", "user.email").strip()
    except (GitNotFound, GitTimeout):
        raise
    except RuntimeError:
        return ""


def repo_name(root: str) -> str:
    try:
        out = _git(root, "rev-parse", "--show-toplevel").strip()
        return os.path.basename(out)
    except (GitNotFound, GitTimeout):
        raise
    except RuntimeError:
        pass
    # No working tree (a bare repository, or the .git folder of a normal
    # one): name it after its git directory, "project.git" -> "project".
    try:
        git_dir = _git(root, "rev-parse", "--git-dir").strip()
    except (GitNotFound, GitTimeout):
        raise
    except RuntimeError:
        return root
    path = os.path.abspath(os.path.join(root, git_dir))
    name = os.path.basename(path)
    if name == ".git":
        name = os.path.basename(os.path.dirname(path))
    elif name.endswith(".git"):
        name = name[:-4]
    return name or root
