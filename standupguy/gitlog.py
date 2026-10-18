"""Git ground truth: commits, files, merges, uncommitted work."""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import secrets
import subprocess
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


class GitNotFound(RuntimeError):
    """`git` is not on PATH (or not installed)."""


class GitError(RuntimeError):
    """A git command exited with an error. str() is a one-line summary (git's
    message cut at 200 characters); `stderr` is everything git said."""

    def __init__(self, args_text: str, stderr: str):
        super().__init__(f"git {args_text} failed: {stderr.strip()[:200]}")
        self.stderr = stderr.strip()


GIT_NOT_FOUND_MESSAGE = (
    "git not found on PATH. Install Git (https://git-scm.com/downloads; on "
    "Windows: Git for Windows), then open a new terminal.")


def _git(root: str, *args: str, env: dict | None = None) -> str:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    # git prints UTF-8 (commit messages, author names): decode it by hand,
    # never with the console code page, never dying on a stray byte, and
    # never in text mode — universal newlines would turn a lone CR inside a
    # commit subject into a newline and corrupt the record around it.
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True,
                           timeout=60, env=full_env)
    except FileNotFoundError:
        raise GitNotFound(GIT_NOT_FOUND_MESSAGE) from None
    if p.returncode != 0:
        raise GitError(" ".join(args), p.stderr.decode("utf-8", "replace"))
    return p.stdout.decode("utf-8", "replace")


_MERGE_RE = re.compile(r"merge pull request #(\d+)", re.IGNORECASE)

# Does a git date string already carry a time of day? Then it is passed to
# git untouched. Matches 10:00, 2026-10-14T10:00, noon/midnight/now/tea,
# "3 hours ago", 5pm, @1700000000 and raw unix timestamps.
_HAS_TIME_RE = re.compile(
    r"\d{1,2}:\d{2}"
    r"|\b(?:noon|midnight|now|tea|hours?|minutes?|seconds?)\b"
    r"|\d\s*[ap]m\b"
    r"|^@\d+$"
    r"|^\d{8,}$",
    re.IGNORECASE)

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
    if not s:
        return None
    m = _ISO_DATE_RE.match(s)
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
    if day:
        return (f"'{s}': git skips a weekday name unless 'last' or a number "
                f"comes right before it (a bare '{day}' means today). Use "
                f"'last {day}' for the most recent {day.capitalize()} before "
                f"today, or a date such as 2026-10-12.")
    return None


# today, yesterday, N days ago, N weeks ago (git also takes "2.days.ago")
_CALENDAR_DAY_RE = re.compile(
    r"^(?:(today)|(yesterday)|([0-9]{1,6})[\s.]+(day|week)s?[\s.]+ago)$",
    re.IGNORECASE)


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
    try:
        if m.group(1):
            return today
        if m.group(2):
            return today - datetime.timedelta(days=1)
        days = int(m.group(3)) * (7 if m.group(4).lower() == "week" else 1)
        return today - datetime.timedelta(days=days)
    except OverflowError:       # before year 1: leave it to git
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
    turned into the explicit local date here, see _calendar_day; any other
    word is passed to git VERBATIM — git does the date math, in its own
    local timezone, exactly as it would for the user typing it:

        yesterday      -> "2026-10-16 00:00"     (end: "2026-10-16 23:59:59")
        3 days ago     -> "2026-10-14 00:00"     (today is 2026-10-17)
        2026-10-14     -> "2026-10-14 00:00"     (end: "2026-10-14 23:59:59")
        last monday    -> "last monday 00:00"

    Values that already carry a time ("2026-10-14 15:30", "noon", "now",
    "2 hours ago", "@1700000000") pass through unchanged.
    """
    s = (since or "").strip()
    if not s or _HAS_TIME_RE.search(s):
        return s
    day = _calendar_day(s)
    if day is not None:
        s = day.isoformat()
    return f"{s} 23:59:59" if end else f"{s} 00:00"


def _fold(s: str) -> str:
    """Case- and width-insensitive form for author matching (NFKC + casefold),
    so 'ΞΈΝΟΣ' finds 'Ξένος' and trailing spaces never matter."""
    return unicodedata.normalize("NFKC", s or "").casefold().strip()


def author_matches(commit: Commit, needle: str | None) -> bool:
    """Does `needle` select this commit's author?

    Like `git log --author` but predictable: a case-insensitive substring
    of the author NAME, the author EMAIL, or the combined "Name <email>"
    form — never a regex (so 'dan+work@x.com' or 'J. Doe [bot]' match
    literally and can't make git fail), never case-sensitive, and
    whitespace around the needle is ignored. An empty needle matches
    everyone.
    """
    n = _fold(needle or "")
    if not n:
        return True
    name, email = _fold(commit.author), _fold(commit.email)
    return n in name or n in email or n in f"{name} <{email}>"


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
def _log_format(token: str = "") -> str:
    rs, us = "%x1e" + token, "%x1f" + token
    return f"--pretty=format:{rs}%H{us}%an{us}%ae{us}%cd{us}%s"


# What a record must look like to be believed: a hex sha (the exact set of
# real ones is checked against `git rev-list`, see commits_since) and the
# YYYY-MM-DD that --date=format-local:%Y-%m-%d prints.
_SHA_RE = re.compile(r"[0-9a-f]+")
_DAY_RE = re.compile(r"\d+-\d\d-\d\d")


def parse_log(out: str, token: str = "") -> list[Commit]:
    """Parse the output of `git log -z <_log_format(token)> --name-only`.

    A record that is not exactly five fields, or whose sha is not hex or
    whose date is not YYYY-MM-DD, is dropped rather than guessed at."""
    rs, us = "\x1e" + token, "\x1f" + token
    commits: list[Commit] = []
    for record in out.split(rs):
        header, _, body = record.partition("\n")
        header = header.strip("\0")
        if not header:
            continue
        fields = header.split(us, 4)
        if len(fields) != 5:
            continue
        sha, an, ae, ad, subject = fields
        if not (_SHA_RE.fullmatch(sha) and _DAY_RE.fullmatch(ad)):
            continue
        m = _MERGE_RE.search(subject)
        commits.append(Commit(
            sha=sha, author=an, email=ae, date=ad, subject=subject,
            files=[p for p in body.split("\0") if p],
            pr_number=int(m.group(1)) if m else None))
    return commits


def commits_since(root: str, since: str, author: str | None = None,
                  until: str | None = None) -> list[Commit]:
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
    except GitNotFound:
        raise
    except RuntimeError as e:
        # empty repo (no commits yet) -> no history, not an error
        if "unknown revision" in str(e):
            return []
        raise
    commits = [c for c in parse_log(out, token) if c.sha in real]
    if author:
        commits = [c for c in commits if author_matches(c, author)]
    return commits


def uncommitted(root: str) -> list[str]:
    """Repo-relative paths with uncommitted changes. A bare repository has no
    working tree (and `git status` refuses to run there): nothing in it can
    be uncommitted."""
    if _git(root, "rev-parse", "--is-inside-work-tree").strip() != "true":
        return []
    # -z: paths come back raw and NUL-separated — no C-style quoting of
    # non-ASCII ("caf\303\251.py"), spaces or quotes to undo.
    out = _git(root, "status", "--porcelain", "-z")
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
    except GitNotFound:
        raise
    except RuntimeError:
        return ""


def repo_name(root: str) -> str:
    try:
        out = _git(root, "rev-parse", "--show-toplevel").strip()
        return os.path.basename(out)
    except GitNotFound:
        raise
    except RuntimeError:
        pass
    # No working tree (a bare repository, or the .git folder of a normal
    # one): name it after its git directory, "project.git" -> "project".
    try:
        git_dir = _git(root, "rev-parse", "--git-dir").strip()
    except GitNotFound:
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
