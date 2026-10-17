"""Git ground truth: commits, files, merges, uncommitted work."""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import subprocess
import unicodedata


@dataclasses.dataclass
class Commit:
    sha: str
    author: str
    email: str
    date: str          # YYYY-MM-DD in the LOCAL timezone of this machine
    subject: str
    files: list[str]
    pr_number: int | None = None


class GitNotFound(RuntimeError):
    """`git` is not on PATH (or not installed)."""


GIT_NOT_FOUND_MESSAGE = (
    "git not found on PATH. Install Git (https://git-scm.com/downloads; on "
    "Windows: Git for Windows), then open a new terminal.")


def _git(root: str, *args: str, env: dict | None = None) -> str:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    # git prints UTF-8 (commit messages, author names); never decode it with
    # the console code page and never die on a stray byte.
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=60, env=full_env)
    except FileNotFoundError:
        raise GitNotFound(GIT_NOT_FOUND_MESSAGE) from None
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()[:200]}")
    return p.stdout


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
_DATE_WORDS = {
    "now", "today", "yesterday", "noon", "midnight", "tea", "never",
    "am", "pm", "ago", "last", "this", "next",
    "second", "seconds", "minute", "minutes", "hour", "hours",
    "day", "days", "week", "weeks", "month", "months", "year", "years",
    "fortnight", "utc", "gmt", "z", "t",
}


def _known_date_word(word: str) -> bool:
    if word in _DATE_WORDS:
        return True
    # git matches month/weekday names by prefix, 3 letters or more
    return len(word) >= 3 and any(
        name.startswith(word) for name in _MONTHS + _WEEKDAYS)


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
    return None


def _since_to_arg(since: str, end: bool = False) -> str:
    """Normalize a `since`/`until` value for git.

    Git's approxidate fills in the CURRENT time of day for a bare date or
    a word like "yesterday" ("2026-10-14" run at 15:00 means 2026-10-14
    15:00; "yesterday" means 24 hours ago) — silently dropping that
    morning's commits. For a standup you want whole days, so any value
    without an explicit time gets " 00:00" appended (or " 23:59:59" when
    it is the END of the range, so `--until 2026-10-15` includes the
    15th) and the words themselves are passed to git VERBATIM — git does
    the date math, in its own local timezone, exactly as it would for the
    user typing it:

        yesterday      -> "yesterday 00:00"      (end: "yesterday 23:59:59")
        3 days ago     -> "3 days ago 00:00"
        2026-10-14     -> "2026-10-14 00:00"     (end: "2026-10-14 23:59:59")
        last monday    -> "last monday 00:00"

    Values that already carry a time ("2026-10-14 15:30", "noon", "now",
    "2 hours ago", "@1700000000") pass through unchanged.
    """
    s = (since or "").strip()
    if not s or _HAS_TIME_RE.search(s):
        return s
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
# subject, then (with -z) the touched paths NUL-separated. Control
# characters cannot appear in a git ident or subject, and -z keeps paths
# raw (no quoting), so a '|' in an author name or subject, or a path that
# happens to start with "COMMIT:", can no longer break the parse.
_LOG_FORMAT = "--pretty=format:%x1e%H%x1f%an%x1f%ae%x1f%ad%x1f%s"


def parse_log(out: str) -> list[Commit]:
    """Parse the output of `git log -z <_LOG_FORMAT> --name-only`."""
    commits: list[Commit] = []
    for record in out.split("\x1e"):
        header, _, body = record.partition("\n")
        header = header.strip("\0")
        if not header:
            continue
        fields = header.split("\x1f", 4)
        if len(fields) != 5:
            continue
        sha, an, ae, ad, subject = fields
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
    # PDT commit lands on the right day for a user in Winnipeg.
    args = ["log", "-z", f"--since={_since_to_arg(since)}",
            "--date=format-local:%Y-%m-%d", _LOG_FORMAT, "--name-only"]
    if until:
        args.append(f"--until={_since_to_arg(until, end=True)}")
    args.append("HEAD")
    try:
        out = _git(root, *args)
    except GitNotFound:
        raise
    except RuntimeError as e:
        # empty repo (no commits yet) -> no history, not an error
        if "unknown revision" in str(e):
            return []
        raise
    commits = parse_log(out)
    if author:
        commits = [c for c in commits if author_matches(c, author)]
    return commits


def uncommitted(root: str) -> list[str]:
    """Repo-relative paths with uncommitted changes."""
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
        return root
