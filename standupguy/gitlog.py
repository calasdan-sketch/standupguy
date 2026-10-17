"""Git ground truth: commits, files, merges, uncommitted work."""

from __future__ import annotations

import dataclasses
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


def _git(root: str, *args: str, env: dict | None = None) -> str:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    # git prints UTF-8 (commit messages, author names); never decode it with
    # the console code page and never die on a stray byte.
    p = subprocess.run(["git", "-C", root, *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=60, env=full_env)
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


def _since_to_arg(since: str) -> str:
    """Normalize a `since`/`until` value for git.

    Git's approxidate fills in the CURRENT time of day for a bare date or
    a word like "yesterday" ("2026-10-14" run at 15:00 means 2026-10-14
    15:00; "yesterday" means 24 hours ago) — silently dropping that
    morning's commits. For a standup you want whole days, so any value
    without an explicit time gets " 00:00" appended and the words
    themselves are passed to git VERBATIM — git does the date math, in
    its own local timezone, exactly as it would for the user typing it:

        yesterday      -> "yesterday 00:00"
        3 days ago     -> "3 days ago 00:00"
        2026-10-14     -> "2026-10-14 00:00"
        last monday    -> "last monday 00:00"

    Values that already carry a time ("2026-10-14 15:30", "noon", "now",
    "2 hours ago", "@1700000000") pass through unchanged.
    """
    s = (since or "").strip()
    if not s or _HAS_TIME_RE.search(s):
        return s
    return f"{s} 00:00"


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


def commits_since(root: str, since: str, author: str | None = None,
                  until: str | None = None) -> list[Commit]:
    """since/until: 'yesterday', '7 days ago', '2026-10-01', or git date
    syntax (see _since_to_arg). author: see author_matches."""
    since = _since_to_arg(since)
    # --date=format-local: the day header is the LOCAL date of this
    # machine, not the committer's timezone (--date=short), so a 23:30
    # PDT commit lands on the right day for a user in Winnipeg.
    args = ["log", f"--since={since}", "--date=format-local:%Y-%m-%d",
            "--pretty=format:COMMIT:%H|%an|%ae|%ad|%s",
            "--name-only", "HEAD"]
    if until:
        args.insert(2, f"--until={_since_to_arg(until)}")
    try:
        out = _git(root, *args)
    except RuntimeError as e:
        # empty repo (no commits yet) -> no history, not an error
        if "unknown revision" in str(e):
            return []
        raise
    commits: list[Commit] = []
    cur: Commit | None = None
    for line in out.splitlines():
        if line.startswith("COMMIT:"):
            if cur:
                commits.append(cur)
            _, rest = line.split("COMMIT:", 1)
            sha, an, ae, ad, subject = rest.split("|", 4)
            m = _MERGE_RE.search(subject)
            cur = Commit(sha=sha, author=an, email=ae, date=ad,
                         subject=subject, files=[],
                         pr_number=int(m.group(1)) if m else None)
        elif cur and line.strip():
            cur.files.append(line.strip())
    if cur:
        commits.append(cur)
    if author:
        commits = [c for c in commits if author_matches(c, author)]
    return commits


def uncommitted(root: str) -> list[str]:
    """Repo-relative paths with uncommitted changes."""
    out = _git(root, "status", "--porcelain")
    paths = []
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # format: XY <path> [-> <newpath>]
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        paths.append(path)
    return paths


def default_author(root: str) -> str:
    try:
        return _git(root, "config", "user.email").strip()
    except RuntimeError:
        return ""


def repo_name(root: str) -> str:
    try:
        out = _git(root, "rev-parse", "--show-toplevel").strip()
        return os.path.basename(out)
    except RuntimeError:
        return root
